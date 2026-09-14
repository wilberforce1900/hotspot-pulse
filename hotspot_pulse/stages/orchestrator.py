"""
stages/orchestrator.py — 阶段0+7：V4 Pro 总指挥调度与最终审核。

分工：**V4 Pro 亲自实现**（最高总指挥，调度 + 拍板 + 安全审核）。
职责：
  1. 收原始输入 → input_parser 解析 + 安全过滤。
  2. 按依赖顺序（可并行则并行）派发各 stage 给对应子代理。
  3. 汇集各「草稿」→ 一致性校验 → 最终安全审核 → 生成 summary → PipelineResult。

委派映射（预演=本地函数调用，正式版=subagent 工具）：
  stage1  collector    → senior-coder
  stage2  topic_graph  → general-reasoner + senior-coder
  stage3  sentiment    → general-reasoner / multimodal-operator
  stage4  growth       → senior-coder(草稿) + V4 Pro(把关)
  stage5  region_heat  → general-reasoner + senior-coder
  stage6  renderer     → visual-analyst
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from hotspot_pulse.config import Config, default_mock_config
from hotspot_pulse.models import (
    HotspotGraph,
    PipelineResult,
    Query,
    RegionGraph,
    Sentiment,
    SentimentAgg,
    TopicGraph,
)
from hotspot_pulse.stages import collector, growth, region_heat, renderer, sentiment, topic_graph
from hotspot_pulse.stages.input_parser import _security_scan, parse_user_input, sanitize_for_delegation

logger = logging.getLogger(__name__)

# 阶段名 → 处理函数（预演=本地直调；正式版替换为 subagent 派发）
_STAGE_HANDLERS: dict[str, Callable[..., object]] = {
    "collector": collector.collect,
    "topic_graph": topic_graph.build_topic_graph,
    "sentiment": sentiment.analyze_sentiment,
    "region_heat": region_heat.build_region_graph,
    "growth": growth.predict_growth,
    "renderer": renderer.render_network,
}


async def dispatch(stage: str, *args, **kwargs):
    """把某阶段委派给对应子代理并收回「草稿」。

    预演实现：直接在本地线程调用对应 stage 模块（CPU 密集时让出事件循环）。
    正式版可对接 subagent 工具。V4 Pro 控制委派前/后安全边界。
    """
    handler = _STAGE_HANDLERS[stage]
    return await asyncio.to_thread(handler, *args, **kwargs)


async def dispatch_guarded(stage: str, warnings: list[str], degrade: Callable[[], object],
                           *args, **kwargs):
    """错误隔离地委派一个阶段：失败不拖垮整体，降级 + 告警（README §7 口径）。"""
    try:
        return await dispatch(stage, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — 阶段边界就是隔离带，任何异常都降级
        logger.exception("阶段 %s 执行失败，已降级", stage)
        warnings.append(f"阶段 {stage} 异常降级：{type(exc).__name__}: {exc}")
        return degrade()


async def _sentiment_stage(query: Query, posts, warnings: list[str]):
    """阶段3 按 include_sentiment 开关派发（此前该配置被无视，stage 总是运行）。"""
    if not query.include_sentiment:
        return None
    return await dispatch_guarded("sentiment", warnings, lambda: None,
                                  posts, query.include_images)


async def run_pipeline(raw_input: str, cfg: Config | None = None,
                       now=None) -> PipelineResult:
    """端到端调度主入口（V4 Pro 亲自）。

    流程：解析→安全过滤→采集→（话题/情绪/区域并行）→增长→渲染→汇总审核。
    now：注入时钟（传给采集层锚定 mock 窗口），缺省当前 UTC；测试可固定以完全复现。
    """
    if cfg is None:
        cfg = default_mock_config()

    # ---- 阶段0：解析 + 安全过滤（V4 Pro）----
    parsed = parse_user_input(raw_input, cfg)
    if parsed.rejected:
        return PipelineResult(
            query=None,
            summary=f"输入被安全过滤拒绝：{parsed.reason}",
            warnings=[parsed.reason],
        )
    if parsed.query is None:
        return PipelineResult(
            query=None,
            summary=f"无法解析输入：{parsed.reason}",
            warnings=[parsed.reason],
        )

    # 委派前二次脱敏（V4 Pro）
    query: Query = sanitize_for_delegation(parsed.query)
    warnings: list[str] = list(parsed.warnings)

    # ---- 阶段1：采集（senior-coder）----
    collect_res = await dispatch_guarded("collector", warnings, lambda: None, query, now)
    posts = collect_res.posts if collect_res is not None else []
    warnings.extend(collect_res.errors if collect_res is not None else [])
    if not posts:
        warnings.append("采集结果为 0 条，后续分析将为空（降级）")

    # ---- 阶段2/3/5：并行派发（三者互不依赖；情绪受 include_sentiment 开关控制）----
    topic, senti, region = await asyncio.gather(
        dispatch_guarded("topic_graph", warnings, TopicGraph,
                         posts, query.tag, query.related_terms),
        _sentiment_stage(query, posts, warnings),
        dispatch_guarded("region_heat", warnings, RegionGraph,
                         posts, cfg.region_mapper),
    )

    # ---- 阶段4：增长预测（senior-coder 草稿 → V4 Pro 审定）----
    predictions = await dispatch_guarded("growth", warnings, list,
                                         posts, cfg.predictor, query.horizon_hours)

    # ---- 阶段6：渲染（visual-analyst）----
    graph = await dispatch_guarded("renderer", warnings, lambda: None,
                                   region, topic, cfg.render)

    # ---- 阶段7：汇总 + 一致性校验 + 安全审核 + 拍板（V4 Pro）----
    return finalize(query, topic, senti, predictions, region, graph, warnings)


def finalize(
    query: Query,
    topic: TopicGraph | None,
    senti: SentimentAgg | None,
    predictions: list,
    region: RegionGraph | None,
    graph: HotspotGraph | None,
    warnings: list[str] | None = None,
) -> PipelineResult:
    """汇总 + 一致性校验 + 安全审核 + 生成 summary（V4 Pro 亲自）。"""
    warnings = list(warnings or [])

    # ---- 一致性校验 ----
    if topic is not None and query.tag not in topic.nodes:
        warnings.append(f"话题图缺少主节点「{query.tag}」")
    if region is not None and query.regions:
        missing = [r for r in query.regions if r not in region.nodes]
        if missing:
            warnings.append(f"限定地域无数据（已降级标注）：{', '.join(missing)}")
    if predictions and topic is not None:
        unknown_topics = [p.topic for p in predictions if p.topic not in topic.nodes]
        if unknown_topics:
            warnings.append(f"预测话题未出现在话题图中：{', '.join(unknown_topics[:5])}")

    # ---- 置信度下限（Query.confidence_threshold 接线）----
    # 低于阈值不剔除（剔除会让报告失去主要内容），而是标记 + 告警，由阅读者自行降权。
    if predictions:
        low_conf = [f"{p.topic}({p.confidence:.0%})" for p in predictions
                    if p.confidence < query.confidence_threshold]
        if low_conf:
            warnings.append(f"预测置信度低于阈值 {query.confidence_threshold:.0%}，仅供参考："
                            + "、".join(low_conf[:5]))

    # ---- 最终安全审核：对下游产出的文本类字段再过一遍注入扫描 ----
    for label, text in _audit_fragments(topic, senti, region):
        hit = _security_scan(text)
        if hit:
            warnings.append(f"安全审核拦截下游产物[{label}]：{hit}")

    summary = _build_summary(query, topic=topic, senti=senti,
                             predictions=predictions, region=region, graph=graph,
                             warnings=warnings)

    return PipelineResult(
        query=query,
        topic_graph=topic,
        sentiment=senti,
        predictions=predictions,
        region_graph=region,
        hotspot_graph=graph,
        summary=summary,
        warnings=warnings,
    )


def _audit_fragments(topic, senti, region):
    """收集需要做安全扫描的文本片段（下游子代理产物，视为不可信草稿）。"""
    frags: list[tuple[str, str]] = []
    if topic is not None:
        for name in topic.nodes:
            frags.append(("话题名", name))
    if region is not None:
        for name in region.nodes:
            frags.append(("区域名", name))
        for e in region.edges:
            frags.append(("区域边", f"{e.from_region}->{e.to_region}"))
    return frags


def _fmt_sentiment(s: Sentiment | None) -> str:
    if s is None:
        return "N/A"
    return f"{s.label.value}(正{s.positive:.0%}/负{s.negative:.0%}/中{s.neutral:.0%})"


def _build_summary(query, topic, senti, predictions, region,
                   graph: HotspotGraph | None, warnings) -> str:
    lines = [
        f"# 热点预测报告 · tag「{query.tag}」",
        f"- 窗口：回溯 {query.window_hours}h / 预测 {query.horizon_hours}h / "
        f"数据源 {query.data_source.value} / 预测器 {query.predictor.value}",
    ]

    # 关联话题
    if topic is not None and topic.nodes:
        names = sorted(topic.nodes, key=lambda n: topic.nodes[n].post_count, reverse=True)
        top_names = [n for n in names if n != query.tag][:5]
        lines.append(f"- 关联话题：{'、'.join(top_names) if top_names else '（无强关联话题）'}")
    else:
        lines.append("- 关联话题：无数据")

    # 增长预测
    if predictions:
        top = predictions[0]
        tail_vol = top.predicted_volume[-1] if top.predicted_volume else 0.0
        sig = top.signal
        signal_txt = ""
        if sig is not None:
            signal_txt = f"，速度 {sig.velocity:.1f}/h"
            if sig.is_breakout:
                signal_txt += "，⚠️ 已触发破点"
        interval_txt = ""
        if top.ci_low and top.ci_high:
            interval_txt = f"（80% 区间 {top.ci_low[-1]:.1f}~{top.ci_high[-1]:.1f}）"
        if top.confidence < query.confidence_threshold:
            signal_txt += f"，⚠️ 置信度低于阈值 {query.confidence_threshold:.0%}"
        lines.append(
            f"- 增长预测：热度最高话题「{top.topic}」，未来末期量约 {tail_vol:.1f}{interval_txt}，"
            f"置信度 {top.confidence:.0%}{signal_txt}"
        )
        if len(predictions) > 1:
            lines.append(f"  （共 {len(predictions)} 个话题有预测）")
    else:
        lines.append("- 增长预测：无数据")

    # 情绪
    if senti is not None:
        overall = _fmt_sentiment(senti.overall)
        lines.append(f"- 情绪：整体 {overall}")
        if senti.by_region:
            top_regions = sorted(senti.by_region, key=lambda r: abs(senti.by_region[r].positive - 0.5))
            r = top_regions[0] if top_regions else ""
            if r:
                lines.append(f"  区域「{r}」：{_fmt_sentiment(senti.by_region[r])}")
    else:
        lines.append("- 情绪：未启用")

    # 区域热度
    if region is not None and region.nodes:
        top_r = sorted(region.nodes, key=lambda n: region.nodes[n].heat_score, reverse=True)
        top3 = [f"{n}({region.nodes[n].post_count}条)" for n in top_r[:3]]
        lines.append(f"- 区域热度 TOP：{', '.join(top3)}")
    else:
        lines.append("- 区域热度：无数据")

    # 网图产物
    if graph is not None and graph.graph_path:
        lines.append(f"- 热力网图：已生成 `{graph.graph_path}`")
    else:
        lines.append("- 热力网图：未生成")

    if warnings:
        lines.append(f"- 数据质量/降级提示（{len(warnings)} 条）：")
        for w in warnings[:5]:
            lines.append(f"  · {w}")

    return "\n".join(lines)

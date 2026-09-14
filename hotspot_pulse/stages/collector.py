"""
stages/collector.py — 阶段1：数据采集。

分工：senior-coder 实现（机械、批量、重 I/O，适合委派），V4 Pro 审定落地。
职责：
  1. 按 Query.data_source 选择适配器（social_api / search / crawler / mock）。
  2. 拉取/轮询原始数据，统一转成 Post。
  3. 幂等去重（(source, external_id)），返回 CollectResult。
"""

from __future__ import annotations

import hashlib
import logging
import random
from datetime import datetime, timedelta, timezone

from hotspot_pulse.models import CollectResult, DataSource, Post, Query

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 共享情绪词典（与 sentiment.py 阶段一致，保证下游情绪分析可确定复现）
# --------------------------------------------------------------------------- #
_POSITIVE_WORDS = ["利好", "上涨", "突破", "增长", "创新", "火爆", "看涨", "惊喜", "期待", "支持"]
_NEGATIVE_WORDS = ["利空", "下跌", "风险", "担忧", "争议", "亏损", "暴跌", "质疑", "恐慌", "失望"]
_NEUTRAL_WORDS = ["发布", "报道", "讨论", "关注", "观察", "数据显示"]

# 关联词池（用于生成话题共现信号）
_RELATED_TAGS = ["股价", "芯片", "科技", "政策", "市场", "研发", "供应链", "出口"]

# 默认区域池
_DEFAULT_REGIONS = ["中国", "美国", "日本", "欧洲"]


def _stable_seed(tag: str) -> int:
    """稳定种子：跨进程可复现（不用内置 hash()，其受 PYTHONHASHSEED 影响）。"""
    return int.from_bytes(hashlib.md5(tag.encode("utf-8")).digest()[:8], "big")


def _mock_text(tag: str, category: str) -> str:
    """按情绪类别生成确定性模板文本（嵌共享词典词）。"""
    if category == "positive":
        return f"{tag} 传来利好，市场看涨，投资者感到惊喜。"
    if category == "negative":
        return f"{tag} 出现利空，市场担忧，股价可能暴跌。"
    return f"关于{tag}的最新报道，数据显示讨论持续升温。"


def _collect_mock(query: Query, now: datetime) -> list[Post]:
    """mock 数据源：离线生成仿真 Post 流，供预演/测试。

    可复现性：tag 决定随机种子；now 由调用方注入（缺省当前 UTC），
    同一 (tag, now) 两次调用产出完全相同的 Post 流。
    """
    rng = random.Random(_stable_seed(query.tag))
    count = rng.randint(300, 500)
    region_pool = query.regions if query.regions else _DEFAULT_REGIONS
    # 用户指定的关联词进入采样池：让用户意图传导到共现信号（related_terms 接线）
    related_pool = list(_RELATED_TAGS) + [
        t for t in query.related_terms if t and t not in _RELATED_TAGS
    ]

    window_start = now - timedelta(hours=query.window_hours)

    posts: list[Post] = []
    for i in range(count):
        # 时间偏置：越接近当下越多（模拟话题「升温」）。指数<1 使 rng.random() 向 1 聚集，
        # 让下游趋势预测能学到正斜率，演示增长预测与破点检测正常工作。
        offset_hours = query.window_hours * (rng.random() ** 0.6)
        created_at = window_start + timedelta(hours=offset_hours)
        region = rng.choice(region_pool)

        roll = rng.random()
        if roll < 0.40:
            category = "positive"
        elif roll < 0.75:
            category = "negative"
        else:
            category = "neutral"
        text = _mock_text(query.tag, category)

        num_related = rng.randint(1, 3)
        related = rng.sample(related_pool, k=num_related)
        tags = [query.tag] + related

        posts.append(Post(
            external_id=f"mock-{i}",
            source=DataSource.MOCK,
            created_at=created_at,
            text=text,
            tags=tags,
            region=region,
            raw={"mock": True},
        ))
    logger.info("mock 采集完成：tag=%s 共 %d 条", query.tag, len(posts))
    return posts


def _collect_social_api(query: Query) -> list[Post]:
    """社交平台 API 适配器。senior-coder。离线预演：返回空列表。"""
    return []


def _collect_search(query: Query) -> list[Post]:
    """搜索引擎/舆情检索适配器。senior-coder。离线预演：返回空列表。"""
    return []


def collect(query: Query, now: datetime | None = None) -> CollectResult:
    """采集主入口：路由到对应适配器并聚合结果（错误隔离 + 幂等去重）。

    now：注入时钟（mock 模式用它锚定窗口），缺省当前 UTC；测试可固定以完全复现。
    """
    errors: list[str] = []
    posts: list[Post] = []
    seen: set[tuple[str, str]] = set()
    deduped = 0
    now = now or datetime.now(timezone.utc)

    try:
        ds = query.data_source
        if ds == DataSource.MOCK:
            raw = _collect_mock(query, now)
        elif ds == DataSource.SOCIAL_API:
            raw = _collect_social_api(query)
            errors.append("源 SOCIAL_API 尚未实现（离线预演）")
        elif ds == DataSource.SEARCH:
            raw = _collect_search(query)
            errors.append("源 SEARCH 尚未实现（离线预演）")
        else:  # CRAWLER 或未知源
            raw = []
            errors.append(f"源 {ds.value} 尚未实现（离线预演）")

        for post in raw:
            key = (post.source.value, post.external_id)
            if key in seen:
                deduped += 1
            else:
                seen.add(key)
                posts.append(post)
    except Exception as exc:  # 错误隔离：任一源失败不拖垮整体
        logger.exception("采集源 %s 失败", query.data_source.value)
        errors.append(f"采集异常：{exc}")

    if deduped:
        logger.info("去重丢弃 %d 条重复 Post", deduped)
    return CollectResult(
        posts=posts,
        fetched_at=now,
        errors=errors,
        deduped=deduped,
    )

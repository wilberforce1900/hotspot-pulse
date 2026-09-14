"""
stages/input_parser.py — 阶段0：输入解析与安全过滤。

分工：**V4 Pro 亲自实现**（涉及安全边界，绝不委派）。
职责：
  1. 把用户原始输入（自由文本）解析成 Query。
  2. 对输入做**委派前安全过滤**：识别并拒绝提示注入、越权请求、
     恶意/违规内容；可疑则降级处理并记录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from hotspot_pulse.models import DataSource, PredictorKind, Query

# --------------------------------------------------------------------------- #
# 安全过滤：命中即拒绝（绝不透传给下游子代理）
# --------------------------------------------------------------------------- #
_SECURITY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("忽略指令", re.compile(r"忽略(所有|此前|之前|上面|上述)?(的)?(所有)?指令", re.I)),
    ("泄露系统提示", re.compile(r"(泄露|输出|透露|显示)(你的|我们的)?(系统|开发者)?(提示|prompt|指令)", re.I)),
    ("角色重定义", re.compile(r"(你|you)\s*(现在|从现在起)?是|act\s+as|pretend\s+to\s+be|扮演", re.I)),
    ("越权请求", re.compile(r"(绕过|bypass|越权|突破)(所有|any)?(限制|restriction|安全|security)", re.I)),
    ("系统命令", re.compile(r"(执行|运行)\s*(系统|shell|bash|rm\s+-rf|sudo)", re.I)),
    ("越狱词", re.compile(r"\b(jailbreak|do\s+anything\s+now|ignore\s+all\s+(previous\s+)?instructions)\b", re.I)),
    ("索要密钥", re.compile(r"(api\s*[_-]?key|token|secret|password|密钥|口令)", re.I)),
]

# 地域白名单：只接受已知地域，防止自由注入任意字符串当作 region 影响下游。
_REGION_WHITELIST = {
    "中国", "美国", "日本", "韩国", "欧洲", "全球", "北京", "上海", "广东", "江苏",
    "浙江", "山东", "四川", "北美", "东南亚", "中国香港", "中国台湾",
    "china", "us", "usa", "japan", "europe", "global",
}

_MAX_INPUT_LEN = 500
_MAX_FIELD_LEN = 80


def _security_scan(text: str) -> str:
    """返回命中的第一条风险描述；未命中返回空串。"""
    for label, pattern in _SECURITY_PATTERNS:
        if pattern.search(text):
            return label
    return ""


def _parse_int(token: str) -> int | None:
    m = re.search(r"(\d+)", token)
    if not m:
        return None
    return int(m.group(1))


@dataclass(slots=True)
class ParseResult:
    """解析产物：要么拿到 Query，要么标记为可疑。"""
    query: Query | None = None
    rejected: bool = False              # 是否因安全风险被拒
    reason: str = ""                    # 拒绝/降级原因
    warnings: list[str] = field(default_factory=list)


def parse_user_input(raw: str, cfg: object | None = None) -> ParseResult:
    """把用户原始输入解析为 Query（V4 Pro 亲自，含安全过滤）。

    支持的轻量语法（预演足够）：
      - 裸词：第一个裸词 = 主 tag，其后裸词 = 关联词（related_terms）
      - 地域：命中白名单的裸词归入 regions（"全球"/"global" → 空=全球）
      - 标记：含图片/图片/image → include_images；不含情绪/no_sentiment → 关情绪
      - key:value：window/horizon/region/tag/source/predictor
      - N小时 / 预测N小时：window / horizon
    """
    warnings: list[str] = []

    if not raw or not raw.strip():
        return ParseResult(query=None, rejected=False, reason="空输入", warnings=warnings)

    text = raw.strip()

    # 1) 安全过滤（先于一切解析）
    hit = _security_scan(text)
    if hit:
        return ParseResult(rejected=True, reason=f"安全过滤命中[{hit}]，已拒绝", warnings=warnings)

    # 2) 长度上限（防超长垃圾）
    if len(text) > _MAX_INPUT_LEN:
        text = text[:_MAX_INPUT_LEN]
        warnings.append(f"输入过长，已截断至 {_MAX_INPUT_LEN} 字符")

    # 3) 分词
    tokens = [t for t in re.split(r"[\s,，;；]+", text) if t]

    tag = ""
    related: list[str] = []
    regions: list[str] = []
    include_sentiment = True
    include_images = False
    window_hours = 24
    horizon_hours = 6
    data_source = DataSource.MOCK
    predictor = PredictorKind.LINEAR

    # 从 cfg 继承默认（若传入 Config）
    if cfg is not None:
        if getattr(getattr(cfg, "predictor", None), "kind", None) is not None:
            predictor = cfg.predictor.kind
        if getattr(getattr(cfg, "render", None), "output_dir", None):
            pass  # render 与 Query 无关，忽略

    def set_field(key: str, value: str) -> None:
        nonlocal tag, window_hours, horizon_hours, data_source, predictor
        key = key.strip().lower()
        value = value.strip()
        if key in ("tag", "topic", "话题"):
            tag = value
        elif key in ("window", "窗口", "回溯"):
            n = _parse_int(value)
            if n:
                window_hours = max(1, min(n, 24 * 30))
        elif key in ("horizon", "预测", "forecast"):
            n = _parse_int(value)
            if n:
                horizon_hours = max(1, min(n, 24 * 7))
        elif key in ("region", "regions", "地域"):
            for r in re.split(r"[|/]", value):
                r = r.strip()
                if r and r.lower() not in ("global", "全球"):
                    regions.append(r)
        elif key in ("source", "data_source", "数据源"):
            if value in {e.value for e in DataSource}:
                data_source = DataSource(value)
        elif key in ("predictor", "预测器"):
            if value in {p.value for p in PredictorKind}:
                predictor = PredictorKind(value)

    for tok in tokens:
        low = tok.lower()

        # 标记词（布尔）
        if low in ("含图片", "图片", "image", "images", "带图"):
            include_images = True
            continue
        if low in ("不含情绪", "无情绪", "no_sentiment", "nosentiment"):
            include_sentiment = False
            continue
        if low in ("情绪", "sentiment"):
            include_sentiment = True
            continue
        # N小时 / 预测N小时
        if re.fullmatch(r"\d+\s*小时", tok):
            window_hours = max(1, min(_parse_int(tok) or 24, 24 * 30))
            continue
        m_horizon = re.fullmatch(r"预测\s*(\d+)\s*小时", tok)
        if m_horizon:
            horizon_hours = max(1, min(int(m_horizon.group(1)), 24 * 7))
            continue

        # key:value / key=value
        m_kv = re.match(r"^([A-Za-z_\u4e00-\u9fff]+)\s*[:=]\s*(.+)$", tok)
        if m_kv:
            set_field(m_kv.group(1), m_kv.group(2))
            continue

        # 地域白名单
        if tok in _REGION_WHITELIST:
            if tok.lower() not in ("global", "全球"):
                regions.append(tok)
            continue

        # 裸词：第一个=tag，其余=关联词
        if not tag:
            tag = tok
        else:
            related.append(tok)

    # 4) 无 tag → 无法分析
    if not tag:
        return ParseResult(query=None, rejected=False, reason="未识别到有效 tag", warnings=warnings)

    # 5) 字段规整
    tag = tag[: _MAX_FIELD_LEN]
    related = [t[:_MAX_FIELD_LEN] for t in related][:20]
    regions = list(dict.fromkeys(r[:_MAX_FIELD_LEN] for r in regions))[:20]

    q = Query(
        tag=tag,
        related_terms=related,
        window_hours=window_hours,
        horizon_hours=horizon_hours,
        regions=regions,
        include_sentiment=include_sentiment,
        include_images=include_images,
        data_source=data_source,
        predictor=predictor,
    )
    return ParseResult(query=q, rejected=False, warnings=warnings)


def sanitize_for_delegation(q: Query) -> Query:
    """在把 Query 交给子代理前，做二次脱敏/剥离（V4 Pro 亲自把关）。

    返回一个「安全副本」：截断字段、白名单化 region、剔除残留注入片段。
    不改动原对象。
    """
    def clean(s: str) -> str:
        s = (s or "").strip()
        s = s.replace("\x00", "").replace("\n", " ").replace("\r", " ")
        # 剔除注入片段（用占位替换，而非整串丢弃）
        for _label, pattern in _SECURITY_PATTERNS:
            s = pattern.sub("", s)
        return s[: _MAX_FIELD_LEN]

    return Query(
        tag=clean(q.tag),
        related_terms=[clean(t) for t in q.related_terms if clean(t)][:20],
        window_hours=q.window_hours,
        horizon_hours=q.horizon_hours,
        regions=[r for r in q.regions if r in _REGION_WHITELIST][:20],
        include_sentiment=q.include_sentiment,
        include_images=q.include_images,
        data_source=q.data_source,
        predictor=q.predictor,
        confidence_threshold=q.confidence_threshold,
    )

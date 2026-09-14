"""
models.py — HotSpot Pulse 统一数据契约（数据层 / 地基）。

为什么先写这个文件：
  主模型与子代理各自开发不同阶段，彼此只通过本文件的 dataclass 交换数据。
  「契约先行」保证：parallel 开发互不阻塞、阶段可独立测试、可 mock。
  任何子代理都**不得擅自改字段**，改动必须由主模型（V4 Pro）审定后统一改。

所有模型建议在实现阶段套 pydantic 做校验；这里先用 @dataclass + 注释把
「字段含义 / 类型 / 单位 / 可选性」钉死。

作者分工：本文件由 V4 Pro 亲自维护（地基，不委派）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def _utcnow() -> datetime:
    """timezone-aware UTC 当前时间（datetime.utcnow 已弃用，naive 时间无法做时区运算）。"""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 枚举：用枚举固定「合法取值」，避免各子代理自由发明字符串
# --------------------------------------------------------------------------- #
class SentimentLabel(str, Enum):
    """情绪极性标签。"""
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"          # 情绪混杂，需细分维度


class EmotionKind(str, Enum):
    """细粒度情绪类别（可选，供情绪分析扩展）。"""
    ANGER = "anger"
    FEAR = "fear"
    JOY = "joy"
    SADNESS = "sadness"
    SURPRISE = "surprise"
    NEUTRAL = "neutral"


class DataSource(str, Enum):
    """数据源适配器标识。"""
    SOCIAL_API = "social_api"
    SEARCH = "search"
    CRAWLER = "crawler"
    MOCK = "mock"            # 离线/预演模式


class PredictorKind(str, Enum):
    """增长预测算法标识（策略模式，可插拔）。"""
    DAMPED = "damped"        # 阻尼趋势（damped linear）：热点 S 形演化的廉价近似，默认
    LINEAR = "linear"        # 无阻尼线性趋势，最省/最快
    ARIMA = "arima"
    PROPHET = "prophet"
    LSTM = "lstm"            # 最贵，需数据量足够


class LayoutAlgo(str, Enum):
    """网图布局算法。"""
    FORCE = "force"          # networkx spring/kamada
    GRAPHVIZ = "graphviz"    # 需要 graphviz 可执行


# --------------------------------------------------------------------------- #
# 输入层
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Query:
    """阶段0 产出：标准化的分析请求（唯一的用户意图载体）。

    由 input_parser 从原始用户输入解析而来，是采集与分析的总入口。
    """
    tag: str                            # 主话题/tag（规范化后，如 'AI芯片'）
    related_terms: list[str] = field(default_factory=list)  # 用户附加的关联词（可选）
    window_hours: int = 24              # 采集回溯窗口（小时）
    horizon_hours: int = 6              # 预测未来时长（小时）
    regions: list[str] = field(default_factory=list)  # 限定地域（空=全球）
    include_sentiment: bool = True      # 是否需要情绪分析
    include_images: bool = False        # 是否有多模态（图片）需要分析
    data_source: DataSource = DataSource.MOCK  # 采集源（预演默认 mock）
    predictor: PredictorKind = PredictorKind.DAMPED  # 预测算法（预演默认阻尼趋势）
    confidence_threshold: float = 0.7   # 预测置信度下限（低于则在报告标记+告警）


# --------------------------------------------------------------------------- #
# 采集层
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Post:
    """阶段1 产出：一条规范化后的原始消息/帖子。

    采集层必须把不同源（API/搜索/爬虫）的异构数据统一成这个结构。
    这是情感、话题、区域分析的唯一数据来源。
    """
    external_id: str                    # 源内唯一 ID（用于去重，跨源前拼上 source）
    source: DataSource                  # 来源
    created_at: datetime                # 发布时间（时区统一 UTC）
    author: str = ""                    # 作者（脱敏可选）
    text: str = ""                      # 正文文本
    images: list[str] = field(default_factory=list)  # 图片 URL/路径（多模态用）
    tags: list[str] = field(default_factory=list)    # 原文带的话题标签
    region: Optional[str] = None        # 已推断地域（GeoIP/字段；None=未知）
    raw: dict = field(default_factory=dict)          # 原始载荷，供回溯（不进分析）


@dataclass(slots=True)
class CollectResult:
    """采集层返回：Post 列表 + 元信息。"""
    posts: list[Post] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=_utcnow)
    errors: list[str] = field(default_factory=list)   # 各源失败/降级信息
    deduped: int = 0                    # 去重丢弃条数


# --------------------------------------------------------------------------- #
# 关联话题层
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Topic:
    """一个话题节点。"""
    name: str                           # 话题名/tag
    heat_score: float = 0.0             # 热度分（0~1，采集+增长综合）
    post_count: int = 0
    top_sentiment: Optional[SentimentLabel] = None


@dataclass(slots=True)
class RelatedEdge:
    """话题之间的关联边。"""
    from_topic: str
    to_topic: str
    weight: float                       # 关联度（0~1，共现/语义相似归一化）
    cooccurrence: int = 0               # 共现条数（可解释性用）


@dataclass(slots=True)
class TopicGraph:
    """阶段2 产出：话题网络（用于热力网图中的「话题子图」与关联展示）。"""
    nodes: dict[str, Topic] = field(default_factory=dict)   # name -> Topic
    edges: list[RelatedEdge] = field(default_factory=list)
    as_of: datetime = field(default_factory=_utcnow)


# --------------------------------------------------------------------------- #
# 情绪层
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Sentiment:
    """单条 Post 的情绪判定。"""
    label: SentimentLabel
    positive: float                     # 0~1
    negative: float
    neutral: float
    emotions: dict[EmotionKind, float] = field(default_factory=dict)  # 细粒度分数


@dataclass(slots=True)
class SentimentAgg:
    """阶段3 产出：按维度聚合的情绪分布。

    可同时按 topic、按 region 聚合——因此用 key 表达维度。
    """
    by_topic: dict[str, Sentiment] = field(default_factory=dict)    # topic -> 聚合情绪
    by_region: dict[str, Sentiment] = field(default_factory=dict)   # region -> 聚合情绪
    overall: Optional[Sentiment] = None                             # 全局


# --------------------------------------------------------------------------- #
# 增长预测层
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class HeatRecord:
    """一条时间桶热度记录（预测的输入时序）。"""
    ts: datetime
    topic: str
    volume: int                         # 该桶消息量
    heat_score: float = 0.0             # 归一化热度


@dataclass(slots=True)
class TrendSignal:
    """增长信号（判断是否「破点/加速」）。"""
    velocity: float                     # 一阶导数（量/小时）
    acceleration: float                 # 二阶导数
    is_breakout: bool                   # 是否出现爆发破点
    breakout_at: Optional[datetime] = None


@dataclass(slots=True)
class Prediction:
    """阶段4 产出：一个话题的短期预测。"""
    topic: str
    horizon_hours: int
    predicted_volume: list[float]       # 每个未来小时预测量
    forecast: list[tuple[datetime, float]] = field(default_factory=list)  # (时间, 预测量)
    confidence: float = 0.0             # 0~1（拟合 R²，含样本不足折扣）
    signal: Optional[TrendSignal] = None
    model: PredictorKind = PredictorKind.DAMPED
    ci_low: list[float] = field(default_factory=list)   # 80% 置信区间下界（与 predicted_volume 等长）
    ci_high: list[float] = field(default_factory=list)  # 80% 置信区间上界


# --------------------------------------------------------------------------- #
# 区域热度层
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RegionNode:
    """区域节点。"""
    region: str
    heat_score: float = 0.0
    post_count: int = 0
    top_topics: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RegionEdge:
    """跨区传播/交互边。"""
    from_region: str
    to_region: str
    weight: float                       # 传播/交互强度


@dataclass(slots=True)
class RegionGraph:
    """阶段5 产出：区域热度网络（热力网图的「区域子图」）。"""
    nodes: dict[str, RegionNode] = field(default_factory=dict)
    edges: list[RegionEdge] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 渲染层 / 最终
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class LayoutHint:
    """网图布局/视觉参数（由 config + 渲染器共同决定）。"""
    algorithm: LayoutAlgo = LayoutAlgo.FORCE
    width: int = 12
    height: int = 8
    color_map: str = "YlOrRd"           # 热度颜色映射（冷→热）
    node_size_by: str = "heat"          # 节点大小编码字段


@dataclass(slots=True)
class HotspotGraph:
    """阶段6 产出：最终热力网图（区域子图 + 话题子图可叠加）。

    视觉层产出：渲染后的 PNG/SVG 路径 + 结构化图数据（供报告引用）。
    """
    graph_path: str                     # 渲染出的图片文件路径
    nodes: list[dict] = field(default_factory=list)      # 序列化后的节点(含 heat/大小)
    edges: list[dict] = field(default_factory=list)      # 序列化后的边
    layout: LayoutHint = field(default_factory=LayoutHint)
    combined_topics: bool = False       # 是否把话题子图叠到区域图上


@dataclass(slots=True)
class PipelineResult:
    """阶段7 产出：端到端最终结果（V4 Pro 审定后发布）。"""
    query: Optional[Query] = None       # 输入被拒/无法解析时可为 None
    topic_graph: Optional[TopicGraph] = None
    sentiment: Optional[SentimentAgg] = None
    predictions: list[Prediction] = field(default_factory=list)
    region_graph: Optional[RegionGraph] = None
    hotspot_graph: Optional[HotspotGraph] = None
    summary: str = ""                   # V4 Pro 生成的最终结论文案
    warnings: list[str] = field(default_factory=list)     # 降级/数据质量提示

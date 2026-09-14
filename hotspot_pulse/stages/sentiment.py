"""
stages/sentiment.py — 阶段3：情绪分析。

分工：general-reasoner（文本情绪口径/分类）+ multimodal-operator（含图片时）。
职责：
  1. 逐 Post 判情绪（SentimentLabel + 细粒度 EmotionKind）。
  2. 可选：对 Post.images 做图像情绪（多模态，触发时交给 multimodal-operator）。
  3. 按 topic、按 region 聚合 → SentimentAgg。
"""

from __future__ import annotations

from collections import Counter, defaultdict

from hotspot_pulse.models import EmotionKind, Post, Sentiment, SentimentAgg, SentimentLabel

# --------------------------------------------------------------------------- #
# 共享情绪词典（与 collector.py 阶段一致）
# --------------------------------------------------------------------------- #
_POSITIVE_KEYWORDS = {"利好", "上涨", "突破", "增长", "创新", "火爆", "看涨", "惊喜", "期待", "支持"}
_NEGATIVE_KEYWORDS = {"利空", "下跌", "风险", "担忧", "争议", "亏损", "暴跌", "质疑", "恐慌", "失望"}

_EMOTION_KEYWORDS = {
    EmotionKind.ANGER: {"愤怒", "气愤"},
    EmotionKind.FEAR: {"恐惧", "恐慌", "担忧"},
    EmotionKind.JOY: {"开心", "惊喜", "兴奋"},
    EmotionKind.SADNESS: {"难过", "失望"},
    EmotionKind.SURPRISE: {"惊讶", "意外"},
}


def _normalize_probabilities(pos: int, neg: int) -> tuple[float, float, float]:
    """命中次数 → (positive, negative, neutral)，三者之和 ≈ 1（+1 为中性基座）。"""
    total = pos + neg + 1
    return pos / total, neg / total, 1.0 / total


def _normalize_emotions(counts: Counter[EmotionKind]) -> dict[EmotionKind, float]:
    if not counts:
        return {}
    total = sum(counts.values())
    return {k: v / total for k, v in counts.items()}


def _aggregate_sentiments(items: list[Sentiment]) -> Sentiment:
    """一组 Sentiment → 单个聚合（概率求平均、label 取众数、emotions 求平均）。"""
    if not items:
        return Sentiment(label=SentimentLabel.NEUTRAL, positive=0.0, negative=0.0,
                         neutral=1.0, emotions={})
    n = len(items)
    positive = sum(s.positive for s in items) / n
    negative = sum(s.negative for s in items) / n
    neutral = sum(s.neutral for s in items) / n
    label = Counter(s.label for s in items).most_common(1)[0][0]

    emotion_groups: dict[EmotionKind, list[float]] = defaultdict(list)
    for s in items:
        for k, v in s.emotions.items():
            emotion_groups[k].append(v)
    emotions = {k: sum(vs) / len(vs) for k, vs in emotion_groups.items()}

    return Sentiment(label=label, positive=positive, negative=negative,
                     neutral=neutral, emotions=emotions)


def analyze_sentiment(posts: list[Post], include_images: bool) -> SentimentAgg:
    """情绪分析主入口：逐 Post 分类，按 topic 与 region 两套聚合 + 全局。"""
    flat: list[tuple[str, str, Sentiment]] = []
    for post in posts:
        s = _classify_text(post.text)
        # include_images=True 时含图 Post 可走多模态；预演无真实图，保留文本结果
        if include_images and post.images:
            _classify_image(post)  # 占位：后续接视觉模型
        topics = post.tags if post.tags else ["unknown"]
        region = post.region if post.region else "unknown"
        for topic in topics:
            flat.append((topic, region, s))
    return _aggregate(flat)


def _classify_text(text: str) -> Sentiment:
    """单条文本情绪分类（共享词典口径）。"""
    text = (text or "").lower()
    pos = sum(1 for kw in _POSITIVE_KEYWORDS if kw in text)
    neg = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in text)

    if pos == 0 and neg == 0:
        label = SentimentLabel.NEUTRAL
    elif pos > 0 and neg == 0:
        label = SentimentLabel.POSITIVE
    elif pos == 0 and neg > 0:
        label = SentimentLabel.NEGATIVE
    else:
        label = SentimentLabel.MIXED

    positive, negative, neutral = _normalize_probabilities(pos, neg)

    emotion_counts: Counter[EmotionKind] = Counter()
    for kind, keywords in _EMOTION_KEYWORDS.items():
        hit = sum(1 for kw in keywords if kw in text)
        if hit:
            emotion_counts[kind] += hit

    return Sentiment(label=label, positive=positive, negative=negative,
                     neutral=neutral, emotions=_normalize_emotions(emotion_counts))


def _classify_image(post: Post) -> Sentiment:
    """图像情绪/表情识别（多模态）。预演占位：返回 NEUTRAL，后续接视觉模型。"""
    return Sentiment(label=SentimentLabel.NEUTRAL, positive=0.0, negative=0.0,
                     neutral=1.0, emotions={})


def _aggregate(sentiments: list[tuple[str, str, Sentiment]]) -> SentimentAgg:
    """按 (topic, region) 聚合成两套分布 + 全局。空输入返回零值 NEUTRAL。"""
    topic_groups: dict[str, list[Sentiment]] = defaultdict(list)
    region_groups: dict[str, list[Sentiment]] = defaultdict(list)
    all_items: list[Sentiment] = []
    for topic, region, s in sentiments:
        topic_groups[topic].append(s)
        region_groups[region].append(s)
        all_items.append(s)

    return SentimentAgg(
        by_topic={t: _aggregate_sentiments(v) for t, v in topic_groups.items()},
        by_region={r: _aggregate_sentiments(v) for r, v in region_groups.items()},
        overall=_aggregate_sentiments(all_items),
    )

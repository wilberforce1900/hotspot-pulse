"""
stages/topic_graph.py — 阶段2：关联话题发现。

分工：general-reasoner（语义/共现判定）+ senior-coder（聚合与图构建实现）。
职责：
  1. 从 Post 的 tags + 正文提取/规整话题。
  2. 计算话题间关联度（共现 + 语义相似），归一化为 weight。
  3. 产出 TopicGraph（nodes + edges）。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timezone

from hotspot_pulse.models import Post, RelatedEdge, Topic, TopicGraph

logger = logging.getLogger(__name__)

_HASHTAG_PATTERN = re.compile(r"[#@](\S+)")
_LATIN_WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+\-.]{2,}")   # ≥3 字符的英文/技术词
_CJK_RUN_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,}")              # ≥2 字的连续中文串
_TOP_K = 8  # 关联话题数量上限
_INTER_TOPIC_TOP_K = 12  # 话题间共现边上限（seed 星形边之外，防大图边爆炸）

# 英文停用词（标题提取用；保持小而够用，纯标准库不引依赖）
_LATIN_STOPWORDS = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "her", "was",
    "one", "our", "out", "day", "get", "has", "him", "his", "how", "man", "new",
    "now", "old", "see", "two", "way", "who", "its", "did", "that", "this",
    "with", "have", "from", "they", "will", "would", "there", "their", "what",
    "about", "which", "when", "make", "like", "time", "just", "know", "take",
    "into", "your", "good", "some", "them", "than", "then", "over", "after",
    "before", "amid", "could", "should", "being", "under", "between", "more",
})

# 网络样板词（RSS 链接/版式常用词，对话题无信息量）
_WEB_BOILERPLATE = frozenset({
    "url", "http", "https", "www", "com", "org", "net", "html", "amp",
    "article", "feed", "rss", "link", "links", "read", "more",
})

# 中文提取参数：bigram 需出现在 ≥_CJK_MIN_POSTS 个不同帖子（语料级阈值，抗偶发噪声）
_CJK_MIN_POSTS = 3
_CJK_TOP_K = 20
_EXTRA_TAGS_PER_POST = 6   # 每帖最多追加的提取词数（防标签爆炸）


def build_topic_graph(posts: list[Post], seed_tag: str,
                      related_terms: list[str] | None = None) -> TopicGraph:
    """核心：从 Post 流构建话题网络。

    - 以 seed_tag 为主节点。
    - 话题来源优先级：post.tags 为主；tags 为空时回退提取正文 #hashtag。
    - 用户指定的 related_terms 出现过时强制纳入节点（不被 TOP_K 挤掉）。
    - 共现：post 含 seed_tag 时，其余 tag 的共现计数 +1。
    - weight = 0.7 × (cooccurrence/max_cooccurrence) + 0.3 × semantic_similarity，夹 [0,1]。
    - 共现为 0 但语义相似 > 0.35 的边也保留（语义兜底）。
    - 话题间也建共现边（真网络，非 seed 星形）：确定性取共现最强的
      _INTER_TOPIC_TOP_K 条，权重公式与 seed 边一致。
    """
    if not posts:
        return TopicGraph(nodes={}, edges=[], as_of=datetime.now(timezone.utc))

    total = len(posts)
    tag_counts: Counter[str] = Counter()
    all_tags: set[str] = set()
    for post in posts:
        for tag in _post_tags(post):
            tag_counts[tag] += 1
            all_tags.add(tag)

    nodes: dict[str, Topic] = {
        seed_tag: Topic(
            name=seed_tag,
            heat_score=float(tag_counts.get(seed_tag, 0) / total),
            post_count=tag_counts.get(seed_tag, 0),
        )
    }

    cooc: Counter[str] = Counter()
    for post in posts:
        if seed_tag not in _post_tags(post):
            continue
        for tag in _post_tags(post):
            if tag != seed_tag:
                cooc[tag] += 1

    top_topics = [t for t, _ in cooc.most_common(_TOP_K)]
    # 用户指定的关联词出现过 → 强制纳入（用户意图优先于自动排序）
    user_terms = [t for t in (related_terms or [])
                  if t and t != seed_tag and t in all_tags and t not in top_topics]
    top_topics.extend(user_terms)
    # 不足 top_k 时，按出现频次从高到低有序补足（确定性，不用 set.pop()）
    remaining = sorted((all_tags - {seed_tag}) - set(top_topics), key=lambda t: -tag_counts[t])
    top_topics.extend(remaining[:max(0, _TOP_K - len(top_topics))])

    for t in top_topics:
        if t not in nodes:
            nodes[t] = Topic(
                name=t,
                heat_score=float(tag_counts.get(t, 0) / total),
                post_count=tag_counts.get(t, 0),
            )

    max_cooc = max(cooc.values()) if cooc else 0
    edges: list[RelatedEdge] = []
    for t in top_topics:
        c = cooc.get(t, 0)
        semantic = _semantic_similarity(seed_tag, t)
        if max_cooc > 0:
            weight = 0.7 * (c / max_cooc) + 0.3 * semantic
        else:
            weight = semantic
        if c == 0 and semantic <= 0.35:
            weight = 0.0
        weight = max(0.0, min(1.0, weight))
        if weight > 0.0:
            edges.append(RelatedEdge(from_topic=seed_tag, to_topic=t, weight=weight, cooccurrence=c))

    # 话题间共现边：把话题图从 seed 星形升级为真网络（确定性：共现降序、同数按名称序）
    pair_cooc = _pairwise_cooccurrence(posts, set(nodes) - {seed_tag})
    if pair_cooc:
        max_pair = max(pair_cooc.values())
        ranked = sorted(pair_cooc.items(), key=lambda kv: (-kv[1], kv[0]))
        for (a, b), c in ranked[:_INTER_TOPIC_TOP_K]:
            weight = max(0.0, min(1.0, 0.7 * (c / max_pair)
                                  + 0.3 * _semantic_similarity(a, b)))
            if weight > 0.0:
                edges.append(RelatedEdge(from_topic=a, to_topic=b, weight=weight, cooccurrence=c))

    logger.info("话题图构建完成：%d 节点 / %d 边（seed=%s）", len(nodes), len(edges), seed_tag)
    return TopicGraph(nodes=nodes, edges=edges, as_of=datetime.now(timezone.utc))


def _pairwise_cooccurrence(posts: list[Post],
                           keep: set[str]) -> Counter:
    """统计 keep 内话题两两共现次数（同一 post 的 tags 视为全部共现）。

    返回键为排序后的 (a, b) 元组（a < b），保证无向边不重复计数。
    """
    counter: Counter = Counter()
    for post in posts:
        tags = sorted(set(_post_tags(post)) & keep)
        for i in range(len(tags)):
            for j in range(i + 1, len(tags)):
                counter[(tags[i], tags[j])] += 1
    return counter


# --------------------------------------------------------------------------- #
# 话题词提取：让真实源（RSS/GDELT，原生只有查询 tag）的话题图不再孤点
# --------------------------------------------------------------------------- #
def _extract_hashtags(text: str) -> list[str]:
    """从正文提取 #话题 / @提及（去符号，保留原文大小写）。"""
    return [m.group(1).lstrip("#@") for m in _HASHTAG_PATTERN.finditer(text or "")
            if len(m.group(1)) >= 2]


def _extract_latin_words(text: str) -> list[str]:
    """提取 ≥3 字符的英文/技术词（小写化、去停用词、去重保序）。"""
    seen: set[str] = set()
    words: list[str] = []
    for m in _LATIN_WORD_PATTERN.finditer(text or ""):
        w = m.group(0).lower().rstrip(".-")
        if (len(w) >= 3 and w not in _LATIN_STOPWORDS
                and w not in _WEB_BOILERPLATE and w not in seen):
            seen.add(w)
            words.append(w)
    return words


def _cjk_bigram_topics(posts: list[Post]) -> list[str]:
    """语料级中文 bigram 话题：出现在 ≥3 个帖子的字符 bigram，取频次前 K。

    纯标准库做不了真分词；语料级阈值（跨帖子复现）是噪声与可读性的折中——
    单帖偶发搭配进不来，多帖反复出现的二字词大概率是实义词。
    """
    post_bigrams: list[set[str]] = []
    for post in posts:
        grams: set[str] = set()
        for run in _CJK_RUN_PATTERN.findall(post.text or ""):
            grams.update(run[i:i + 2] for i in range(len(run) - 1))
        post_bigrams.append(grams)
    counter: Counter = Counter()
    for grams in post_bigrams:
        counter.update(grams)
    qualified = [(g, c) for g, c in counter.items() if c >= _CJK_MIN_POSTS]
    qualified.sort(key=lambda kv: (-kv[1], kv[0]))   # 确定性排序
    return [g for g, _ in qualified[:_CJK_TOP_K]]


def enrich_posts(posts: list[Post], seed_tag: str) -> list[Post]:
    """给真实源的帖子补提取话题词（浅拷贝，不改原对象）。

    提取三路：#hashtag（正文）、英文词（去停用词）、中文 bigram（语料级阈值）。
    每帖追加 ≤6 个、排除 seed 与已有 tag。仅 orchestrator 对 RSS/GDELT 源调用
    ——mock 源自带丰富关联 tag，再叠加提取只会引入模板噪声。
    """
    if not posts:
        return posts
    cjk_topics = set(_cjk_bigram_topics(posts))

    enriched: list[Post] = []
    for post in posts:
        candidates = _extract_post_topics(post, seed_tag, cjk_topics)
        if candidates:
            enriched.append(Post(
                external_id=post.external_id, source=post.source,
                created_at=post.created_at, author=post.author,
                text=post.text, images=list(post.images),
                tags=post.tags + candidates, region=post.region, raw=post.raw,
            ))
        else:
            enriched.append(post)
    logger.info("话题词提取：CJK bigram 候选 %d 个，增强完成", len(cjk_topics))
    return enriched


def _extract_post_topics(post: Post, seed_tag: str,
                         cjk_topics: set[str]) -> list[str]:
    """单帖提取：hashtag → 英文词 → 命中的语料级 bigram；去重、排除已有，截上限。"""
    existing = set(post.tags)
    picked: list[str] = []
    for term in (_extract_hashtags(post.text)
                 + _extract_latin_words(post.text)
                 + [g for g in cjk_topics if g in (post.text or "")]):
        t = term.strip()
        if (t and t != seed_tag and t not in existing and t not in picked
                and len(picked) < _EXTRA_TAGS_PER_POST):
            picked.append(t)
    return picked


def _post_tags(post: Post) -> list[str]:
    """单条 Post 的话题词：优先 post.tags，为空时回退提取正文 #hashtag/@提及。"""
    if post.tags:
        return post.tags
    return [m.group(1).lstrip("#@") for m in _HASHTAG_PATTERN.finditer(post.text or "")]


def _semantic_similarity(a: str, b: str) -> float:
    """轻量语义相似度：2-字符 bigram 集合的 Jaccard（对 CJK 友好，纯标准库）。"""
    if not a or not b:
        return 0.0

    def bigrams(s: str) -> set[str]:
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set(s)

    a_set, b_set = bigrams(a), bigrams(b)
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / len(a_set | b_set)

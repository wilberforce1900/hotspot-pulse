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
_TOP_K = 8  # 关联话题数量上限
_INTER_TOPIC_TOP_K = 12  # 话题间共现边上限（seed 星形边之外，防大图边爆炸）


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

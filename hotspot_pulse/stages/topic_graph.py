"""
stages/topic_graph.py — 阶段2：关联话题发现。

分工：general-reasoner（语义/共现判定）+ senior-coder（聚合与图构建实现）。
职责：
  1. 从 Post 的 tags + 正文提取/规整话题。
  2. 计算话题间关联度（共现 + 语义相似），归一化为 weight。
  3. 产出 TopicGraph（nodes + edges）。
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

from hotspot_pulse.models import Post, RelatedEdge, Topic, TopicGraph

_HASHTAG_PATTERN = re.compile(r"[#@](\S+)")
_TOP_K = 8  # 关联话题数量上限


def build_topic_graph(posts: list[Post], seed_tag: str) -> TopicGraph:
    """核心：从 Post 流构建话题网络。

    - 以 seed_tag 为主节点。
    - 共现：post.tags 含 seed_tag 时，其余 tag 的共现计数 +1。
    - weight = 0.7 × (cooccurrence/max_cooccurrence) + 0.3 × semantic_similarity，夹 [0,1]。
    - 共现为 0 但语义相似 > 0.35 的边也保留（语义兜底）。
    """
    if not posts:
        return TopicGraph(nodes={}, edges=[], as_of=datetime.now(timezone.utc))

    total = len(posts)
    tag_counts: Counter[str] = Counter()
    all_tags: set[str] = set()
    for post in posts:
        for tag in post.tags:
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
        if seed_tag not in post.tags:
            continue
        for tag in post.tags:
            if tag != seed_tag:
                cooc[tag] += 1

    top_topics = [t for t, _ in cooc.most_common(_TOP_K)]
    # 不足 top_k 时，按出现频次从高到低有序补足（确定性，不用 set.pop()）
    remaining = sorted((all_tags - {seed_tag}) - set(top_topics), key=lambda t: -tag_counts[t])
    top_topics.extend(remaining[:_TOP_K - len(top_topics)])

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

    return TopicGraph(nodes=nodes, edges=edges, as_of=datetime.now(timezone.utc))


def _extract_tags(posts: list[Post]) -> list[str]:
    """从 Post.tags 与正文规整话题名（去 #/@、Latin 小写、按频次降序去重）。"""
    counter: Counter[str] = Counter()
    for post in posts:
        tags = post.tags or [m.group(1) for m in _HASHTAG_PATTERN.finditer(post.text)]
        for tag in tags:
            tag = tag.strip().lstrip("#@").lower()
            if tag:
                counter[tag] += 1
    return [t for t, _ in counter.most_common()]


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

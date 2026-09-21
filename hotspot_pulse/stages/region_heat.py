"""
stages/region_heat.py — 阶段5：热度区域分布。

分工：general-reasoner（地域推断/归类口径）+ senior-coder（聚合与图构建实现）。
职责：
  1. 为每条 Post 补全/规整 region（region_mapper 策略）。
  2. 按区域聚合热度（热度分 = 量归一化）。
  3. 构建 RegionGraph（节点=区域，边=跨区传播强度，供热力网图用）。

V4 Pro 审定修复：
  - _region_edges 必须使用与节点一致的「推断后 region」，避免节点/边对不上。
  - 兜底 hash 用稳定哈希（跨进程可复现）。
  - 边权按 max 归一（最强边=1.0），利于可视化区分。
"""

from __future__ import annotations

from collections import Counter, defaultdict

from hotspot_pulse.config import RegionMapperConfig
from hotspot_pulse.models import Post, RegionEdge, RegionGraph, RegionNode
from hotspot_pulse.shared import DEFAULT_REGIONS as _DEFAULT_POOL
from hotspot_pulse.shared import stable_hash as _stable_hash


def build_region_graph(posts: list[Post], cfg: RegionMapperConfig) -> RegionGraph:
    """核心：构建区域热度网络（节点=区域，边=跨区传播）。"""
    if not posts:
        return RegionGraph()

    # 1) 推断 region（与节点、边共用同一份结果，保证一致）
    post_regions: list[tuple[Post, str]] = []
    for post in posts:
        r = _infer_region(post, cfg)
        post_regions.append((post, r if r and r.strip() else "unknown"))

    total = len(posts)
    region_posts: dict[str, list[Post]] = defaultdict(list)
    region_topics: dict[str, Counter] = defaultdict(Counter)
    for post, r in post_regions:
        region_posts[r].append(post)
        for tag in post.tags:
            region_topics[r][tag] += 1

    nodes: dict[str, RegionNode] = {}
    for r, plist in region_posts.items():
        nodes[r] = RegionNode(
            region=r,
            heat_score=len(plist) / total,
            post_count=len(plist),
            top_topics=[t for t, _ in region_topics[r].most_common(3)],
        )

    return RegionGraph(nodes=nodes, edges=_region_edges(nodes, post_regions))


def _infer_region(post: Post, cfg: RegionMapperConfig) -> str:
    """从 Post 推断区域（策略：field / mock / geoip）。"""
    mode = cfg.mode
    if mode == "field":
        return post.region or "unknown"
    if mode == "mock":
        if post.region:
            return post.region
        return _DEFAULT_POOL[_stable_hash(post.external_id) % len(_DEFAULT_POOL)]
    # geoip 及其它未知 mode：离线无数据库 → unknown
    return "unknown"


def _region_edges(nodes: dict[str, RegionNode],
                  post_regions: list[tuple[Post, str]]) -> list[RegionEdge]:
    """跨区传播边：同一 topic 在 ≥2 区域出现 → 两两建边，权重按 max 归一。"""
    if not post_regions:
        return []

    topic_regions: dict[str, set[str]] = defaultdict(set)
    topic_region_count: Counter[tuple[str, str]] = Counter()
    for post, r in post_regions:
        for tag in post.tags:
            topic_regions[tag].add(r)
            topic_region_count[(tag, r)] += 1

    cross_topics = [t for t, rs in topic_regions.items() if len(rs) >= 2]
    if not cross_topics:
        return []

    raw: dict[tuple[str, str], float] = defaultdict(float)
    for t in cross_topics:
        rs = sorted(topic_regions[t])
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a, b = rs[i], rs[j]
                raw[(a, b)] += topic_region_count[(t, a)] + topic_region_count[(t, b)]

    if not raw:
        return []
    max_w = max(raw.values())
    return [RegionEdge(from_region=a, to_region=b, weight=w / max_w)
            for (a, b), w in raw.items()]

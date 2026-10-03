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
from datetime import datetime

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
    """跨区传播边：同一 topic 在 ≥2 区域出现 → 两两建边，权重按 max 归一。

    方向 = 传播语义：对每对区域取「各话题上更早出现的一侧」投票，得票多者为
    from（先爆发），平票按区域名字典序（确定性）。from → to 读作「先 → 后」。
    """
    if not post_regions:
        return []

    topic_regions: dict[str, set[str]] = defaultdict(set)
    topic_region_count: Counter[tuple[str, str]] = Counter()
    topic_region_first_ts: dict[tuple[str, str], datetime] = {}
    for post, r in post_regions:
        for tag in post.tags:
            topic_regions[tag].add(r)
            topic_region_count[(tag, r)] += 1
            key = (tag, r)
            if key not in topic_region_first_ts or post.created_at < topic_region_first_ts[key]:
                topic_region_first_ts[key] = post.created_at

    cross_topics = [t for t, rs in topic_regions.items() if len(rs) >= 2]
    if not cross_topics:
        return []

    raw: dict[tuple[str, str], float] = defaultdict(float)
    lead_votes: Counter[tuple[str, str]] = Counter()   # (x, y) → x 领先票数
    for t in cross_topics:
        rs = sorted(topic_regions[t])
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a, b = rs[i], rs[j]
                raw[(a, b)] += topic_region_count[(t, a)] + topic_region_count[(t, b)]
                ta = topic_region_first_ts.get((t, a))
                tb = topic_region_first_ts.get((t, b))
                if ta is not None and tb is not None:
                    if ta < tb:
                        lead_votes[(a, b)] += 1
                    elif tb < ta:
                        lead_votes[(b, a)] += 1
                # 时间并列（或缺失）不投票

    if not raw:
        return []
    max_w = max(raw.values())
    edges: list[RegionEdge] = []
    for (a, b), w in raw.items():
        # 先行方为 from：a 领先票多 → (a,b)；b 多或平票按字典序 a<b（键本身有序）
        if lead_votes[(b, a)] > lead_votes[(a, b)]:
            src, dst = b, a
        else:
            src, dst = a, b
        edges.append(RegionEdge(from_region=src, to_region=dst, weight=w / max_w))
    edges.sort(key=lambda e: (e.from_region, e.to_region))
    return edges

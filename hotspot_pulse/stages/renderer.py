"""
stages/renderer.py — 阶段6：热力网图渲染。

分工：visual-analyst（图表/网图可视化专长）出草稿，**V4 Pro 审定落地**。
职责：
  1. 把 TopicGraph 与 RegionGraph 转成可视化网图。
  2. 节点大小/颜色按热度编码，边宽按关联/传播强度。
  3. 输出 SVG + 结构化 HotspotGraph（供报告引用）。

说明：环境无 matplotlib/networkx → 手写 SVG（纯标准库）+ 圆形布局降级。
      （visual-analyst 草稿把 dict 契约当成 list 且无法写文件，已由 V4 Pro 驳回重写。）
"""

from __future__ import annotations

import math
import os
from xml.sax.saxutils import escape

from hotspot_pulse.config import RenderConfig
from hotspot_pulse.models import HotspotGraph, LayoutHint, RegionGraph, TopicGraph

# YlOrRd 三锚点：0.0 黄 → 0.5 橙 → 1.0 红
_ANCHORS = [(0.0, (255, 255, 178)), (0.5, (253, 141, 60)), (1.0, (189, 0, 38))]


def _color_for_heat(heat: float, cmap: str = "YlOrRd") -> tuple:
    """热度(0~1)→RGB 三元组。仅实现 YlOrRd 渐变，其余 cmap 退回同一套。"""
    heat = max(0.0, min(1.0, float(heat)))
    if heat <= 0.0:
        return _ANCHORS[0][1]
    if heat >= 1.0:
        return _ANCHORS[-1][1]
    lo, hi = (_ANCHORS[0], _ANCHORS[1]) if heat <= 0.5 else (_ANCHORS[1], _ANCHORS[2])
    t = (heat - lo[0]) / (hi[0] - lo[0])
    return tuple(int(round(lo[1][k] + (hi[1][k] - lo[1][k]) * t)) for k in range(3))


def _size_for_heat(heat: float) -> float:
    """热度→节点半径：8 ~ 30。"""
    return 8.0 + max(0.0, min(1.0, float(heat))) * 22.0


def _hex(color: tuple) -> str:
    return "#%02x%02x%02x" % color


def render_network(region: RegionGraph, topics: TopicGraph, cfg: RenderConfig) -> HotspotGraph:
    """渲染热力网图主入口（圆形布局，手写 SVG）。

    - 区域节点=实心圆（fill 热度色）；话题节点=空心圆（stroke 热度色）以区分。
    - 边宽 = 1 + weight*4；先画边再画节点，避免遮挡。
    """
    os.makedirs(cfg.output_dir, exist_ok=True)
    combine = getattr(cfg, "combine_topic_region", True)

    # ---- 收集节点（key = (kind, name) 避免区域/话题同名冲突）----
    node_items: list[tuple[str, str, float, int]] = []  # (kind, name, heat, post_count)
    for name, node in (region.nodes or {}).items():
        node_items.append(("region", name, node.heat_score, node.post_count))
    if combine:
        for name, node in (topics.nodes or {}).items():
            node_items.append(("topic", name, node.heat_score, node.post_count))

    n = len(node_items)
    width = int(cfg.width * 96)
    height = int(cfg.height * 96)
    cx, cy = width / 2.0, height / 2.0
    radius = min(width, height) * 0.38

    coords: dict[tuple[str, str], tuple[float, float]] = {}
    if n > 0:
        for i, (kind, name, _h, _c) in enumerate(node_items):
            ang = 2 * math.pi * i / n - math.pi / 2
            coords[(kind, name)] = (cx + radius * math.cos(ang), cy + radius * math.sin(ang))

    # ---- 组装 SVG ----
    parts: list[str] = []
    parts.append(f'<svg width="{width}" height="{height}" '
                 f'viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">')
    parts.append('<rect width="100%" height="100%" fill="#ffffff"/>')
    parts.append('<text x="12" y="20" font-family="sans-serif" font-size="14" fill="#333">'
                 f'Hotspot · 实心=区域 / 空心=话题</text>')

    # 边（先画）
    def draw_edge(kind: str, src: str, dst: str, weight: float) -> None:
        a = coords.get((kind, src))
        b = coords.get((kind, dst))
        if a is None or b is None or src == dst:
            return
        w = 1.0 + max(0.0, min(1.0, weight)) * 4.0
        parts.append(f'<line x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}" '
                     f'stroke="#9e9e9e" stroke-width="{w:.1f}" stroke-opacity="0.7"/>')

    for e in (region.edges or []):
        draw_edge("region", e.from_region, e.to_region, e.weight)
    if combine:
        for e in (topics.edges or []):
            draw_edge("topic", e.from_topic, e.to_topic, e.weight)

    # 节点
    nodes_data: list[dict] = []
    edges_data: list[dict] = []
    for kind, name, heat, post_count in node_items:
        x, y = coords[(kind, name)]
        size = _size_for_heat(heat)
        color = _hex(_color_for_heat(heat, cfg.color_map))
        label = escape(name)
        if kind == "region":
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{size:.1f}" '
                         f'fill="{color}" stroke="#5b5b5b" stroke-width="1"/>')
        else:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{size:.1f}" '
                         f'fill="#ffffff" stroke="{color}" stroke-width="2.5"/>')
        parts.append(f'<text x="{x:.1f}" y="{y + size + 13:.1f}" text-anchor="middle" '
                     f'font-family="sans-serif" font-size="12" fill="#222">{label}</text>')
        nodes_data.append({
            "name": name, "kind": kind, "heat": round(heat, 4),
            "size": round(size, 2), "color": color, "post_count": post_count,
        })

    for e in (region.edges or []):
        edges_data.append({"kind": "region", "source": e.from_region,
                           "target": e.to_region, "weight": round(e.weight, 4)})
    if combine:
        for e in (topics.edges or []):
            edges_data.append({"kind": "topic", "source": e.from_topic,
                               "target": e.to_topic, "weight": round(e.weight, 4)})

    if n == 0:
        parts.append('<text x="50%" y="50%" text-anchor="middle" font-family="sans-serif" '
                     'font-size="16" fill="#888">暂无数据</text>')
    parts.append('</svg>')

    path = os.path.join(cfg.output_dir, "hotspot.svg")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))

    layout = LayoutHint(algorithm=cfg.layout, width=cfg.width, height=cfg.height,
                        color_map=cfg.color_map, node_size_by="heat")
    return HotspotGraph(
        graph_path=path,
        nodes=nodes_data,
        edges=edges_data,
        layout=layout,
        combined_topics=combine,
    )

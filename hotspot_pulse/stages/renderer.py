"""
stages/renderer.py — 阶段6：热力网图渲染。

分工：visual-analyst（图表/网图可视化专长）出草稿，**V4 Pro 审定落地**。
职责：
  1. 把 TopicGraph 与 RegionGraph 转成可视化网图。
  2. 力导向布局（Fruchterman–Reingold 简化版，纯标准库、确定性、无随机）：
     节点位置由斥力/边引力/同类弱聚簇/向心力迭代平衡，结构相关而非装饰。
  3. 节点大小/颜色按热度编码，边宽按关联/传播强度；输出 SVG + 结构化
     HotspotGraph（nodes 含 x/y 坐标，供下游交互/存档复用）。

说明：networkx/graphviz 布局为设计选项、未接（避免第三方依赖）；
     区域节点初始置于左半圆、话题置于右半圆，配合同类聚簇力自然分成两簇。
"""

from __future__ import annotations

import logging
import math
import os
from xml.sax.saxutils import escape

from hotspot_pulse.config import RenderConfig
from hotspot_pulse.models import HotspotGraph, LayoutHint, RegionGraph, TopicGraph

logger = logging.getLogger(__name__)

# YlOrRd 三锚点：0.0 黄 → 0.5 橙 → 1.0 红
_ANCHORS = [(0.0, (255, 255, 178)), (0.5, (253, 141, 60)), (1.0, (189, 0, 38))]

_LAYOUT_ITERATIONS = 200
_MARGIN = 60.0          # 节点离画布边缘最小距离
_SAME_KIND_PULL = 0.04  # 同类节点向本簇质心的聚簇强度
_CENTER_PULL = 0.02     # 全局向心强度


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


def _force_layout(node_items: list[tuple[str, str, float, int]],
                  edges: list[tuple[str, str, str, float]],
                  width: int, height: int) -> dict[tuple[str, str], tuple[float, float]]:
    """确定性力导向布局。

    node_items: (kind, name, heat, post_count)；edges: (kind, src, dst, weight)。
    返回 (kind, name) -> (x, y)。无随机数：初始角度按组内索引均分。
    """
    n = len(node_items)
    keys = [(kind, name) for kind, name, _h, _c in node_items]
    if n == 0:
        return {}
    cx, cy = width / 2.0, height / 2.0
    if n == 1:
        return {keys[0]: (cx, cy)}
    radius = min(width, height) * 0.30

    # 初始：区域左半圆 / 话题右半圆（起点分组，后续被力微调）
    pos: dict[tuple[str, str], tuple[float, float]] = {}
    for group_kind, phase0, phase1 in (("region", math.pi / 2, 3 * math.pi / 2),
                                       ("topic", -math.pi / 2, math.pi / 2)):
        group = [k for k in keys if k[0] == group_kind]
        for i, key in enumerate(group):
            ang = phase0 + (phase1 - phase0) * (i / max(1, len(group)))
            pos[key] = (cx + radius * math.cos(ang), cy + radius * math.sin(ang))

    k = 0.8 * math.sqrt((width * height) / n)   # 理想边长
    edge_pairs = [(g, src, dst, max(0.05, w)) for g, src, dst, w in edges
                  if (g, src) in pos and (g, dst) in pos and src != dst]

    for it in range(_LAYOUT_ITERATIONS):
        temp = (width / 10.0) * (1.0 - it / _LAYOUT_ITERATIONS)
        disp: dict[tuple[str, str], list[float]] = {key: [0.0, 0.0] for key in keys}

        # 斥力：所有节点对（d 夹下限防除零）
        for i in range(n):
            for j in range(i + 1, n):
                a, b = keys[i], keys[j]
                dx = pos[a][0] - pos[b][0]
                dy = pos[a][1] - pos[b][1]
                d = max(1.0, math.hypot(dx, dy))
                f = (k * k) / d
                ux, uy = dx / d, dy / d
                disp[a][0] += ux * f
                disp[a][1] += uy * f
                disp[b][0] -= ux * f
                disp[b][1] -= uy * f

        # 引力：沿边（权重越强拉越紧）
        for g, src, dst, w in edge_pairs:
            dx = pos[(g, dst)][0] - pos[(g, src)][0]
            dy = pos[(g, dst)][1] - pos[(g, src)][1]
            d = max(1.0, math.hypot(dx, dy))
            f = (d * d) / k * w
            ux, uy = dx / d, dy / d
            disp[(g, src)][0] += ux * f
            disp[(g, src)][1] += uy * f
            disp[(g, dst)][0] -= ux * f
            disp[(g, dst)][1] -= uy * f

        # 同类聚簇 + 全局向心
        centroids: dict[str, tuple[float, float]] = {}
        for kind in ("region", "topic"):
            members = [pos[key] for key in keys if key[0] == kind]
            if members:
                centroids[kind] = (sum(p[0] for p in members) / len(members),
                                   sum(p[1] for p in members) / len(members))
        for key in keys:
            gx, gy = disp[key]
            gx += (cx - pos[key][0]) * _CENTER_PULL
            gy += (cy - pos[key][1]) * _CENTER_PULL
            if key[0] in centroids:
                gx += (centroids[key[0]][0] - pos[key][0]) * _SAME_KIND_PULL
                gy += (centroids[key[0]][1] - pos[key][1]) * _SAME_KIND_PULL
            disp[key] = [gx, gy]

        # 位移夹到温度内 + 边界夹紧
        for key in keys:
            dx, dy = disp[key]
            d = max(1.0, math.hypot(dx, dy))
            step = min(d, temp)
            nx = min(max(pos[key][0] + dx / d * step, _MARGIN), width - _MARGIN)
            ny = min(max(pos[key][1] + dy / d * step, _MARGIN), height - _MARGIN)
            pos[key] = (nx, ny)

    # 终位归一：整体等比缩放进画布中央 ~90% 区域（斥力强的稀疏图不再贴边、留白均衡）
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    span_x = max(1.0, max(xs) - min(xs))
    span_y = max(1.0, max(ys) - min(ys))
    target_w = (width - 2 * _MARGIN) * 0.9
    target_h = (height - 2 * _MARGIN) * 0.9
    scale = min(target_w / span_x, target_h / span_y)
    mid_x, mid_y = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    for key in keys:
        px, py = pos[key]
        pos[key] = (cx + (px - mid_x) * scale, cy + (py - mid_y) * scale)

    # 最小间距：强连通小簇内节点可能被边引力压到重叠，两两推开（确定性）
    sizes = {(kind, name): _size_for_heat(heat) for kind, name, heat, _c in node_items}
    for _ in range(40):
        moved = False
        for i in range(n):
            for j in range(i + 1, n):
                a, b = keys[i], keys[j]
                dx = pos[a][0] - pos[b][0]
                dy = pos[a][1] - pos[b][1]
                d = math.hypot(dx, dy)
                min_d = sizes[a] + sizes[b] + 12.0
                if d >= min_d:
                    continue
                if d < 1e-6:  # 完全重合：按索引方向确定性错开
                    dx, dy, d = 1.0, 1.0, math.sqrt(2.0)
                push = (min_d - d) / 2.0
                ux, uy = dx / d, dy / d
                pos[a] = (min(max(pos[a][0] + ux * push, _MARGIN), width - _MARGIN),
                          min(max(pos[a][1] + uy * push, _MARGIN), height - _MARGIN))
                pos[b] = (min(max(pos[b][0] - ux * push, _MARGIN), width - _MARGIN),
                          min(max(pos[b][1] - uy * push, _MARGIN), height - _MARGIN))
                moved = True
        if not moved:
            break

    return pos


def render_network(region: RegionGraph, topics: TopicGraph, cfg: RenderConfig) -> HotspotGraph:
    """渲染热力网图主入口（力导向布局，手写 SVG）。

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
        for t_name, t_node in (topics.nodes or {}).items():
            node_items.append(("topic", t_name, t_node.heat_score, t_node.post_count))

    edge_items: list[tuple[str, str, str, float]] = []
    for e in (region.edges or []):
        edge_items.append(("region", e.from_region, e.to_region, e.weight))
    if combine:
        for te in (topics.edges or []):
            edge_items.append(("topic", te.from_topic, te.to_topic, te.weight))

    width = int(cfg.width * 96)
    height = int(cfg.height * 96)
    coords = _force_layout(node_items, edge_items, width, height)

    # ---- 组装 SVG ----
    parts: list[str] = []
    parts.append(f'<svg width="{width}" height="{height}" '
                 f'viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">')
    parts.append('<rect width="100%" height="100%" fill="#ffffff"/>')
    parts.append('<text x="12" y="20" font-family="sans-serif" font-size="14" fill="#333">'
                 'Hotspot · 实心=区域 / 空心=话题</text>')

    # 边（先画）
    def draw_edge(kind: str, src: str, dst: str, weight: float) -> None:
        a = coords.get((kind, src))
        b = coords.get((kind, dst))
        if a is None or b is None or src == dst:
            return
        w = 1.0 + max(0.0, min(1.0, weight)) * 4.0
        parts.append(f'<line x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}" '
                     f'stroke="#9e9e9e" stroke-width="{w:.1f}" stroke-opacity="0.7"/>')

    for g, src, dst, w in edge_items:
        draw_edge(g, src, dst, w)

    # 分组质心标注（置于簇上方，避开节点与标签）
    node_sizes = {(kind, name): _size_for_heat(heat) for kind, name, heat, _c in node_items}
    for kind, caption in (("region", "区域"), ("topic", "话题")):
        members = [(g, n) for (g, n) in coords if g == kind]
        if members:
            mx = sum(coords[k][0] for k in members) / len(members)
            top = min(coords[k][1] - node_sizes[k] for k in members)
            parts.append(f'<text x="{mx:.1f}" y="{top - 8:.1f}" text-anchor="middle" '
                         f'font-family="sans-serif" font-size="15" fill="#777">{caption}</text>')

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
            "x": round(x, 1), "y": round(y, 1),
        })

    for g, src, dst, w in edge_items:
        edges_data.append({"kind": g, "source": src, "target": dst, "weight": round(w, 4)})

    if not node_items:
        parts.append('<text x="50%" y="50%" text-anchor="middle" font-family="sans-serif" '
                     'font-size="16" fill="#888">暂无数据</text>')
    parts.append('</svg>')

    path = os.path.join(cfg.output_dir, "hotspot.svg")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))

    layout = LayoutHint(algorithm=cfg.layout, width=cfg.width, height=cfg.height,
                        color_map=cfg.color_map, node_size_by="heat")
    logger.info("网图已渲染：%d 节点 / %d 边 → %s", len(nodes_data), len(edges_data), path)
    return HotspotGraph(
        graph_path=path,
        nodes=nodes_data,
        edges=edges_data,
        layout=layout,
        combined_topics=combine,
    )

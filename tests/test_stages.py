"""tests/test_stages.py — 阶段2/3/5/6：话题图、情绪、区域热度、渲染。"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from hotspot_pulse.config import RegionMapperConfig, RenderConfig
from hotspot_pulse.models import DataSource, Post
from hotspot_pulse.stages.region_heat import build_region_graph
from hotspot_pulse.stages.renderer import render_network
from hotspot_pulse.stages.sentiment import _classify_text, analyze_sentiment
from hotspot_pulse.stages.topic_graph import build_topic_graph

T0 = datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc)


def make_post(i: int, tags: list[str], region: str | None = None, text: str = "报道") -> Post:
    return Post(
        external_id=f"p{i}",
        source=DataSource.MOCK,
        created_at=T0 + timedelta(hours=i),
        text=text,
        tags=tags,
        region=region,
    )


class TestTopicGraph(unittest.TestCase):
    def test_seed_node_and_cooccurrence_edges(self):
        posts = [make_post(i, ["AI芯片", "股价", "出口"]) for i in range(5)]
        g = build_topic_graph(posts, "AI芯片")
        self.assertIn("AI芯片", g.nodes)
        self.assertEqual(g.nodes["AI芯片"].post_count, 5)
        edge_targets = {e.to_topic for e in g.edges}
        self.assertIn("股价", edge_targets)
        self.assertIn("出口", edge_targets)
        for e in g.edges:
            self.assertEqual(e.from_topic, "AI芯片")
            self.assertGreater(e.weight, 0.0)
            self.assertLessEqual(e.weight, 1.0)

    def test_empty_posts(self):
        g = build_topic_graph([], "AI芯片")
        self.assertEqual(g.nodes, {})
        self.assertEqual(g.edges, [])

    def test_related_terms_forced_included(self):
        # 10 个共现=2 的杂词挤满 TOP_K=8；共现=1 的「出口」正常情况下进不来
        posts = []
        for j in range(10):
            posts.append(make_post(2 * j, ["AI芯片", f"杂{j}"]))
            posts.append(make_post(2 * j + 1, ["AI芯片", f"杂{j}"]))
        posts.append(make_post(99, ["AI芯片", "出口"]))

        g = build_topic_graph(posts, "AI芯片")
        self.assertNotIn("出口", g.nodes)  # 未指定 → 被 TOP_K 挤掉

        g2 = build_topic_graph(posts, "AI芯片", related_terms=["出口"])
        self.assertIn("出口", g2.nodes)     # 用户指定 → 强制纳入
        edge = next(e for e in g2.edges if e.to_topic == "出口")
        self.assertEqual(edge.cooccurrence, 1)

    def test_hashtag_fallback_from_text(self):
        # post.tags 为空时，从正文 #hashtag 提取话题（_post_tags 回退路径）
        posts = [
            make_post(0, [], text="#AI芯片 看涨 #股价"),
            make_post(1, [], text="#AI芯片 持续讨论"),
        ]
        g = build_topic_graph(posts, "AI芯片")
        self.assertEqual(g.nodes["AI芯片"].post_count, 2)
        self.assertIn("股价", g.nodes)

    def test_semantic_edge_without_cooccurrence(self):
        # 「AI芯片」与「AI芯片产业」共现为 0，但 bigram 高度重叠 → 语义兜底边
        posts = [make_post(0, ["AI芯片"]), make_post(1, ["AI芯片产业"])]
        g = build_topic_graph(posts, "AI芯片")
        edge = next((e for e in g.edges if e.to_topic == "AI芯片产业"), None)
        self.assertIsNotNone(edge)
        self.assertEqual(edge.cooccurrence, 0)
        self.assertGreater(edge.weight, 0.35)


class TestSentiment(unittest.TestCase):
    def test_positive_text(self):
        s = _classify_text("AI芯片利好，股价上涨，市场惊喜")
        self.assertEqual(s.label.value, "positive")
        self.assertGreater(s.positive, s.negative)

    def test_negative_text(self):
        s = _classify_text("利空消息，风险加剧，恐慌情绪蔓延")
        self.assertEqual(s.label.value, "negative")
        self.assertGreater(s.negative, s.positive)

    def test_mixed_text(self):
        s = _classify_text("利好与风险并存")
        self.assertEqual(s.label.value, "mixed")

    def test_neutral_text(self):
        s = _classify_text("关于该话题的一般报道")
        self.assertEqual(s.label.value, "neutral")
        self.assertAlmostEqual(s.positive + s.negative + s.neutral, 1.0)

    def test_aggregate_by_topic_and_region(self):
        posts = [
            make_post(0, ["A"], region="中国", text="利好"),
            make_post(1, ["A"], region="美国", text="利好"),
            make_post(2, ["B"], region="中国", text="利空"),
        ]
        agg = analyze_sentiment(posts, include_images=False)
        self.assertEqual(set(agg.by_topic), {"A", "B"})
        self.assertEqual(set(agg.by_region), {"中国", "美国"})
        self.assertIsNotNone(agg.overall)
        self.assertEqual(agg.by_topic["A"].label.value, "positive")
        self.assertEqual(agg.by_topic["B"].label.value, "negative")

    def test_post_without_tags_falls_back_to_unknown(self):
        agg = analyze_sentiment([make_post(0, [], text="利好")], include_images=False)
        self.assertIn("unknown", agg.by_topic)


class TestRegionHeat(unittest.TestCase):
    def test_nodes_aggregated_by_region(self):
        posts = [make_post(i, ["AI芯片"], region="中国") for i in range(3)]
        posts += [make_post(10 + i, ["AI芯片"], region="美国") for i in range(1)]
        g = build_region_graph(posts, RegionMapperConfig(mode="field"))
        self.assertEqual(set(g.nodes), {"中国", "美国"})
        self.assertEqual(g.nodes["中国"].post_count, 3)
        self.assertAlmostEqual(g.nodes["中国"].heat_score, 0.75)
        self.assertEqual(g.nodes["中国"].top_topics, ["AI芯片"])

    def test_cross_region_edges_for_shared_topic(self):
        posts = [make_post(0, ["T"], region="中国"), make_post(1, ["T"], region="美国")]
        g = build_region_graph(posts, RegionMapperConfig(mode="field"))
        pairs = {(e.from_region, e.to_region) for e in g.edges}
        self.assertIn(("中国", "美国"), pairs)

    def test_no_edges_when_topics_are_single_region(self):
        posts = [make_post(0, ["T1"], region="中国"), make_post(1, ["T2"], region="美国")]
        g = build_region_graph(posts, RegionMapperConfig(mode="field"))
        self.assertEqual(g.edges, [])

    def test_mock_mode_fills_unknown_region(self):
        post = make_post(0, ["T"], region=None)
        g = build_region_graph([post], RegionMapperConfig(mode="mock"))
        self.assertEqual(len(g.nodes), 1)
        self.assertNotEqual(list(g.nodes)[0], "unknown")


class TestRenderer(unittest.TestCase):
    def test_renders_svg_file_with_nodes(self):
        posts = [make_post(i, ["AI芯片", "股价"], region="中国") for i in range(3)]
        posts += [make_post(10, ["AI芯片"], region="美国")]
        region_g = build_region_graph(posts, RegionMapperConfig(mode="field"))
        topic_g = build_topic_graph(posts, "AI芯片")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = RenderConfig(output_dir=tmp)
            graph = render_network(region_g, topic_g, cfg)
            self.assertTrue(os.path.exists(graph.graph_path))
            self.assertTrue(graph.graph_path.endswith("hotspot.svg"))
            with open(graph.graph_path, encoding="utf-8") as fh:
                svg = fh.read()
            self.assertIn("<svg", svg)
            self.assertEqual(svg.count("<circle"), len(graph.nodes))
            kinds = {n["kind"] for n in graph.nodes}
            self.assertEqual(kinds, {"region", "topic"})

    def test_empty_graph_renders_placeholder(self):
        from hotspot_pulse.models import RegionGraph, TopicGraph
        with tempfile.TemporaryDirectory() as tmp:
            graph = render_network(RegionGraph(), TopicGraph(), RenderConfig(output_dir=tmp))
            self.assertEqual(graph.nodes, [])
            with open(graph.graph_path, encoding="utf-8") as fh:
                self.assertIn("暂无数据", fh.read())

    def test_force_layout_deterministic_and_bounded(self):
        # 同一输入两次渲染字节级一致（无随机）；坐标都在画布内并写入 nodes_data
        posts = [make_post(i, ["AI芯片", "股价"], region="中国") for i in range(3)]
        posts += [make_post(10, ["AI芯片"], region="美国")]
        region_g = build_region_graph(posts, RegionMapperConfig(mode="field"))
        topic_g = build_topic_graph(posts, "AI芯片")

        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            cfg1, cfg2 = RenderConfig(output_dir=tmp1), RenderConfig(output_dir=tmp2)
            g1 = render_network(region_g, topic_g, cfg1)
            g2 = render_network(region_g, topic_g, cfg2)
            with open(g1.graph_path, encoding="utf-8") as fh:
                svg1 = fh.read()
            with open(g2.graph_path, encoding="utf-8") as fh:
                svg2 = fh.read()
            self.assertEqual(svg1, svg2)

        width_px, height_px = int(cfg1.width * 96), int(cfg1.height * 96)
        for node in g1.nodes:
            self.assertIn("x", node)
            self.assertIn("y", node)
            self.assertGreaterEqual(node["x"], 0)
            self.assertLessEqual(node["x"], width_px)
            self.assertGreaterEqual(node["y"], 0)
            self.assertLessEqual(node["y"], height_px)

    def test_region_and_topic_nodes_separate_clusters(self):
        # 力导向 + 同类聚簇：区域簇质心与话题簇质心应明显分开（比节点半径大）
        posts = [make_post(i, ["AI芯片", f"话{i}"], region="中国") for i in range(2)]
        posts += [make_post(10, ["AI芯片", f"话{i}"], region="美国") for i in range(2, 4)]
        region_g = build_region_graph(posts, RegionMapperConfig(mode="field"))
        topic_g = build_topic_graph(posts, "AI芯片")
        with tempfile.TemporaryDirectory() as tmp:
            graph = render_network(region_g, topic_g, RenderConfig(output_dir=tmp))
        regions = [n for n in graph.nodes if n["kind"] == "region"]
        topics = [n for n in graph.nodes if n["kind"] == "topic"]
        self.assertTrue(regions and topics)
        r_cx = sum(n["x"] for n in regions) / len(regions)
        t_cx = sum(n["x"] for n in topics) / len(topics)
        self.assertGreater(abs(r_cx - t_cx), 2 * max(n["size"] for n in graph.nodes))


if __name__ == "__main__":
    unittest.main()

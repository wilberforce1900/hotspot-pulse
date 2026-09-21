"""tests/test_rss.py — RSS/Atom 适配器：纯解析（无网络）、DTD 防护、配置接线。"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone

from hotspot_pulse.config import load_config
from hotspot_pulse.models import DataSource, Query
from hotspot_pulse.stages.collector import _parse_date, _parse_feed, collect

FIXED_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

RSS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rss version="2.0"><channel><title>科技新闻</title>'
    '<item><title>AI芯片利好 &amp; 增长</title>'
    '<link>https://example.com/1</link><guid>guid-1</guid>'
    '<description>&lt;p&gt;市场看涨&lt;/p&gt;</description>'
    '<pubDate>Mon, 15 Sep 2026 08:00:00 GMT</pubDate></item>'
    '<item><title>第二条</title><link>https://example.com/2</link>'
    '<pubDate>not-a-date</pubDate></item>'
    '</channel></rss>'
).encode("utf-8")

ATOM_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<feed xmlns="http://www.w3.org/2005/Atom"><title>T</title>'
    '<entry><id>a1</id><title>标题一</title><link href="https://e.com/1"/>'
    '<summary>摘要内容</summary><updated>2026-09-15T09:00:00Z</updated></entry>'
    '</feed>'
).encode("utf-8")

DTD_XML = (
    '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "boom">]>'
    '<rss version="2.0"><channel><item><title>&a;</title></item></channel></rss>'
).encode("utf-8")


def make_query() -> Query:
    return Query(tag="AI芯片", data_source=DataSource.RSS)


class TestParseFeed(unittest.TestCase):
    def test_rss2_items_to_posts(self):
        posts, errors = _parse_feed(RSS_XML, make_query(), FIXED_NOW, max_posts=100)
        self.assertEqual(errors, [])
        self.assertEqual(len(posts), 2)
        first = posts[0]
        self.assertEqual(first.external_id, "guid-1")
        self.assertEqual(first.source, DataSource.RSS)
        self.assertEqual(first.created_at, datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc))
        self.assertIn("AI芯片利好", first.text)       # HTML 实体已还原
        self.assertIn("市场看涨", first.text)
        self.assertNotIn("<p>", first.text)            # HTML 标签已剥离
        self.assertEqual(first.tags, ["AI芯片"])
        second = posts[1]
        self.assertEqual(second.external_id, "https://example.com/2")  # 无 guid 回退 link
        self.assertEqual(second.created_at, FIXED_NOW)  # 日期解析失败回退 now

    def test_atom_entries_to_posts(self):
        posts, errors = _parse_feed(ATOM_XML, make_query(), FIXED_NOW, max_posts=100)
        self.assertEqual(errors, [])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].external_id, "a1")
        self.assertEqual(posts[0].created_at,
                         datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc))

    def test_dtd_entity_rejected(self):
        posts, errors = _parse_feed(DTD_XML, make_query(), FIXED_NOW, max_posts=100)
        self.assertEqual(posts, [])
        self.assertTrue(any("实体" in e for e in errors))

    def test_malformed_xml_rejected(self):
        posts, errors = _parse_feed(b"<rss><unclosed>", make_query(), FIXED_NOW, 100)
        self.assertEqual(posts, [])
        self.assertTrue(any("解析失败" in e for e in errors))

    def test_max_posts_truncates(self):
        posts, _ = _parse_feed(RSS_XML, make_query(), FIXED_NOW, max_posts=1)
        self.assertEqual(len(posts), 1)


class TestParseDate(unittest.TestCase):
    def test_rfc822(self):
        self.assertEqual(_parse_date("Mon, 15 Sep 2026 08:00:00 GMT"),
                         datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc))

    def test_iso8601(self):
        self.assertEqual(_parse_date("2026-09-15T09:00:00Z"),
                         datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc))

    def test_garbage_and_none(self):
        self.assertIsNone(_parse_date("not-a-date"))
        self.assertIsNone(_parse_date(None))
        self.assertIsNone(_parse_date(""))


class TestRssConfigAndCollect(unittest.TestCase):
    def test_config_whitelist_carries_feed_settings(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write('{"data_source": "rss", "base_url": "https://example.com/feed",'
                     ' "api_key_env": "HOTSPOT_RSS_KEY", "params": {"lang": "zh"},'
                     ' "max_posts": 50}')
            path = fh.name
        try:
            cfg = load_config(path)
            src = cfg.data_sources["rss"]
            self.assertEqual(src.kind, DataSource.RSS)
            self.assertEqual(src.base_url, "https://example.com/feed")
            self.assertEqual(src.api_key_env, "HOTSPOT_RSS_KEY")  # 只存环境变量名
            self.assertEqual(src.params, {"lang": "zh"})
            self.assertEqual(src.max_posts, 50)
            # 配置指定的数据源应传导进 Query（无需输入串再写 source:rss）
            from hotspot_pulse.stages.input_parser import parse_user_input
            q = parse_user_input("AI芯片 24小时", cfg).query
            self.assertEqual(q.data_source, DataSource.RSS)
            # 显式 source: 覆盖配置
            q2 = parse_user_input("AI芯片 source:mock", cfg).query
            self.assertEqual(q2.data_source, DataSource.MOCK)
        finally:
            os.unlink(path)

    def test_collect_rss_unconfigured_degrades(self):
        result = collect(make_query(), now=FIXED_NOW, source_cfg=None)
        self.assertEqual(result.posts, [])
        self.assertTrue(any("RSS 源未配置" in e for e in result.errors))

    def test_collect_rss_unreachable_degrades(self):
        from hotspot_pulse.config import DataSourceConfig
        cfg = DataSourceConfig(kind=DataSource.RSS,
                               base_url="http://127.0.0.1:9/feed")  # discard 端口，必失败
        result = collect(make_query(), now=FIXED_NOW, source_cfg=cfg)
        self.assertEqual(result.posts, [])
        self.assertTrue(any("RSS 拉取失败" in e for e in result.errors))


if __name__ == "__main__":
    unittest.main()

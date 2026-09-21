"""tests/test_gdelt.py — GDELT Doc 2.0 适配器：纯解析（无网络）、日期、降级、配置。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from hotspot_pulse.config import DataSourceConfig, load_config
from hotspot_pulse.models import DataSource, Query
from hotspot_pulse.stages.collector import (
    _parse_gdelt,
    _parse_gdelt_seendate,
    collect,
)

FIXED_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

GDELT_JSON = json.dumps({
    "articles": [
        {"url": "https://news.example.com/a1", "title": "AI芯片利好增长",
         "seendate": "20260915T080000Z", "domain": "news.example.com",
         "language": "Chinese", "sourcecountry": "China"},
        {"url": "https://us.example.com/a2", "title": "Chip market update",
         "seendate": "20260915T093000Z", "domain": "us.example.com",
         "language": "English", "sourcecountry": "United States"},
        {"url": "", "title": "", "seendate": "20260915T100000Z"},          # 空条目跳过
        {"url": "https://jp.example.com/a3", "title": "半導体報道",
         "seendate": "bad-date", "domain": "jp.example.com",
         "language": "Japanese", "sourcecountry": "Brazil"},               # 日期失败回退 now
    ],
}).encode("utf-8")


def make_query() -> Query:
    return Query(tag="AI芯片", data_source=DataSource.GDELT)


class TestParseGdelt(unittest.TestCase):
    def test_articles_to_posts(self):
        posts, errors = _parse_gdelt(GDELT_JSON, make_query(), FIXED_NOW, max_posts=250)
        self.assertEqual(errors, [])
        self.assertEqual(len(posts), 3)                    # 空条目被跳过
        first = posts[0]
        self.assertEqual(first.external_id, "https://news.example.com/a1")
        self.assertEqual(first.source, DataSource.GDELT)
        self.assertEqual(first.created_at, datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(first.tags, ["AI芯片"])
        self.assertIn("AI芯片利好增长", first.text)
        self.assertIn("news.example.com", first.text)      # domain 拼进文本

    def test_country_mapping(self):
        posts, _ = _parse_gdelt(GDELT_JSON, make_query(), FIXED_NOW, max_posts=250)
        by_id = {p.external_id: p for p in posts}
        self.assertEqual(by_id["https://news.example.com/a1"].region, "中国")
        self.assertEqual(by_id["https://us.example.com/a2"].region, "美国")
        # 未收录国别原样透传 + 坏日期回退 now
        self.assertEqual(by_id["https://jp.example.com/a3"].region, "Brazil")
        self.assertEqual(by_id["https://jp.example.com/a3"].created_at, FIXED_NOW)

    def test_max_posts_truncates(self):
        posts, _ = _parse_gdelt(GDELT_JSON, make_query(), FIXED_NOW, max_posts=1)
        self.assertEqual(len(posts), 1)

    def test_non_json_degrades(self):
        posts, errors = _parse_gdelt(b"<html>rate limited</html>", make_query(), FIXED_NOW, 10)
        self.assertEqual(posts, [])
        self.assertTrue(any("非 JSON" in e for e in errors))

    def test_missing_articles_degrades(self):
        posts, errors = _parse_gdelt(b'{"foo": 1}', make_query(), FIXED_NOW, 10)
        self.assertEqual(posts, [])
        self.assertTrue(any("articles" in e for e in errors))


class TestParseSeenDate(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(_parse_gdelt_seendate("20260915T080000Z"),
                         datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc))

    def test_invalid_and_none(self):
        self.assertIsNone(_parse_gdelt_seendate("bad-date"))
        self.assertIsNone(_parse_gdelt_seendate(""))
        self.assertIsNone(_parse_gdelt_seendate(None))


class TestGdeltConfigAndCollect(unittest.TestCase):
    def test_config_whitelist(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write('{"data_source": "gdelt", "max_posts": 100}')
            path = fh.name
        try:
            cfg = load_config(path)
            src = cfg.data_sources["gdelt"]
            self.assertEqual(src.kind, DataSource.GDELT)
            self.assertEqual(src.max_posts, 100)
            self.assertEqual(src.api_key_env, "")          # 免密钥
        finally:
            os.unlink(path)

    def test_collect_gdelt_unreachable_degrades(self):
        cfg = DataSourceConfig(kind=DataSource.GDELT,
                               base_url="http://127.0.0.1:9/doc")  # discard 端口，必失败
        result = collect(make_query(), now=FIXED_NOW, source_cfg=cfg)
        self.assertEqual(result.posts, [])
        self.assertTrue(any("GDELT 拉取失败" in e for e in result.errors))

    def test_parse_layer_keeps_duplicates_for_dedup(self):
        # 同一 URL 出现两次：解析层不去重，交给 collect() 的幂等去重收敛
        doubled = json.dumps({
            "articles": [
                {"url": "https://x.com/1", "title": "t1", "seendate": "20260915T080000Z",
                 "sourcecountry": "China"},
                {"url": "https://x.com/1", "title": "t1", "seendate": "20260915T080000Z",
                 "sourcecountry": "China"},
            ],
        }).encode()
        posts, _ = _parse_gdelt(doubled, make_query(), FIXED_NOW, 10)
        self.assertEqual(len(posts), 2)
        keys = {(p.source.value, p.external_id) for p in posts}
        self.assertEqual(len(keys), 1)   # collect() 按 key 去重后只会剩一条


if __name__ == "__main__":
    unittest.main()

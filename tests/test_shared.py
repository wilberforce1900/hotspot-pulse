"""tests/test_shared.py — 共享常量与稳定哈希（单一来源校验）。"""

from __future__ import annotations

import unittest

from hotspot_pulse.shared import (
    DEFAULT_REGIONS,
    EMOTION_KEYWORDS,
    NEGATIVE_KEYWORDS,
    POSITIVE_KEYWORDS,
    stable_hash,
)


class TestShared(unittest.TestCase):
    def test_stable_hash_deterministic_and_discriminating(self):
        self.assertEqual(stable_hash("AI芯片"), stable_hash("AI芯片"))
        self.assertNotEqual(stable_hash("AI芯片"), stable_hash("新能源"))
        self.assertIsInstance(stable_hash("x"), int)

    def test_default_regions_shared_shape(self):
        self.assertEqual(set(DEFAULT_REGIONS), {"中国", "美国", "日本", "欧洲"})

    def test_lexicon_nonempty_and_disjoint(self):
        self.assertTrue(POSITIVE_KEYWORDS)
        self.assertTrue(NEGATIVE_KEYWORDS)
        self.assertFalse(POSITIVE_KEYWORDS & NEGATIVE_KEYWORDS)
        self.assertEqual(set(EMOTION_KEYWORDS),
                         {"anger", "fear", "joy", "sadness", "surprise"})


if __name__ == "__main__":
    unittest.main()

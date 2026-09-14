"""tests/test_growth.py — 阶段4 时序聚合、缺桶补零、预测器与破点检测。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from hotspot_pulse.config import PredictorConfig
from hotspot_pulse.models import DataSource, HeatRecord, Post, PredictorKind
from hotspot_pulse.stages.growth import (
    _detect_signal,
    _forecast_damped,
    _forecast_linear,
    _to_heat_series,
    predict_growth,
)

T0 = datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc)


def make_post(i: int, hour: int, tags: list[str], text: str = "讨论") -> Post:
    return Post(
        external_id=f"p{i}",
        source=DataSource.MOCK,
        created_at=T0 + timedelta(hours=hour),
        text=text,
        tags=tags,
    )


def make_series(volumes: list[int]) -> list[HeatRecord]:
    return [HeatRecord(ts=T0 + timedelta(hours=i), topic="X", volume=v)
            for i, v in enumerate(volumes)]


class TestToHeatSeries(unittest.TestCase):
    def test_buckets_by_hour_and_tag(self):
        posts = [
            make_post(0, 1, ["AI芯片", "股价"]),
            make_post(1, 1, ["AI芯片"]),
            make_post(2, 2, ["AI芯片"]),
        ]
        records = _to_heat_series(posts, bucket_hours=1)
        by_key = {(r.topic, r.ts.hour): r.volume for r in records}
        self.assertEqual(by_key[("AI芯片", 1)], 2)
        self.assertEqual(by_key[("AI芯片", 2)], 1)
        self.assertEqual(by_key[("股价", 1)], 1)

    def test_heat_score_normalized_per_topic(self):
        posts = [
            make_post(0, 1, ["A", "B"]),
            make_post(1, 1, ["A"]),
            make_post(2, 2, ["A"]),
        ]
        records = _to_heat_series(posts)
        scores = {(r.topic, r.ts.hour): r.heat_score for r in records}
        self.assertEqual(scores[("A", 1)], 1.0)   # A 的峰值桶（2 点）
        self.assertEqual(scores[("B", 1)], 1.0)   # B 唯一桶即峰值
        self.assertEqual(scores[("A", 2)], 0.5)   # 2 点中 1 点

    def test_gap_buckets_filled_with_zero(self):
        # 话题只在第 0 和第 3 小时有数据 → 中间 2 个缺桶必须补零（等距时序）
        posts = [make_post(0, 0, ["G"]), make_post(1, 3, ["G"])]
        records = _to_heat_series(posts)
        self.assertEqual(len(records), 4)
        volumes = [r.volume for r in records]
        self.assertEqual(volumes, [1, 0, 0, 1])
        # 等距校验：相邻桶时间差恒为 1 小时
        for prev, cur in zip(records, records[1:]):
            self.assertEqual(cur.ts - prev.ts, timedelta(hours=1))


class TestForecastLinear(unittest.TestCase):
    def test_rising_series_positive_slope(self):
        predicted, confidence, _, ci_high = _forecast_linear(make_series([1, 2, 3, 4, 5, 6]), 3)
        self.assertTrue(all(b > a for a, b in zip(predicted, predicted[1:])))
        self.assertGreater(predicted[0], 6)  # 延续上升趋势
        self.assertAlmostEqual(confidence, 1.0, places=6)  # 完美线性 R²=1
        self.assertTrue(all(h >= p for p, h in zip(predicted, ci_high)))  # 区间覆盖中心

    def test_flat_series_zero_growth(self):
        predicted, _, _, _ = _forecast_linear(make_series([3, 3, 3, 3]), 2)
        self.assertEqual(predicted, [3.0, 3.0])

    def test_insufficient_samples(self):
        predicted, confidence, ci_low, ci_high = _forecast_linear(make_series([1]), 3)
        self.assertEqual(predicted, [0.0, 0.0, 0.0])
        self.assertEqual(confidence, 0.0)
        self.assertEqual((ci_low, ci_high), ([0.0] * 3, [0.0] * 3))

    def test_prediction_never_negative(self):
        predicted, _, ci_low, _ = _forecast_linear(make_series([10, 6, 2]), 5)
        self.assertTrue(all(v >= 0.0 for v in predicted))
        self.assertTrue(all(v >= 0.0 for v in ci_low))


class TestForecastDamped(unittest.TestCase):
    def test_rising_series_increment_decays(self):
        predicted, _, _, _ = _forecast_damped(make_series([1, 2, 3, 4, 5, 6]), 6)
        increments = [b - a for a, b in zip(predicted, predicted[1:])]
        self.assertTrue(all(i > 0 for i in increments))       # 仍在上升
        self.assertTrue(all(b <= a for a, b in zip(increments, increments[1:])))  # 增量逐级衰减

    def test_long_horizon_converges_to_plateau(self):
        predicted, _, _, _ = _forecast_damped(make_series([1, 3, 5, 7, 9, 11]), 48)
        tail = predicted[-6:]
        spread = max(tail) - min(tail)
        self.assertLess(spread, 0.5)  # 远端收敛到平台，不发散

    def test_confidence_band_covers_center_and_widens(self):
        predicted, _, ci_low, ci_high = _forecast_damped(make_series([2, 1, 4, 3, 6, 5]), 4)
        for p, lo, hi in zip(predicted, ci_low, ci_high):
            self.assertLessEqual(lo, p)
            self.assertGreaterEqual(hi, p)
        widths = [h - lo for lo, h in zip(ci_low, ci_high)]
        self.assertTrue(all(b >= a for a, b in zip(widths, widths[1:])))  # 区间随视野变宽

    def test_declining_series_still_declines(self):
        predicted, _, _, _ = _forecast_damped(make_series([9, 7, 5, 3, 1]), 3)
        self.assertTrue(all(b <= a for a, b in zip(predicted, predicted[1:])))


class TestDetectSignal(unittest.TestCase):
    def test_breakout_detected(self):
        cfg = PredictorConfig(breakout_velocity_ratio=1.5)
        sig = _detect_signal(make_series([2, 2, 2, 20, 30, 40, 50, 60]), cfg)
        self.assertTrue(sig.is_breakout)
        self.assertGreater(sig.acceleration, 0)

    def test_no_breakout_on_flat_series(self):
        cfg = PredictorConfig()
        sig = _detect_signal(make_series([3, 3, 3, 3, 3, 3]), cfg)
        self.assertFalse(sig.is_breakout)
        self.assertEqual(sig.acceleration, 0.0)

    def test_small_sample_skips_breakout(self):
        cfg = PredictorConfig()
        sig = _detect_signal(make_series([1, 1, 100, 200]), cfg)  # n=4 < 6
        self.assertFalse(sig.is_breakout)


class TestPredictGrowth(unittest.TestCase):
    def test_topics_sorted_by_predicted_volume(self):
        cfg = PredictorConfig()
        posts = (
            [make_post(i, i % 10, ["热"]) for i in range(30)]
            + [make_post(100 + i, i % 10, ["冷"]) for i in range(6)]
        )
        preds = predict_growth(posts, cfg, horizon_hours=6)
        self.assertGreaterEqual(len(preds), 2)
        tails = [p.predicted_volume[-1] for p in preds]
        self.assertEqual(tails, sorted(tails, reverse=True))

    def test_low_sample_confidence_discounted(self):
        cfg = PredictorConfig(min_samples=8)
        # 3 个桶、量 [1,2,3] 递增：n=3 < min_samples → 置信度应打 0.5 折
        posts = (
            [make_post(0, 0, ["小样本"])]
            + [make_post(1, 1, ["小样本"]), make_post(2, 1, ["小样本"])]
            + [make_post(i, 2, ["小样本"]) for i in (3, 4, 5)]
        )
        preds = predict_growth(posts, cfg, horizon_hours=2)
        self.assertEqual(len(preds), 1)
        raw_conf = _forecast_linear(make_series([1, 2, 3]), 2)[1]
        self.assertGreater(raw_conf, 0)
        self.assertAlmostEqual(preds[0].confidence, raw_conf * 0.5)

    def test_model_kind_recorded(self):
        posts = [make_post(i, i % 6, ["话题"]) for i in range(12)]
        preds = predict_growth(posts, PredictorConfig(kind=PredictorKind.LINEAR), 3)
        self.assertEqual(preds[0].model, PredictorKind.LINEAR)

    def test_damped_is_default(self):
        posts = [make_post(i, i % 6, ["话题"]) for i in range(12)]
        preds = predict_growth(posts, PredictorConfig(), 3)
        self.assertEqual(preds[0].model, PredictorKind.DAMPED)

    def test_bands_len_matches_horizon(self):
        posts = [make_post(i, i % 6, ["话题"]) for i in range(12)]
        preds = predict_growth(posts, PredictorConfig(), 5)
        for p in preds:
            self.assertEqual(len(p.ci_low), 5)
            self.assertEqual(len(p.ci_high), 5)
            self.assertEqual(len(p.ci_low), len(p.predicted_volume))


if __name__ == "__main__":
    unittest.main()

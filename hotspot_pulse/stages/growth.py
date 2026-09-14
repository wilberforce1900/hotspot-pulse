"""
stages/growth.py — 阶段4：短期增长预测。

分工：senior-coder 出「实现草稿」+ **V4 Pro 亲自审定算法选型与判定口径**。
职责：
  1. 把 Post 流按时间桶聚合 → HeatRecord 时序（缺桶补零，保证等距）。
  2. 按 PredictorKind 用策略模式预测未来 horizon 量 + 80% 置信区间。
  3. 提取 TrendSignal（velocity / acceleration / breakout）。

V4 Pro 审定口径：
  - DAMPED（默认）阻尼趋势：ŷ(n+i) = a + b·Σφ^j。热点演化近似 S 形，
    无阻尼线性外推在长视野发散（爆发期低估、持续期高估），φ<1 使增量逐级衰减。
  - LINEAR 最小二乘保留为可选（最省）。
  - 置信区间：拟合残差 σ 的 80% 带（z≈1.28），宽度随视野 √i 增长，下界夹 ≥0。
  - 破点：n>=6 才判定（前/后半段斜率对比），避免小样本误报。
  - 样本不足 min_samples → 置信度 ×0.5（不硬造）。
  - 修复：_to_heat_series 对全量 posts 按 tag 聚合一次，再按 topic 分组建模，
    避免「先按话题切分 + 内部再遍历全量 tags」导致的跨话题串流；
    并对首末观测桶之间的缺桶补零——否则相隔多小时的两点会被当作相邻点，
    系统性扭曲斜率与破点判定。
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta

from hotspot_pulse.config import PredictorConfig
from hotspot_pulse.models import HeatRecord, Post, Prediction, PredictorKind, TrendSignal

logger = logging.getLogger(__name__)

_Z80 = 1.2816  # 80% 双侧置信区间 z 值


def predict_growth(posts: list[Post], cfg: PredictorConfig, horizon_hours: int) -> list[Prediction]:
    """预测主入口：按话题切分时序，各自建模。"""
    records = _to_heat_series(posts, bucket_hours=1)
    by_topic: dict[str, list[HeatRecord]] = defaultdict(list)
    for r in records:
        by_topic[r.topic].append(r)

    predictions: list[Prediction] = []
    for topic, series in by_topic.items():
        n = len(series)
        if n < 2:
            continue
        discount = 0.5 if n < cfg.min_samples else 1.0

        predicted_volume, raw_conf, ci_low, ci_high = _forecast(
            series, horizon_hours, cfg.kind, cfg.damping_factor)
        confidence = max(0.0, min(1.0, raw_conf * discount))
        signal = _detect_signal(series, cfg)

        forecast = []
        last_ts = series[-1].ts
        for i, v in enumerate(predicted_volume):
            forecast.append((last_ts + timedelta(hours=i + 1), v))

        predictions.append(Prediction(
            topic=topic,
            horizon_hours=horizon_hours,
            predicted_volume=predicted_volume,
            forecast=forecast,
            confidence=confidence,
            signal=signal,
            model=cfg.kind,
            ci_low=ci_low,
            ci_high=ci_high,
        ))

    predictions.sort(key=lambda p: p.predicted_volume[-1] if p.predicted_volume else 0.0,
                     reverse=True)
    logger.info("增长预测完成：%d 个话题（model=%s）", len(predictions), cfg.kind.value)
    return predictions


def _to_heat_series(posts: list[Post], bucket_hours: int = 1) -> list[HeatRecord]:
    """对全量 posts 按 (topic, 时间桶) 聚合；每条 post 的每个 tag 各计一次。

    缺桶补零：某话题首末观测桶之间没有数据的桶也产出 volume=0 记录，
    保证时序等距——下游斜率/破点把 (index) 当时间轴，缺桶会压缩时间距离。
    """
    bucket_counts: dict[tuple[str, datetime], int] = defaultdict(int)
    for post in posts:
        hour = post.created_at.hour
        bucket = post.created_at.replace(
            hour=(hour // bucket_hours) * bucket_hours,
            minute=0, second=0, microsecond=0,
        )
        for tag in post.tags:
            bucket_counts[(tag, bucket)] += 1

    topic_series: dict[str, dict[datetime, int]] = defaultdict(dict)
    topic_max: dict[str, int] = defaultdict(int)
    for (topic, ts), v in bucket_counts.items():
        topic_series[topic][ts] = v
        topic_max[topic] = max(topic_max[topic], v)

    step = timedelta(hours=bucket_hours)
    records: list[HeatRecord] = []
    for topic, series in topic_series.items():
        max_v = topic_max[topic]
        ts_cur, ts_end = min(series), max(series)
        while ts_cur <= ts_end:
            v = series.get(ts_cur, 0)
            records.append(HeatRecord(
                ts=ts_cur, topic=topic, volume=v,
                heat_score=0.0 if max_v == 0 else v / max_v,
            ))
            ts_cur += step
    records.sort(key=lambda r: (r.topic, r.ts))
    return records


def _slope(x: list, y: list) -> float:
    """最小二乘斜率 b = (n·Σxy − ΣxΣy) / (n·Σx² − (Σx)²)。"""
    n = len(x)
    if n < 2:
        return 0.0
    sx = sum(x)
    sy = sum(y)
    sxy = sum(xi * yi for xi, yi in zip(x, y))
    sxx = sum(xi * xi for xi in x)
    denom = n * sxx - sx * sx
    return 0.0 if denom == 0 else (n * sxy - sx * sy) / denom


def _detect_signal(series: list[HeatRecord], cfg: PredictorConfig) -> TrendSignal:
    """从时序提取速度/加速度/破点（V4 Pro 把关口径）。

    - velocity = 全序列线性斜率（量/小时）：稳健、与预测趋势一致，
      避免「最近 3 点」被末端截断桶/随机抖动带偏。
    - acceleration = 后半段斜率 − 前半段斜率（趋势是否在加速）。
    - 破点：n>=6 才判定（前后半段有基线），后半段斜率 > 前半段 × ratio 且 >0。
    """
    n = len(series)
    volumes = [r.volume for r in series]

    velocity = _slope(list(range(n)), volumes) if n >= 2 else 0.0

    acceleration = 0.0
    is_breakout = False
    breakout_at = None
    if n >= 6:
        half = n // 2
        slope_before = _slope(list(range(half)), volumes[:half])
        slope_after = _slope(list(range(half, n)), volumes[half:])
        acceleration = slope_after - slope_before
        if slope_after > slope_before * cfg.breakout_velocity_ratio and slope_after > 0:
            is_breakout = True
            breakout_at = series[half].ts

    return TrendSignal(velocity=velocity, acceleration=acceleration,
                       is_breakout=is_breakout, breakout_at=breakout_at)


def _fit_linear(series: list[HeatRecord]) -> tuple[float, float, float, float]:
    """对 (index, volume) 做最小二乘拟合，返回 (a, b, R², 残差标准差 σ)。"""
    n = len(series)
    x = list(range(n))
    y = [r.volume for r in series]
    sx = sum(x)
    sy = sum(y)
    sxy = sum(xi * yi for xi, yi in zip(x, y))
    sxx = sum(xi * xi for xi in x)
    denom = n * sxx - sx * sx
    b = 0.0 if denom == 0 else (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n

    y_mean = sy / n
    ss_res = sum((yi - (a + b * xi)) ** 2 for xi, yi in zip(x, y))
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    r2 = 0.0 if ss_tot == 0 else 1.0 - (ss_res / ss_tot)
    sigma = math.sqrt(ss_res / max(1, n - 2))
    return a, b, max(0.0, min(1.0, r2)), sigma


def _bands(sigma: float, n: int, horizon_hours: int,
           center: list[float]) -> tuple[list[float], list[float]]:
    """80% 置信带：±z·σ·√(1+i/n)，宽度随视野增长；下界夹 ≥0。"""
    ci_low: list[float] = []
    ci_high: list[float] = []
    for i, v in enumerate(center, start=1):
        band = _Z80 * sigma * math.sqrt(1.0 + i / max(1, n))
        ci_low.append(max(0.0, v - band))
        ci_high.append(v + band)
    return ci_low, ci_high


def _forecast(series: list[HeatRecord], horizon_hours: int,
              kind: PredictorKind, damping_factor: float = 0.85,
              ) -> tuple[list[float], float, list[float], list[float]]:
    """具体预测器（策略）。返回 (predicted, R², ci_low, ci_high)。

    arima / prophet / lstm 未实现（需数据量/外部依赖），回落 DAMPED。
    """
    if kind == PredictorKind.LINEAR:
        return _forecast_linear(series, horizon_hours)
    # DAMPED 及未实现的 kind 统一走阻尼趋势
    return _forecast_damped(series, horizon_hours, damping_factor)


def _forecast_linear(series: list[HeatRecord], horizon_hours: int,
                     ) -> tuple[list[float], float, list[float], list[float]]:
    """LINEAR 无阻尼最小二乘：ŷ = a + b·x，预测未来 horizon 桶。"""
    n = len(series)
    if n < 2:
        zero = [0.0] * horizon_hours
        return zero, 0.0, list(zero), list(zero)

    a, b, r2, sigma = _fit_linear(series)
    predicted = [max(0.0, a + b * (n + i)) for i in range(horizon_hours)]
    ci_low, ci_high = _bands(sigma, n, horizon_hours, predicted)
    return predicted, r2, ci_low, ci_high


def _forecast_damped(series: list[HeatRecord], horizon_hours: int, phi: float = 0.85,
                     ) -> tuple[list[float], float, list[float], list[float]]:
    """DAMPED 阻尼趋势：ŷ(n+i) = a + b·Σ_{j=1..i} φ^j。

    增量逐级衰减（b·φ^i），收敛到平台 a + b·φ/(1-φ)，贴合热点「升温后饱和」
    的 S 形演化；斜率 b≤0（降温）时与线性一致地递减。
    """
    n = len(series)
    if n < 2:
        zero = [0.0] * horizon_hours
        return zero, 0.0, list(zero), list(zero)

    a, b, r2, sigma = _fit_linear(series)
    phi = min(max(phi, 0.01), 0.99)
    denom = 1.0 - phi
    predicted = [max(0.0, a + b * (phi * (1.0 - phi ** i) / denom))
                 for i in range(1, horizon_hours + 1)]
    ci_low, ci_high = _bands(sigma, n, horizon_hours, predicted)
    return predicted, r2, ci_low, ci_high

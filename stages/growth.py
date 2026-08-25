"""
stages/growth.py — 阶段4：短期增长预测。

分工：senior-coder 出「实现草稿」+ **V4 Pro 亲自审定算法选型与判定口径**。
职责：
  1. 把 Post 流按时间桶聚合 → HeatRecord 时序。
  2. 按 PredictorKind 用策略模式预测未来 horizon 量。
  3. 提取 TrendSignal（velocity / acceleration / breakout）。

V4 Pro 审定口径：
  - LINEAR 最小二乘 + R² 置信度。
  - 破点：n>=6 才判定（前/后半段斜率对比），避免小样本误报。
  - 样本不足 min_samples → 置信度 ×0.5（不硬造）。
  - 修复：_to_heat_series 对全量 posts 按 tag 聚合一次，再按 topic 分组建模，
    避免「先按话题切分 + 内部再遍历全量 tags」导致的跨话题串流。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from config import PredictorConfig
from models import HeatRecord, Post, Prediction, PredictorKind, TrendSignal


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

        predicted_volume, confidence = _forecast(series, horizon_hours, cfg.kind)
        confidence = max(0.0, min(1.0, confidence * discount))
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
        ))

    predictions.sort(key=lambda p: p.predicted_volume[-1] if p.predicted_volume else 0.0,
                     reverse=True)
    return predictions


def _to_heat_series(posts: list[Post], bucket_hours: int = 1) -> list[HeatRecord]:
    """对全量 posts 按 (topic, 时间桶) 聚合；每条 post 的每个 tag 各计一次。"""
    bucket_counts: dict[tuple[str, object], int] = defaultdict(int)
    for post in posts:
        hour = post.created_at.hour
        bucket = post.created_at.replace(
            hour=(hour // bucket_hours) * bucket_hours,
            minute=0, second=0, microsecond=0,
        )
        for tag in post.tags:
            bucket_counts[(tag, bucket)] += 1

    topic_max: dict[str, int] = defaultdict(int)
    for (topic, _), v in bucket_counts.items():
        topic_max[topic] = max(topic_max[topic], v)

    records: list[HeatRecord] = []
    for (topic, ts), v in bucket_counts.items():
        max_v = topic_max[topic]
        records.append(HeatRecord(
            ts=ts, topic=topic, volume=v,
            heat_score=0.0 if max_v == 0 else v / max_v,
        ))
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


def _forecast(series: list[HeatRecord], horizon_hours: int,
              kind: object) -> tuple[list[float], float]:
    """具体预测器（策略）。预演默认 LINEAR；其它 kind 回落 LINEAR。"""
    if kind == PredictorKind.LINEAR:
        return _forecast_linear(series, horizon_hours)
    # arima / prophet / lstm 未实现（需数据量/外部依赖），预演统一回落 LINEAR
    return _forecast_linear(series, horizon_hours)


def _forecast_linear(series: list[HeatRecord], horizon_hours: int) -> tuple[list[float], float]:
    """LINEAR 最小二乘：y = a + b·x，预测未来 horizon 桶，置信度 = R²（夹 [0,1]）。"""
    n = len(series)
    if n < 2:
        return [0.0] * horizon_hours, 0.0

    x = list(range(n))
    y = [r.volume for r in series]
    sx = sum(x)
    sy = sum(y)
    sxy = sum(xi * yi for xi, yi in zip(x, y))
    sxx = sum(xi * xi for xi in x)
    denom = n * sxx - sx * sx
    b = 0.0 if denom == 0 else (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n

    predicted = [max(0.0, a + b * (n + i)) for i in range(horizon_hours)]

    y_mean = sy / n
    ss_res = sum((yi - (a + b * xi)) ** 2 for xi, yi in zip(x, y))
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    r2 = 0.0 if ss_tot == 0 else 1.0 - (ss_res / ss_tot)
    return predicted, max(0.0, min(1.0, r2))

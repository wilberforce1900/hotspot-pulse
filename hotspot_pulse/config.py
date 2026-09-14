"""
config.py — 系统配置（策略/适配器的选择器）。

分工：本文件由 lightweight-assistant 起草、V4 Pro 审定（低风险、机械）。
作用：把所有「可替换件」的选择集中到一处，主流程不硬编码。

原则：
- 配置决定「用哪个适配器/策略/算法」，不改主流程代码。
- 预演默认全部走 mock + 最省算法，保证无网络可端到端跑通。
- 环境无 numpy/matplotlib/networkx/yaml → 全部纯标准库；配置加载用 JSON。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from hotspot_pulse.models import DataSource, LayoutAlgo, PredictorKind


@dataclass(slots=True)
class DataSourceConfig:
    """某个数据源的连接参数（按 DataSource 各配一份）。"""
    kind: DataSource = DataSource.MOCK
    base_url: str = ""                  # API/爬虫端点
    api_key_env: str = ""               # 密钥的环境变量名（不写明文密钥！）
    params: dict = field(default_factory=dict)          # 额外请求参数
    poll_interval_seconds: int = 300    # 轮询间隔
    max_posts: int = 2000               # 单次上限


@dataclass(slots=True)
class RegionMapperConfig:
    """地域映射方式：如何从 Post 推断 region。"""
    mode: str = "mock"                  # mock | field(用 Post.region) | geoip
    geoip_db: str = ""                  # geoip 数据库路径（mode=geoip 时用）


@dataclass(slots=True)
class PredictorConfig:
    """增长预测器选择（策略模式）。"""
    kind: PredictorKind = PredictorKind.DAMPED   # 默认阻尼趋势：热点演化S形，线性外推会高估
    min_samples: int = 8                # 时序至少多少点才建模，否则用 mock/线性
    breakout_velocity_ratio: float = 1.5   # 破点判定：速度突增倍数阈值
    damping_factor: float = 0.85        # kind=DAMPED 的阻尼系数 φ ∈ (0,1)，越小越早平台化
    lstm_epochs: int = 20               # kind=LSTM 时的训练轮数


@dataclass(slots=True)
class RenderConfig:
    """渲染参数。"""
    layout: LayoutAlgo = LayoutAlgo.FORCE
    width: int = 12
    height: int = 8
    color_map: str = "YlOrRd"
    output_dir: str = "out/"            # 图产物目录
    combine_topic_region: bool = True   # 是否把话题子图叠加到区域热力网图


@dataclass(slots=True)
class Config:
    """总配置。各 stage 通过它取「自己那部分」，互不耦合。"""
    data_sources: dict[str, DataSourceConfig] = field(default_factory=dict)
    region_mapper: RegionMapperConfig = field(default_factory=RegionMapperConfig)
    predictor: PredictorConfig = field(default_factory=PredictorConfig)
    render: RenderConfig = field(default_factory=RenderConfig)


def default_mock_config() -> Config:
    """返回一份「离线可跑」的默认配置：mock 数据源 + DAMPED 预测 + 手写 SVG 渲染。"""
    return Config(
        data_sources={
            DataSource.MOCK.value: DataSourceConfig(kind=DataSource.MOCK, max_posts=2000),
        },
        region_mapper=RegionMapperConfig(mode="mock"),
        predictor=PredictorConfig(kind=PredictorKind.DAMPED),
        render=RenderConfig(layout=LayoutAlgo.FORCE, output_dir="out/"),
    )


def _valid_kind(value: object, enum_cls) -> bool:
    """白名单校验：只接受枚举合法值，其余一律忽略（防越权/防注入）。"""
    return isinstance(value, str) and value in {e.value for e in enum_cls}


def load_config(path: str = "") -> Config:
    """从 JSON 文件加载配置；缺省/损坏则回落默认 mock（不抛异常）。

    分工：实现由 lightweight-assistant 完成；只读、无副作用、无密钥。
    安全：只合并「白名单字段」，未知键忽略；密钥只读环境变量名，不读明文。
    """
    cfg = default_mock_config()
    if not path:
        return cfg

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return cfg  # 文件缺失或 JSON 损坏 → 回落默认，保证离线也能跑

    if not isinstance(data, dict):
        return cfg

    # ---- 白名单合并（其余键丢弃）----
    if _valid_kind(data.get("data_source"), DataSource):
        kind = DataSource(data["data_source"])
        cfg.data_sources = {kind.value: DataSourceConfig(kind=kind)}

    if _valid_kind(data.get("predictor"), PredictorKind):
        cfg.predictor.kind = PredictorKind(data["predictor"])

    rm = data.get("region_mapper")
    if isinstance(rm, dict) and isinstance(rm.get("mode"), str):
        cfg.region_mapper.mode = rm["mode"]

    if isinstance(data.get("min_samples"), int) and data["min_samples"] >= 2:
        cfg.predictor.min_samples = data["min_samples"]

    if isinstance(data.get("breakout_velocity_ratio"), (int, float)):
        cfg.predictor.breakout_velocity_ratio = float(data["breakout_velocity_ratio"])

    r = data.get("render")
    if isinstance(r, dict):
        if _valid_kind(r.get("layout"), LayoutAlgo):
            cfg.render.layout = LayoutAlgo(r["layout"])
        if isinstance(r.get("output_dir"), str):
            cfg.render.output_dir = r["output_dir"]
        if isinstance(r.get("color_map"), str):
            cfg.render.color_map = r["color_map"]

    return cfg

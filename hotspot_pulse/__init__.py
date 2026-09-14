"""
hotspot_pulse — HotSpot Pulse 热点流量预测与情绪分析系统（离线 MOCK 预演）。

包结构：
  models.py          统一数据契约（dataclass 地基）
  config.py          配置加载（适配器/策略选择器）
  cli.py             命令行入口（pip install 后的 `hotspot` 命令）
  stages/            流水线各阶段（委派单元）
    input_parser.py  —— 阶段0 输入解析+安全过滤
    collector.py     —— 阶段1 数据采集
    topic_graph.py   —— 阶段2 关联话题发现
    sentiment.py     —— 阶段3 情绪分析
    growth.py        —— 阶段4 短期增长预测
    region_heat.py   —— 阶段5 区域热度分布
    renderer.py      —— 阶段6 热力网图渲染
    orchestrator.py  —— 阶段0+7 调度与最终审核
"""

from __future__ import annotations

import logging

from hotspot_pulse.config import Config, load_config
from hotspot_pulse.models import PipelineResult, Query
from hotspot_pulse.stages.orchestrator import run_pipeline

# 库入口挂 NullHandler：不打无主日志，由应用（cli.py / 测试）决定日志落点
logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = "0.1.0"

__all__ = [
    "Config",
    "PipelineResult",
    "Query",
    "__version__",
    "load_config",
    "run_pipeline",
]

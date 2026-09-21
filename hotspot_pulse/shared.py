"""
shared.py — 跨阶段共享的词典、常量与稳定哈希（单一来源）。

为什么单独成模块：情绪词典曾在 collector.py 与 sentiment.py 各存一份、
默认区域池与稳定哈希曾在 collector.py 与 region_heat.py 各存一份——
两处定义必然漂移。此处为唯一权威来源，各 stage 只准 import 不准复制。
"""

from __future__ import annotations

import hashlib

# --------------------------------------------------------------------------- #
# 情绪词典（sentiment.py 分类口径；collector mock 文案模板同源）
# --------------------------------------------------------------------------- #
POSITIVE_KEYWORDS = frozenset({
    "利好", "上涨", "突破", "增长", "创新", "火爆", "看涨", "惊喜", "期待", "支持",
})
NEGATIVE_KEYWORDS = frozenset({
    "利空", "下跌", "风险", "担忧", "争议", "亏损", "暴跌", "质疑", "恐慌", "失望",
})

EMOTION_KEYWORDS = {
    "anger": {"愤怒", "气愤"},
    "fear": {"恐惧", "恐慌", "担忧"},
    "joy": {"开心", "惊喜", "兴奋"},
    "sadness": {"难过", "失望"},
    "surprise": {"惊讶", "意外"},
}

# --------------------------------------------------------------------------- #
# 默认区域池（collector mock 采样 & region_heat 兜底共用）
# --------------------------------------------------------------------------- #
DEFAULT_REGIONS = ("中国", "美国", "日本", "欧洲")


def stable_hash(s: str) -> int:
    """跨进程可复现的稳定哈希（内置 hash() 受 PYTHONHASHSEED 影响，禁用于落盘/复现场景）。"""
    return int.from_bytes(hashlib.md5(s.encode("utf-8")).digest()[:8], "big")

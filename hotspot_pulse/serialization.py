"""
serialization.py — 把 PipelineResult（及任意契约对象）序列化为可存档的 JSON。

为什么需要：PipelineResult 内部是 dataclass（含 Enum、datetime、dict[Enum, float]），
summary 只是给人看的字符串；没有序列化就无法存档、回放、对接下游。
实现：通用递归转换，纯标准库；不改动 models.py 的契约定义。

约定：
  dataclass → dict（字段名 → 转换值）
  Enum      → .value
  datetime  → ISO-8601 字符串（含时区偏移）
  dict      → key 一律转字符串（Enum key 取 .value）
  set/tuple → list
  其余（str/int/float/bool/None）原样
"""

from __future__ import annotations

import json
import logging
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


def to_jsonable(obj: Any) -> Any:
    """把任意契约对象递归转换为 JSON 可写结构。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {_key_str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    # 兜底：未知类型退化为字符串，不让存档失败
    return str(obj)


def _key_str(key: Any) -> str:
    return key.value if isinstance(key, Enum) else str(key)


def result_to_dict(result) -> dict:
    """PipelineResult → dict（json.loads 可回读的结构）。"""
    return to_jsonable(result)


def write_report(result, path: str) -> str:
    """把完整结果写成 JSON 文件，返回实际路径。"""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result_to_dict(result), fh, ensure_ascii=False, indent=2)
    logger.info("完整结果已序列化写入 %s", path)
    return path

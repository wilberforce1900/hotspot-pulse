#!/usr/bin/env python3
"""
main.py — HotSpot Pulse 命令行入口。

用法：
    python main.py "AI芯片 24小时 中国"
    python main.py "AI芯片"                 # 缺省参数
    pip install . && hotspot "AI芯片"        # 安装后可用 hotspot 命令

输入语法见 stages/input_parser.py 的 parse_user_input 注释：
    裸词第一个 = 主 tag；N小时 = 窗口；地域白名单；key:value 支持 window/horizon/region 等。
"""

from __future__ import annotations

import asyncio
import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    raw = " ".join(argv).strip() or "AI芯片"

    from config import load_config
    from stages.orchestrator import run_pipeline

    cfg = load_config("")  # 空路径 → 默认 mock + LINEAR
    result = asyncio.run(run_pipeline(raw, cfg))

    print(result.summary)
    if result.warnings:
        print("\n[数据质量/降级提示]", file=sys.stderr)
        for w in result.warnings:
            print(f"  · {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

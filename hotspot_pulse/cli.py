"""
cli.py — HotSpot Pulse 命令行入口。

用法：
    hotspot "AI芯片 24小时 中国"
    python main.py "AI芯片"            # 根目录薄壳入口，转到这里
    pip install . && hotspot "AI芯片"

输入语法见 stages/input_parser.py 的 parse_user_input 注释：
    裸词第一个 = 主 tag；N小时 = 窗口；地域白名单；key:value 支持 window/horizon/region 等。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from hotspot_pulse.config import load_config
from hotspot_pulse.stages.orchestrator import run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hotspot",
        description="HotSpot Pulse — 热点流量预测与情绪分析（离线 MOCK 预演）",
    )
    parser.add_argument("query", nargs="*", default=["AI芯片"],
                        help='分析请求，如 "AI芯片 24小时 中国"（缺省 "AI芯片"）')
    parser.add_argument("--config", default="",
                        help="JSON 配置文件路径（缺省=内置 mock 配置）")
    args = parser.parse_args(argv)

    raw = " ".join(args.query).strip() or "AI芯片"
    cfg = load_config(args.config)

    result = asyncio.run(run_pipeline(raw, cfg))

    print(result.summary)
    if result.warnings:
        print("\n[数据质量/降级提示]", file=sys.stderr)
        for w in result.warnings:
            print(f"  · {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

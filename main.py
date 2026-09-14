#!/usr/bin/env python3
"""main.py — 兼容薄壳：转发到 hotspot_pulse.cli（保留 `python main.py` 用法）。"""

from hotspot_pulse.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
每日一键：依次执行
  1. get_info/market_data_fetcher.py  — 消息面 + 资金面 JSON
  2. get_info/spider.py               — 雪球情绪面 JSON（需 Playwright / Chrome 配置）
  3. analyze_and_push.py               — 千问生成日报 → daily_report/

用法（在项目根目录）:
  python run_daily.py

依赖：请从仓库根目录运行，保证 output_info、api_keys.local.json、get_info 配置路径正确。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _reconfigure_stdio_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    _reconfigure_stdio_utf8()

    parser = argparse.ArgumentParser(description="每日一键：抓取数据 + 生成日报")
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="跳过 market_data_fetcher（仅用已有 output_info）",
    )
    parser.add_argument(
        "--skip-spider",
        action="store_true",
        help="跳过 spider（仅用已有 today_xueqiu.json）",
    )
    parser.add_argument(
        "--skip-analyze",
        action="store_true",
        help="只抓取数据，不调用千问写日报",
    )
    args = parser.parse_args()

    steps: list[tuple[str, Path]] = []
    if not args.skip_fetch:
        steps.append(("市场数据 (akshare)", ROOT / "get_info" / "market_data_fetcher.py"))
    if not args.skip_spider:
        steps.append(("雪球爬虫 (Playwright)", ROOT / "get_info" / "spider.py"))
    if not args.skip_analyze:
        steps.append(("千问日报", ROOT / "analyze_and_push.py"))

    if not steps:
        print("[ERROR] 未选择任何步骤，请去掉部分 --skip-*")
        return 1

    print("=" * 50)
    print("run_daily：开始执行当日流水线")
    print("=" * 50)

    for label, script in steps:
        if not script.is_file():
            print(f"[ERROR] 找不到脚本: {script}")
            return 1

        print("\n" + "=" * 50)
        print(f">>> {label}: {script.relative_to(ROOT)}")
        print("=" * 50)

        r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT))
        if r.returncode != 0:
            print(f"\n[ERROR] {script.name} 退出码 {r.returncode}，流水线中止。")
            return r.returncode

    print("\n" + "=" * 50)
    print("run_daily：全部步骤已完成。")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""构建回测数据集：拉取近 3 年前复权日线（bt_klines）与沪深300 基准（bt_index）。

用法：
    python scripts/build_backtest_data.py                # 全量（已缓存充足的自动跳过）
    python scripts/build_backtest_data.py --limit 50     # 只拉前 50 只（联调用）
    python scripts/build_backtest_data.py --force        # 忽略缓存全部重拉
    python scripts/build_backtest_data.py --start 2023-09-15

说明：本脚本只写 bt_klines / bt_index 两张回测专用表，不触碰扫描用的
klines 表，因此对看板既有功能零影响。脚本可反复执行（断点续传）。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db  # noqa: E402
from app.backtest import data as bt_data  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="构建回测数据集")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（联调）")
    parser.add_argument("--force", action="store_true", help="忽略缓存全部重拉")
    parser.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--workers", type=int, default=None, help="并发线程数")
    args = parser.parse_args()

    if args.workers:
        config.BT_FETCH_WORKERS = args.workers

    conn = db.connect()
    codes = None
    if args.limit:
        from app.db import load_stocks
        stocks = load_stocks(conn)
        codes = sorted(c for c in stocks if c.startswith(config.KEEP_CODE_PREFIXES))
        codes = codes[: args.limit]
        print(f"仅处理前 {len(codes)} 只（联调模式）")

    report = bt_data.build_bt_data(
        conn,
        codes=codes,
        start_date=args.start,
        end_date=args.end,
        force=args.force,
        progress=lambda m: print(m, flush=True),
    )
    print("\n=== 数据现状 ===")
    for k, v in bt_data.bt_data_status(conn).items():
        print(f"  {k}: {v}")
    conn.close()
    return 0 if report["fetched"] or report["skipped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

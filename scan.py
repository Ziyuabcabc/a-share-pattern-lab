#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行扫描入口。

用法示例：
    python scan.py                     # 常规扫描（盘后运行，增量更新）
    python scan.py --with-chips        # 同时抓取筹码集中度（耗时显著增加）
    python scan.py --limit 20          # 仅处理预筛后前 20 只（调试用）
    python scan.py --full-refresh      # 忽略缓存，全量重拉K线
    python scan.py --no-export         # 不导出 CSV
    python scan.py --serve             # 扫描完成后启动本地 API 服务

扫描完成后输出统计：高匹配分数量、中匹配分数量、总候选数。
本工具仅做历史数据统计与形态匹配度打分，不提供任何操作指引；
历史数据不等于未来表现。
"""

from __future__ import annotations

import argparse
import logging
import sys

from app import config, db, scanner


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="scan.py",
        description="A股历史形态匹配研究看板 - 全市场扫描（本地运行，仅供研究）",
    )
    p.add_argument("--with-chips", action="store_true",
                   help="抓取筹码集中度（接口缺失时对应项自动计 0 分）")
    p.add_argument("--limit", type=int, default=None,
                   help="仅处理预筛后前 N 只个股（调试用）")
    p.add_argument("--full-refresh", action="store_true",
                   help="忽略本地 K 线缓存，全量重拉")
    p.add_argument("--no-export", action="store_true", help="不导出 CSV")
    p.add_argument("--serve", action="store_true", help="扫描完成后启动本地 API 服务")
    return p.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args(argv)

    print("=" * 64)
    print("A股历史形态匹配研究看板 · 全市场扫描（本地运行版）")
    print("仅做历史数据统计与技术指标计算，历史数据不等于未来表现")
    print("=" * 64)

    stats = scanner.run_scan(
        progress=print,
        with_chips=args.with_chips,
        limit=args.limit,
        full_refresh=args.full_refresh,
    )

    print("-" * 64)
    print(f"扫描批次      : #{stats['run_id']}")
    print(f"预筛后处理    : {stats['total_scanned']} 只")
    print(f"总候选数      : {stats['candidates']}")
    print(f"高匹配分(≥{config.HIGH_SCORE_THRESHOLD}) : {stats['high_count']}")
    print(f"中匹配分(≥{config.MID_SCORE_THRESHOLD}) : {stats['mid_count']}")
    if args.with_chips:
        print(f"筹码数据成功  : {stats['chip_fetched']} 只（缺失项已计 0 分）")
    if stats["errors"]:
        print(f"处理失败      : {stats['errors']} 只（已跳过，不影响整体）")

    if not args.no_export:
        with db.get_conn() as conn:
            items = db.query_results(conn, run_id=stats["run_id"], limit=100000)
        path = scanner.export_csv(items)
        print(f"CSV 已导出    : {path}")

    if args.serve:
        print(f"启动本地 API 服务: http://{config.API_HOST}:{config.API_PORT}/docs")
        from app.main import launch

        launch()
    return 0


if __name__ == "__main__":
    sys.exit(main())

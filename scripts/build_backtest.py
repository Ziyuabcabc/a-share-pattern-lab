# -*- coding: utf-8 -*-
"""执行历史回测并落库。

用法：
    python scripts/build_backtest.py                     # 用默认参数跑全量
    python scripts/build_backtest.py --step 20           # 指定调仓间隔（交易日）
    python scripts/build_backtest.py --start 2023-09-15 --end 2026-09-15
    python scripts/build_backtest.py --horizons 5,10,20  # 指定持仓周期

前置：先运行 scripts/build_backtest_data.py 准备近 3 年日线数据。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.backtest import engine as bt_engine  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="执行历史回测")
    parser.add_argument("--start", default=None, help="观察日起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="观察窗口结束日期 YYYY-MM-DD")
    parser.add_argument("--step", type=int, default=20, help="调仓间隔（交易日）")
    parser.add_argument("--horizons", default="5,10,20", help="持仓周期，逗号分隔")
    args = parser.parse_args()

    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    params = bt_engine.BacktestParams(
        start_date=args.start, end_date=args.end, step=args.step, horizons=horizons,
    )

    conn = db.connect()
    summary = bt_engine.run_backtest(
        conn, params=params, progress=lambda m: print(m, flush=True))
    conn.close()

    print("\n=== 回测摘要 ===")
    print(f"区间       {summary['start_date']} ~ {summary['end_date']}")
    print(f"调仓点     {summary['rebalance_points']} 个（每 {summary['step']} 个交易日）")
    print(f"股票数     {summary['stock_count']}")
    print(f"样本数     {summary['trade_count']}")
    print(f"可复现满分 {summary['score_max']}")
    print("\n=== 分档绩效 ===")
    header = f"{'分组':<10}{'周期':>4}{'样本':>8}{'胜率':>9}{'平均收益':>10}{'超额':>10}{'回撤':>10}{'盈亏比':>9}"
    print(header)
    def fmt(v):
        return "—" if v is None else f"{v * 100:.2f}%"

    for s in summary["stats"]:
        pl = "—" if s["pl_ratio"] is None else f"{s['pl_ratio']:.2f}"
        line = (f"{s['group']:<10}{s['horizon']:>4}{s['samples']:>8}"
                f"{fmt(s['win_rate']):>9}{fmt(s['avg_return']):>10}"
                f"{fmt(s['excess_return']):>10}{fmt(s['max_drawdown']):>10}{pl:>9}")
        print(line)
    print(f"\n耗时 {summary['elapsed_sec']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

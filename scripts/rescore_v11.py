# -*- coding: utf-8 -*-
"""v1.1 全量重算：保留K线缓存，按新打分规则重新计算全部候选分数。

执行内容：
1. 读取本地 K 线缓存的全部股票（不重新拉取K线）
2. 行业取自 stocks 对照表（兜底「其他」）
3. 联网批量获取 PE（腾讯行情）并计算行业内截面分位；失败时该项计 0 分
4. 用 v1.1 三模块规则（总分100）重算分数，写入新扫描批次

用法：
    python scripts/rescore_v11.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, scanner  # noqa: E402
from app.scoring import load_hot_industries  # noqa: E402


def main() -> None:
    hot = load_hot_industries(force=True)
    print(f"打分版本: v1.1（总分100）| 热点行业 {len(hot)} 个: {hot}")
    print()

    with db.get_conn() as conn:
        codes = sorted(db.kline_codes(conn))
        print(f"K线缓存股票: {len(codes)} 只（沿用缓存，不重新拉取）")

        stocks_table = db.load_stocks(conn)

        def industry_of(code: str) -> str:
            row = stocks_table.get(code)
            return (row["industry"] if row else None) or scanner.INDUSTRY_FALLBACK

        industry_by_code = {c: industry_of(c) for c in codes}
        listing_map = scanner._loads(db.meta_get(conn, "listing_days_map"))

        # PE 行业分位（联网，约 1-2 分钟；失败时对应项计 0 分）
        print("拉取 PE 行情并计算行业分位...")
        pe_pct_map, pe_value_map = scanner.prepare_pe_percentiles(
            conn, codes, industry_by_code, progress=print
        )

        run_id = db.create_run(conn)
        print(f"\n重算批次 #{run_id} 启动（v1.1 规则）")

        results: list[dict] = []
        errors = 0
        started = time.time()
        for i, code in enumerate(codes, 1):
            try:
                name_row = stocks_table.get(code)
                name = (name_row["name"] if name_row else "") or code
                listing_days = listing_map.get(code)
                if listing_days is None:
                    first = conn.execute(
                        "SELECT MIN(trade_date) AS d FROM klines WHERE code=?", (code,)
                    ).fetchone()
                    if first and first["d"]:
                        from datetime import datetime

                        listing_days = (
                            datetime.now()
                            - datetime.strptime(str(first["d"]), "%Y-%m-%d")
                        ).days
                chip = db.latest_chip(conn, code)
                item = scanner.compute_stock(
                    conn, code, name, None, listing_days, chip=chip,
                    industry=industry_by_code.get(code),
                    pe=pe_value_map.get(code),
                    pe_percentile=pe_pct_map.get(code),
                    use_kline_turnover=True,
                )
            except Exception as exc:  # noqa: BLE001
                errors += 1
                print(f"  [跳过] {code}: {exc}")
                continue
            if item:
                item["industry"] = industry_by_code.get(code, scanner.INDUSTRY_FALLBACK)
                results.append(item)

            if i % 500 == 0:
                conn.commit()
                elapsed = time.time() - started
                print(f"  进度 {i}/{len(codes)}（耗时 {elapsed:.0f}s）")

        results.sort(key=lambda r: (-r["total_score"], r["code"]))
        high = sum(1 for r in results if r["total_score"] >= config.HIGH_SCORE_THRESHOLD)
        mid = sum(
            1 for r in results
            if config.MID_SCORE_THRESHOLD <= r["total_score"] < config.HIGH_SCORE_THRESHOLD
        )
        db.save_results(conn, run_id, results)
        db.finish_run(
            conn, run_id, "success", len(codes), len(results), high, mid,
            message=f"rescore v1.1 errors={errors}",
        )
        conn.commit()
        print(
            f"\n重算完成：批次 #{run_id}，候选 {len(results)}，"
            f"高匹配分(≥{config.HIGH_SCORE_THRESHOLD}) {high}，"
            f"中匹配分(≥{config.MID_SCORE_THRESHOLD}) {mid}，失败 {errors}"
        )


if __name__ == "__main__":
    main()

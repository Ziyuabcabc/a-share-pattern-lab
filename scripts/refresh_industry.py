#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行业对照表刷新工具：重建「代码-行业」映射并回写 stocks 表。

用途：行业分类源更新（如新增数据源、口径调整）后，无需重跑整轮扫描，
只刷新行业字段并输出覆盖情况，便于自查「其他」占比。

用法：
    python scripts/refresh_industry.py

覆盖优先级：申万一级（主源）> 东财行业板块 > 新浪行业板块 > 北交所名单
（证监会行业口径映射为申万一级）。全部未命中才落在「其他」。
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import data_source, db  # noqa: E402


def main() -> int:
    print("[1/3] 拉取行业分类（申万一级主源 + 东财/新浪/北交所补缺）…")
    industry_map = data_source.fetch_industry_map()
    if not industry_map:
        print("行业分类拉取失败：所有数据源均不可用，保持本地缓存不变。")
        return 1

    with db.get_conn() as conn:
        db.meta_set(conn, "industry_map_v2", _dumps(industry_map))
        db.meta_set(
            conn, "industry_map_v2_updated_at",
            datetime.now().isoformat(timespec="seconds"),
        )

        print("[2/3] 回写 stocks 表行业字段…")
        rows = conn.execute("SELECT code FROM stocks").fetchall()
        updates = [
            (industry_map.get(str(r["code"])) or "其他", str(r["code"]))
            for r in rows
        ]
        conn.executemany("UPDATE stocks SET industry=? WHERE code=?", updates)

        print("[3/3] 覆盖情况：")
        dist = Counter({
            r["industry"] or "其他": r["n"]
            for r in conn.execute(
                "SELECT industry, COUNT(*) AS n FROM stocks GROUP BY industry"
            )
        })
        total = sum(dist.values())
        other = dist.get("其他", 0)
        print(f"    stocks 总数 {total}，行业类别 {len(dist)}，"
              f"「其他」{other} 只（{other / total * 100:.2f}%）")
        for name, n in dist.most_common(8):
            print(f"      {name:<10}{n:>6}")

    print("完成。下次扫描将直接使用刷新后的行业字段（无需重新抓取K线）。")
    return 0


def _dumps(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":
    raise SystemExit(main())

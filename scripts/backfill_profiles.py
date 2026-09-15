# -*- coding: utf-8 -*-
"""历史候选池数据回填：行业/板块字段补全，禁止空值。

执行内容：
1. 独立拉取「代码-名称-行业-板块」基础信息（行业主源为申万一级行业成分，
   东财/新浪行业补缺；代码名称来自新浪全市场列表，均与K线接口无关），
   全量落库 stocks 对照表；
2. 回填 scan_results 全部历史批次：
   - 行业：优先新行业映射（申万），匹配不到回退旧值，最终标注「其他」
   - 板块：按代码前缀重新判定（60 沪主板 / 00 深主板 / 30 创业板 / 68 科创板）

用法：
    python scripts/backfill_profiles.py           # 24h 内有缓存则复用行业映射
    python scripts/backfill_profiles.py --refresh # 强制重新拉取行业映射
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import data_source, db, scanner  # noqa: E402


def main() -> None:
    with db.get_conn() as conn:
        # 1. 独立基础信息对照表（代码-名称-行业-板块）
        # 行业映射带 meta 缓存（v2 = 申万一级主源，24 小时内直接复用）
        cached_at = db.meta_get(conn, "industry_map_v2_updated_at")
        industry_map = None
        if cached_at and "--refresh" not in sys.argv:
            delta = datetime.now() - datetime.fromisoformat(cached_at)
            if delta.total_seconds() < 24 * 3600:
                industry_map = scanner._loads(db.meta_get(conn, "industry_map_v2"))
                if industry_map:
                    print(f"行业映射使用本地缓存（更新于 {cached_at}，"
                          f"覆盖 {len(industry_map)} 只）")
        if not industry_map:
            print("拉取行业分类（申万一级主源 + 东财/新浪补缺）...")
            industry_map = data_source.fetch_industry_map()
            db.meta_set(conn, "industry_map_v2", scanner._dumps(industry_map))
            db.meta_set(
                conn, "industry_map_v2_updated_at",
                datetime.now().isoformat(timespec="seconds"),
            )
        print(f"  行业映射覆盖 {len(industry_map)} 只 / "
              f"{len(set(industry_map.values()))} 个行业")

        print("拉取全市场代码与名称（新浪列表）...")
        spot = data_source.fetch_spot()
        rows = []
        for _, r in spot.iterrows():
            code = str(r["code"])
            rows.append(
                {
                    "code": code,
                    "name": str(r.get("name") or ""),
                    "board": scanner.board_of(code),
                    "industry": industry_map.get(code) or scanner.INDUSTRY_FALLBACK,
                    "listing_days": None,
                }
            )
        db.upsert_stocks(conn, rows)
        conn.commit()
        print(f"  stocks 对照表已写入 {len(rows)} 只")

        profile = db.load_stocks(conn)

        def _profile_industry(code: str) -> str | None:
            """对照表行业；「其他」视为未匹配，允许新行业映射升级覆盖。"""
            value = profile[code]["industry"] if code in profile else None
            return None if value == scanner.INDUSTRY_FALLBACK else value

        # 2. 回填历史扫描结果：行业补齐 + 板块按前缀重算
        batches = conn.execute(
            "SELECT run_id, COUNT(*) AS n FROM scan_results GROUP BY run_id"
        ).fetchall()
        for batch in batches:
            run_id, total = batch["run_id"], batch["n"]
            fixed_industry = 0
            fixed_board = 0
            for row in conn.execute(
                "SELECT id, code, industry, board FROM scan_results WHERE run_id=?",
                (run_id,),
            ).fetchall():
                code = str(row["code"])
                # 优先采用新行业映射（申万一級主源，保证分类体系统一），
                # 映射缺失时回退旧对照表（「其他」视为未匹配），仍无则标注「其他」
                new_industry = (
                    industry_map.get(code)
                    or _profile_industry(code)
                    or scanner.INDUSTRY_FALLBACK
                )
                new_board = scanner.board_of(code)
                updates = {}
                if row["industry"] != new_industry:
                    updates["industry"] = new_industry
                    if not row["industry"] or row["industry"] == scanner.INDUSTRY_FALLBACK:
                        fixed_industry += 1
                if row["board"] != new_board:
                    updates["board"] = new_board
                    fixed_board += 1
                if updates:
                    sets = ", ".join(f"{k}=?" for k in updates)
                    conn.execute(
                        f"UPDATE scan_results SET {sets} WHERE id=?",
                        (*updates.values(), row["id"]),
                    )
            conn.commit()
            print(f"  批次 #{run_id}（{total} 只）："
                  f"行业补齐/升级 {fixed_industry}，板块标签更新 {fixed_board}")

        # 3. 校验：不允许任何空值残留
        for col in ("industry", "board"):
            nulls = conn.execute(
                f"SELECT COUNT(*) AS c FROM scan_results "
                f"WHERE {col} IS NULL OR {col}=''"
            ).fetchone()["c"]
            assert nulls == 0, f"{col} 仍有 {nulls} 条空值"
        print("校验通过：行业/板块字段无空值")


if __name__ == "__main__":
    main()

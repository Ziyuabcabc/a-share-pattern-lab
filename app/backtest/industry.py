# -*- coding: utf-8 -*-
"""分行业回测（v1.7.0）：按申万一级行业维度拆分已有回测结果。

设计意图
--------
v1.6 的回测在全部行业内**混合统计**，无法回答「形态特征的区分能力是否
由少数行业驱动」。本模块对**已有的回测明细**（``bt_trades``）做二次聚合，
按申万一级行业逐一拆分，不重新计算指标、不改变任何打分口径，因此：

1. 与 v1.6 的分档绩效表**同源可对账**——把全部行业的三组样本合并起来，
   数值必然回到 v1.6 的整体分档统计；
2. 可直接在既有回测批次上回算（``scripts/build_v17_analysis.py``），
   无需重跑回测。

指标口径（与整体分档统计完全一致，均为纯统计量）
------------------------------------------------
- 上涨胜率：行业内该组样本收益率严格大于 0 的比例
- 平均收益率：行业内该组样本收益率的算术平均
- 最大回撤：行业内该组按调仓观察点先等权聚合、再复利累积成净值序列后，
  自历史峰值的最大回落幅度（负值）

**样本量提示**：行业内再按三组拆分后，单格样本数会显著变小。低于
``config.IND_MIN_SAMPLES`` 的组合仍如实展示数值，但标记 ``enough=False``，
前端与简报据此提示「样本不足」，避免据小样本下结论。
"""

from __future__ import annotations

import logging
import sqlite3

from app import config
from app.backtest import data as bt_data
from app.backtest import engine as bt_engine
from app.backtest import stats as bt_stats

logger = logging.getLogger(__name__)

# bt_trades 明细表实际落库的持仓周期列（超出该范围无法回算）
HORIZON_COLUMNS = {5: "ret_5", 10: "ret_10", 20: "ret_20"}
UNKNOWN_INDUSTRY = "未分类"


def supported_horizons(horizons) -> list[int]:
    """过滤出 ``bt_trades`` 中有对应列的持仓周期。"""
    return [int(h) for h in horizons if int(h) in HORIZON_COLUMNS]


# ---------------------------------------------------------------------------
# 行业全集
# ---------------------------------------------------------------------------
def industry_universe(conn: sqlite3.Connection) -> list[str]:
    """全部申万一级行业（以 stocks 表为准，并入回测明细中出现过的行业）。

    用全集而非「有样本的行业」，是为了让覆盖率可核对——没有样本的行业
    会以空值出现在结果中，而不是被静默丢弃。

    ``config.INDUSTRY_PLACEHOLDERS`` 中的值表示「未能匹配到申万一级行业」
    （见 ``app/scanner.INDUSTRY_FALLBACK``），不属于任何一级行业分类，排除。
    """
    names: set[str] = set()
    for sql in (
        "SELECT DISTINCT industry FROM stocks WHERE industry IS NOT NULL AND industry <> ''",
        "SELECT DISTINCT industry FROM bt_trades WHERE industry IS NOT NULL AND industry <> ''",
    ):
        try:
            names.update(str(r[0]) for r in conn.execute(sql) if r[0])
        except sqlite3.OperationalError:  # 表尚未建立
            continue
    names -= set(config.INDUSTRY_PLACEHOLDERS)
    return sorted(names)


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------
def _placeholder_filter(col: str = "industry") -> tuple[str, list[str]]:
    """构造「排除占位行业」的 SQL 片段与参数。

    占位值表示该个股未能匹配到任何申万一级行业（见
    ``app/scanner.INDUSTRY_FALLBACK``），不属于行业分类体系。若不排除，
    它会被当成一个真实行业计入 ``covered_count``，出现「覆盖 32 / 共 31」
    这类自相矛盾的覆盖率。与 :func:`industry_universe` 保持同一口径。
    """
    items = [str(x) for x in config.INDUSTRY_PLACEHOLDERS if str(x)]
    if not items:
        return "", []
    marks = ",".join("?" for _ in items)
    return f" AND {col} NOT IN ({marks})", items


def _aggregate_cells(conn: sqlite3.Connection, run_id: int, col: str) -> dict:
    """按（行业 × 分组）聚合样本数 / 上涨数 / 收益合计。"""
    ph_sql, ph_args = _placeholder_filter()
    sql = (
        f"SELECT industry, group_name, COUNT({col}) AS n, "
        f"SUM(CASE WHEN {col} > 0 THEN 1 ELSE 0 END) AS wins, "
        f"SUM({col}) AS total FROM bt_trades "
        f"WHERE run_id=? AND {col} IS NOT NULL "
        f"AND industry IS NOT NULL AND industry <> ''"
        f"{ph_sql} "
        f"GROUP BY industry, group_name"
    )
    out: dict[tuple[str, str], dict] = {}
    for r in conn.execute(sql, (run_id, *ph_args)):
        n = int(r["n"] or 0)
        if n <= 0:
            continue
        out[(str(r["industry"]), str(r["group_name"]))] = {
            "samples": n,
            "win_rate": float(r["wins"] or 0) / n,
            "avg_return": float(r["total"] or 0.0) / n,
        }
    return out


def _aggregate_periods(conn: sqlite3.Connection, run_id: int, col: str) -> dict:
    """按（行业 × 分组 × 观察日）求等权平均收益，供最大回撤复利使用。"""
    ph_sql, ph_args = _placeholder_filter()
    sql = (
        f"SELECT industry, group_name, obs_date, AVG({col}) AS r FROM bt_trades "
        f"WHERE run_id=? AND {col} IS NOT NULL "
        f"AND industry IS NOT NULL AND industry <> ''"
        f"{ph_sql} "
        f"GROUP BY industry, group_name, obs_date ORDER BY obs_date"
    )
    out: dict[tuple[str, str], list[float]] = {}
    for r in conn.execute(sql, (run_id, *ph_args)):
        out.setdefault((str(r["industry"]), str(r["group_name"])), []).append(
            float(r["r"] or 0.0))
    return out


def compute_rows(conn: sqlite3.Connection, run_id: int, horizons) -> list[dict]:
    """计算全部（行业 × 分组 × 周期）的绩效行。"""
    hs = supported_horizons(horizons)
    rows: list[dict] = []
    for h in hs:
        col = HORIZON_COLUMNS[h]
        cells = _aggregate_cells(conn, run_id, col)
        periods = _aggregate_periods(conn, run_id, col)
        for (industry, group), cell in cells.items():
            nav = bt_stats.build_nav(periods.get((industry, group), []))
            rows.append({
                "industry": industry,
                "group": group,
                "horizon": h,
                "samples": cell["samples"],
                "win_rate": bt_stats.safe_float(cell["win_rate"], 6),
                "avg_return": bt_stats.safe_float(cell["avg_return"], 6),
                "max_drawdown": bt_stats.safe_float(bt_stats.max_drawdown(nav), 6),
            })
    return rows


# ---------------------------------------------------------------------------
# 落库 / 读取
# ---------------------------------------------------------------------------
def save_rows(conn: sqlite3.Connection, run_id: int, rows: list[dict]) -> int:
    """写入 bt_industry（先清空该批次旧结果，保证幂等）。"""
    bt_data.ensure_tables(conn)
    conn.execute("DELETE FROM bt_industry WHERE run_id=?", (run_id,))
    if not rows:
        conn.commit()
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO bt_industry (run_id, industry, group_name, horizon, "
        "samples, win_rate, avg_return, max_drawdown) VALUES (?,?,?,?,?,?,?,?)",
        [(run_id, r["industry"], r["group"], r["horizon"], r["samples"],
          r["win_rate"], r["avg_return"], r["max_drawdown"]) for r in rows],
    )
    conn.commit()
    return len(rows)


def load_rows(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """读取已落库的分行业绩效行。"""
    bt_data.ensure_tables(conn)
    out: list[dict] = []
    for r in conn.execute(
        "SELECT industry, group_name, horizon, samples, win_rate, avg_return, "
        "max_drawdown FROM bt_industry WHERE run_id=? "
        "ORDER BY industry, horizon, group_name", (run_id,)
    ):
        out.append({
            "industry": str(r["industry"]),
            "group": str(r["group_name"]),
            "horizon": int(r["horizon"]),
            "samples": int(r["samples"] or 0),
            "win_rate": r["win_rate"],
            "avg_return": r["avg_return"],
            "max_drawdown": r["max_drawdown"],
        })
    return out


# ---------------------------------------------------------------------------
# 汇总视图
# ---------------------------------------------------------------------------
def build_summary(conn: sqlite3.Connection, run_id: int, horizons,
                  rows: list[dict] | None = None) -> dict:
    """组装接口与简报共用的分行业回测视图。"""
    hs = supported_horizons(horizons)
    if rows is None:
        rows = load_rows(conn, run_id)

    universe = industry_universe(conn)
    by_industry: dict[str, dict[str, dict]] = {}
    for r in rows:
        by_industry.setdefault(r["industry"], {}).setdefault(
            str(r["horizon"]), {})[r["group"]] = r

    # 排名：以最长持仓周期（默认 20 日）高匹配分组的平均收益率为准。
    # 缺该格数据、或样本数不足的行业不参与排名，单独列入 insufficient。
    rank_h = config.IND_RANK_HORIZON if config.IND_RANK_HORIZON in hs else (
        max(hs) if hs else None)
    ranking: list[dict] = []
    insufficient: list[str] = []
    for industry in universe:
        cell = (by_industry.get(industry, {}).get(str(rank_h), {})
                .get("high")) if rank_h is not None else None
        if cell is None or cell.get("avg_return") is None:
            insufficient.append(industry)
            continue
        if cell["samples"] < config.IND_MIN_SAMPLES:
            insufficient.append(industry)
            continue
        ranking.append({
            "industry": industry,
            "samples": cell["samples"],
            "win_rate": cell["win_rate"],
            "avg_return": cell["avg_return"],
            "max_drawdown": cell["max_drawdown"],
        })
    ranking.sort(key=lambda x: x["avg_return"], reverse=True)
    n = config.IND_TOP_N

    # 效果最好 / 最差的行业。两者必须**互不重叠**，否则同一行业会同时出现在
    # 「最好」与「最差」两处，读起来自相矛盾。参与排名的行业不足 2n 个时，
    # 改为对半拆分（前半为相对靠前、后半为相对靠后），不再强行各取 n 个。
    if len(ranking) >= 2 * n:
        top, bottom = ranking[:n], ranking[-n:][::-1]
    else:
        half = max(1, len(ranking) // 2)
        top, bottom = ranking[:half], ranking[half:][::-1]

    # 高匹配分组样本的行业集中度：用于解释「为何参与排名的行业数少于行业总数」。
    # 形态匹配分的高分档依赖热点行业与成长板块加分，样本天然向少数行业聚集。
    total_high = sum(x["samples"] for x in ranking) or 1
    top3 = sum(x["samples"] for x in ranking[:3])
    concentration = {
        "industries_with_samples": len(ranking),
        "industries_total": len(universe),
        "top3_share": round(top3 / total_high, 4),
        "top_industries": [x["industry"] for x in ranking[:3]],
    }

    covered = len(by_industry)
    return {
        "available": bool(rows),
        "run_id": run_id,
        "horizons": hs,
        "rank_horizon": rank_h,
        "min_samples": config.IND_MIN_SAMPLES,
        "industry_count": len(universe),
        "covered_count": covered,
        "expected_count": config.IND_EXPECTED_COUNT,
        "universe": universe,
        "rows": rows,
        "by_industry": by_industry,
        "ranking": ranking,
        "top": top,
        "bottom": bottom,
        "insufficient": insufficient,
        "concentration": concentration,
        "groups": [{"key": g, "label": bt_engine.GROUP_LABELS[g]}
                   for g in bt_engine.GROUP_ORDER],
    }

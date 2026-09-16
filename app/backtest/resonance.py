# -*- coding: utf-8 -*-
"""多周期共振策略（v1.7.0）：日线形态 + 周线形态双重确认。

策略定义
--------
在原有**单日线**策略（日线形态进入高匹配分组）之上，叠加**周线级别**的
同源形态约束，构造「日线 + 周线双共振」分组：

- 日线侧：观察日 T 的形态匹配分 ≥ ``config.RESONANCE_DAILY_THRESHOLD``（60 分）
- 周线侧：最近一根**已收盘**周线上，MACD(3,6,3) 金叉且红柱大于 0，
  且 KDJ(9,3,3) 金叉且 J 小于 100

两项同时成立才计入双共振分组。周线指标与日线**完全同源**——同一套
``app.indicators`` 实现、同一组参数，不做任何参数替换。

周线合成口径
------------
按自然周（ISO 周）由日线合成：开盘取当周首个交易日、收盘取当周最后一个
交易日、最高 / 最低取周内极值、成交量取周内合计。停牌周按实际有成交的
交易日合成，不补零。

未来函数规避（本模块的核心约束）
--------------------------------
**周线的「已收盘」必须以市场交易日历判定，而不是个股自身的最后成交日。**
若个股在周内后段停牌，用个股自身的最后成交日会把该周误判为「已收盘」，
从而在周中就用上了一个尚未定型的周线。因此本模块：

1. 由基准指数日线构造市场交易日历，据此确定每个 ISO 周的**市场周最后交易日**；
2. 某周线只有在 ``市场周最后交易日 <= 观察日 T`` 时才允许被使用，
   保证该周的全部交易日都不晚于 T，周线形态已定型；
3. 观察日 T 处于周中时，使用的是**上一根**已收盘周线，而非当周未完成数据。

这样处理下，周线侧与日线侧一样只依赖 T 及之前的信息。
"""

from __future__ import annotations

import logging
import sqlite3
from bisect import bisect_right

import numpy as np
import pandas as pd

from app import config, db
from app.backtest import data as bt_data
from app.backtest import stats as bt_stats
from app.indicators import kdj, macd

logger = logging.getLogger(__name__)

RESONANCE_KEY = "resonance"
RESONANCE_LABEL = "日线 + 周线双共振分组"
DAILY_HIGH_LABEL = "单日线高匹配分组"

RESONANCE_WEIGHTS = (
    ("日线", f"形态匹配分 ≥ {config.RESONANCE_DAILY_THRESHOLD} 分（高匹配分组）"),
    ("周线", "MACD(3,6,3) 金叉且红柱 &gt; 0"),
    ("周线", "KDJ(9,3,3) 金叉且 J &lt; 100"),
)


# ---------------------------------------------------------------------------
# 市场交易日历 → 每个 ISO 周的「市场周最后交易日」
# ---------------------------------------------------------------------------
def week_end_calendar(conn: sqlite3.Connection) -> dict[tuple[int, int], str]:
    """由基准指数日线构造 {（ISO 年, ISO 周）: 该周最后一个交易日}。"""
    df = bt_data.load_index_series(conn)
    if df.empty:
        return {}
    ts = pd.to_datetime(df["trade_date"])
    iso = ts.dt.isocalendar()
    out: dict[tuple[int, int], str] = {}
    # 指数日线已按日期升序，逐条覆盖即可保证留下的是当周最后一个交易日
    for key, d in zip(zip(iso["year"].to_numpy(), iso["week"].to_numpy()),
                      df["trade_date"].tolist()):
        out[(int(key[0]), int(key[1]))] = str(d)
    return out


# ---------------------------------------------------------------------------
# 周线合成与指标
# ---------------------------------------------------------------------------
def weekly_bars(df: pd.DataFrame) -> pd.DataFrame:
    """日线 → 周线（ISO 自然周），返回按周升序的 DataFrame。

    输出列：iso_year、iso_week、open、high、low、close、volume、bars。
    停牌周按实际交易日合成，不补零；``bars`` 记录该周实际交易日数，
    供后续判断周线是否「完整」（样本过少的周形态不可靠）。
    """
    if df.empty:
        return pd.DataFrame()
    ts = pd.to_datetime(df["trade_date"])
    iso = ts.dt.isocalendar()
    work = df.copy()
    work["iso_year"] = iso["year"].to_numpy()
    work["iso_week"] = iso["week"].to_numpy()
    grouped = work.groupby(["iso_year", "iso_week"], sort=True)
    out = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        bars=("trade_date", "size"),
    ).reset_index()
    return out


def weekly_flags(wk: pd.DataFrame) -> np.ndarray:
    """周线共振标记：MACD(3,6,3) 金叉且红柱 > 0，KDJ(9,3,3) 金叉且 J < 100。"""
    if wk.empty:
        return np.zeros(0, dtype=bool)
    close = wk["close"].astype(float)
    high = wk["high"].astype(float)
    low = wk["low"].astype(float)
    dif, dea, hist = macd(close)
    k, d, j = kdj(high, low, close)
    return ((dif > dea) & (hist > 0) & (k > d) & (j < 100)).to_numpy(dtype=bool)


def _usable_from(wk: pd.DataFrame, cal: dict) -> list[str | None]:
    """每根周线的「可用起始日」= 该周的市场周最后交易日。

    未能在市场日历中定位到的周（例如指数数据缺失）标记为 None，不可用。
    """
    out: list[str | None] = []
    for y, w in zip(wk["iso_year"].to_numpy(), wk["iso_week"].to_numpy()):
        out.append(cal.get((int(y), int(w))))
    return out


# ---------------------------------------------------------------------------
# 打标记
# ---------------------------------------------------------------------------
def compute_flags(conn: sqlite3.Connection, run_id: int) -> list[tuple]:
    """为回测批次内的每一条样本计算周线共振标记。

    返回 [(run_id, obs_date, code, weekly_ok)]，仅供内部落库使用。
    """
    cal = week_end_calendar(conn)
    if not cal:
        raise RuntimeError("基准指数日线为空，无法构造周线日历")

    trades = conn.execute(
        "SELECT code, obs_date FROM bt_trades WHERE run_id=? ORDER BY code, obs_date",
        (run_id,)).fetchall()
    if not trades:
        return []

    by_code: dict[str, list[str]] = {}
    for r in trades:
        by_code.setdefault(str(r["code"]), []).append(str(r["obs_date"]))

    panel = bt_data.load_bt_panel(conn)
    rows: list[tuple] = []
    missing = 0
    for code, obs_dates in by_code.items():
        df = panel.get(code)
        if df is None or len(df) < config.WEEKLY_MIN_BARS:
            missing += 1
            rows.extend((run_id, d, code, 0) for d in obs_dates)
            continue
        wk = weekly_bars(df)
        if len(wk) < config.WEEKLY_MIN_BARS:
            missing += 1
            rows.extend((run_id, d, code, 0) for d in obs_dates)
            continue
        flags = weekly_flags(wk)
        usable = _usable_from(wk, cal)
        # 只保留可用日非空的周线，并按可用日升序排列（周线本身已按周升序，
        # 但市场日历缺失的周会造成空洞，这里显式过滤后再排序）
        pairs = [(u, bool(f)) for u, f in zip(usable, flags) if u is not None]
        if not pairs:
            missing += 1
            rows.extend((run_id, d, code, 0) for d in obs_dates)
            continue
        pairs.sort(key=lambda x: x[0])
        dates = [p[0] for p in pairs]
        vals = [1 if p[1] else 0 for p in pairs]

        # 观察日 T → 最近一根「可用日 <= T」的周线
        for d in obs_dates:
            idx = bisect_right(dates, d) - 1
            rows.append((run_id, d, code, vals[idx] if idx >= 0 else 0))
    if missing:
        logger.info("周线共振：%d 只个股因周线数据不足标记为 0", missing)
    return rows


def save_flags(conn: sqlite3.Connection, run_id: int, rows: list[tuple]) -> int:
    """写入 bt_resonance（先清空该批次旧结果，保证幂等）。"""
    bt_data.ensure_tables(conn)
    conn.execute("DELETE FROM bt_resonance WHERE run_id=?", (run_id,))
    chunk = 20000
    for i in range(0, len(rows), chunk):
        conn.executemany(
            "INSERT OR REPLACE INTO bt_resonance (run_id, obs_date, code, weekly_ok) "
            "VALUES (?,?,?,?)", rows[i:i + chunk])
    conn.commit()
    return len(rows)


def mark_resonance(conn: sqlite3.Connection, run_id: int, progress=None) -> dict:
    """计算并落库周线共振标记。"""
    if progress:
        progress("周线共振：构造市场周线日历…")
    rows = compute_flags(conn, run_id)
    if progress:
        progress(f"周线共振：标记 {len(rows)} 条样本，写入数据库…")
    n = save_flags(conn, run_id, rows)
    hit = sum(1 for r in rows if r[3])
    return {"rows": n, "weekly_hit": hit,
            "weekly_hit_rate": round(hit / n, 4) if n else None}


# ---------------------------------------------------------------------------
# 读取与统计
# ---------------------------------------------------------------------------
def load_flags(conn: sqlite3.Connection, run_id: int) -> dict[tuple[str, str], bool]:
    """读取 {(obs_date, code): weekly_ok}。"""
    out: dict[tuple[str, str], bool] = {}
    for r in conn.execute(
        "SELECT obs_date, code, weekly_ok FROM bt_resonance WHERE run_id=?", (run_id,)
    ):
        out[(str(r["obs_date"]), str(r["code"]))] = bool(r["weekly_ok"])
    return out


def _stats_from_returns(arr: np.ndarray, periods: list[float]) -> dict:
    """由样本收益与按期组合收益汇总绩效（口径与整体分档统计一致）。"""
    nav = bt_stats.build_nav(periods)
    arr = bt_stats._clean(arr)
    return {
        "samples": int(arr.size),
        "win_rate": bt_stats.safe_float(bt_stats.win_rate(arr), 6),
        "avg_return": bt_stats.safe_float(bt_stats.avg_return(arr), 6),
        "max_drawdown": bt_stats.safe_float(bt_stats.max_drawdown(nav), 6),
    }


def compute_stats(conn: sqlite3.Connection, run_id: int, horizons) -> list[dict]:
    """计算双共振分组与单日线高分组的绩效，并给出差异。"""
    from app.backtest import industry as bt_industry

    hs = bt_industry.supported_horizons(horizons)
    flags = load_flags(conn, run_id)
    out: list[dict] = []
    for h in hs:
        col = bt_industry.HORIZON_COLUMNS[h]
        # 双共振：日线高匹配分组 且 周线共振
        res_ret, res_per, daily_ret, daily_per = [], [], [], []
        sql = (f"SELECT obs_date, code, group_name, {col} AS r FROM bt_trades "
               f"WHERE run_id=? AND {col} IS NOT NULL")
        for r in conn.execute(sql, (run_id,)):
            d, code = str(r["obs_date"]), str(r["code"])
            group = str(r["group_name"])
            val = float(r["r"])
            if group == "high":
                daily_ret.append(val)
                daily_per.append((d, val))
                if flags.get((d, code)):
                    res_ret.append(val)
                    res_per.append((d, val))

        def _period_mean(rows_in: list[tuple[str, float]]) -> list[float]:
            """按观察日等权聚合成组合收益序列（升序）。"""
            bucket: dict[str, list[float]] = {}
            for d, v in rows_in:
                bucket.setdefault(d, []).append(v)
            return [float(np.mean(bucket[d])) for d in sorted(bucket)]

        res = _stats_from_returns(np.array(res_ret, dtype=float), _period_mean(res_per))
        daily = _stats_from_returns(np.array(daily_ret, dtype=float), _period_mean(daily_per))
        diff = {}
        for key in ("win_rate", "avg_return", "max_drawdown"):
            a, b = res.get(key), daily.get(key)
            diff[key] = (bt_stats.safe_float(a - b, 6)
                         if (a is not None and b is not None) else None)
        out.append({
            "horizon": h,
            "resonance": {**res, "label": RESONANCE_LABEL},
            "daily_high": {**daily, "label": DAILY_HIGH_LABEL},
            "diff": diff,
        })
    return out


def build_summary(conn: sqlite3.Connection, run_id: int, horizons) -> dict:
    """接口与简报共用的共振对比视图。"""
    from app.backtest import industry as bt_industry

    hs = bt_industry.supported_horizons(horizons)
    if not hs:
        return {"available": False, "reason": "当前回测批次没有可统计的持仓周期"}
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM bt_resonance WHERE run_id=?", (run_id,)).fetchone()[0]
    except sqlite3.OperationalError:
        total = 0
    if not total:
        return {"available": False, "run_id": run_id,
                "reason": "尚未计算周线共振标记，请先运行 v1.7 分析构建脚本"}
    rows = compute_stats(conn, run_id, hs)
    if not any(r["resonance"]["samples"] for r in rows):
        return {"available": False, "run_id": run_id,
                "reason": "双共振分组无有效样本"}
    return {
        "available": True,
        "run_id": run_id,
        "horizons": hs,
        "marked_rows": int(total),
        "weekly_hit_rate": round(
            conn.execute("SELECT AVG(weekly_ok) FROM bt_resonance WHERE run_id=?",
                         (run_id,)).fetchone()[0] or 0.0, 4),
        "rule": [{"side": s, "text": x} for s, x in RESONANCE_WEIGHTS],
        "stats": rows,
        "stats_by_horizon": {str(r["horizon"]): r for r in rows},
    }

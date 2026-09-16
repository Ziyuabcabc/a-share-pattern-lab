# -*- coding: utf-8 -*-
"""向量化回测引擎（v1.6.0）：按历史形态匹配分分档，统计各持仓周期的绩效表现。

严格规避未来函数的三条硬约束
----------------------------
1. **观察点判定只用观察日当天及之前的信息**。全部指标（MACD(3,6,3)、KDJ(9,3,3)、
   量比、5 日涨幅、40 日振幅）都在整段序列上用滚动窗口预计算后取 T 日那一格，
   不使用任何全局统计量（全样本均值、全样本分位、未来极值）。
2. **入场价取 T+1 日开盘价**。T 日收盘后才能算出分数，最早只能在下一个
   交易日开盘执行，因此不存在「按当日收盘价成交」这类未来函数。
3. **出场价取 T+n 日收盘价**（n 为持仓周期）。若持有区间内发生停牌导致
   交易日不连续，该样本整体剔除，不做价格前推。

基础过滤与看板一致：剔除 ST/*ST/退市整理、剔除北交所、剔除上市不满 365 天、
剔除停牌（观察日无成交或次日无法交易）。

回测打分口径：形态、板块、行业、5 日涨幅等项与看板 v1.1 完全一致；筹码集中度、
PE 行业分位、换手率三项在历史时点无免费可复现数据，一律计 0 分，故回测可复现
满分 = config.BT_SCORE_MAX（81 分）。分组阈值沿用看板口径 60 / 30。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app import config, db
from app.backtest import data as bt_data
from app.backtest import stats as bt_stats
from app.indicators import kdj, macd
from app.scoring import is_hot_industry

logger = logging.getLogger(__name__)

GROUP_ORDER = ("high", "mid", "low")
GROUP_LABELS = {
    "high": "高匹配分组（≥60 分）",
    "mid": "中匹配分组（30–59 分）",
    "low": "低匹配分组（<30 分）",
}
BENCHMARK_KEY = "benchmark"


def _group_of(score: float) -> str:
    """按综合匹配分归档（阈值与看板 v1.1 完全一致）。"""
    if score >= config.HIGH_SCORE_THRESHOLD:
        return "high"
    if score >= config.MID_SCORE_THRESHOLD:
        return "mid"
    return "low"


@dataclass
class BacktestParams:
    """回测参数（全部有默认值，可整体覆盖）。"""

    start_date: str | None = None
    end_date: str | None = None
    step: int = config.BT_REBALANCE_STEP
    horizons: tuple[int, ...] = config.BT_HORIZONS
    warmup_bars: int = config.BT_WARMUP_BARS


# ---------------------------------------------------------------------------
# 单只股票：滚动窗口预计算 + 逐日得分
# ---------------------------------------------------------------------------
def _prepare_stock(
    df: pd.DataFrame,
    code: str,
    industry: str | None,
    cal_pos: dict[str, int],
    n_cal: int,
) -> dict | None:
    """对单只股票预计算全序列指标与每日得分（一次算完，供所有调仓点复用）。

    关键：所有指标均为**滚动窗口**结果，第 i 格只依赖第 i 格及之前的数据，
    因此后续按任意调仓点取值都不含未来信息。
    """
    n = len(df)
    if n < 20:
        return None

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)
    open_ = df["open"].astype(float)

    dif, dea, hist = macd(close)
    k, d, j = kdj(high, low, close)

    # 近5日均量（不含当日）——与 app/indicators.volume_stats 口径一致
    vol_ma5 = volume.shift(1).rolling(5).mean()
    ratio = volume / vol_ma5

    # 近5日涨幅
    ret5 = close / close.shift(5) - 1.0

    # 近40日区间振幅（含当日）
    hh40 = high.rolling(config.RANGE_LOOKBACK_DAYS).max()
    ll40 = low.rolling(config.RANGE_LOOKBACK_DAYS).min()
    rr = hh40 / ll40.where(ll40 > 0)

    score = np.zeros(n, dtype=np.float64)
    score += config.SCORE_MACD_GOLD * ((dif > dea) & (hist > 0)).to_numpy()
    score += config.SCORE_KDJ_GOLD * ((k > d) & (j < 100)).to_numpy()
    score += config.SCORE_VOLUME_SURGE * (ratio > config.VOLUME_SURGE_RATIO).to_numpy()
    score += config.SCORE_VOLUME_DOUBLE * (ratio > config.VOLUME_DOUBLE_RATIO).to_numpy()
    score += config.SCORE_RANGE_COMPACT * (
        (rr <= config.RANGE_COMPACT_RATIO) & (ll40 > 0)
    ).to_numpy()
    score += config.SCORE_RETURN5_HEALTHY * (
        (ret5 >= config.RETURN5_MIN_PCT / 100.0)
        & (ret5 <= config.RETURN5_MAX_PCT / 100.0)
    ).to_numpy()
    if code.startswith(("30", "68")):
        score += config.SCORE_GROWTH_BOARD
    if is_hot_industry(industry):
        score += config.SCORE_HOT_INDUSTRY
    score = np.rint(score)

    dates = df["trade_date"].tolist()
    bars_cal = np.array([cal_pos.get(d, -1) for d in dates], dtype=np.int32)
    if (bars_cal < 0).any():
        keep = bars_cal >= 0
        if not keep.any():
            return None
        bars_cal = bars_cal[keep]
        open_ = open_[keep]
        close = close[keep]
        volume = volume[keep]
        score = score[keep]
        n = int(keep.sum())
        if n < 20:
            return None

    # 日历位置 → 该股K线下标 的反查表（-1 表示该日该股无K线，即停牌/未上市）
    lut = np.full(n_cal, -1, dtype=np.int32)
    lut[bars_cal] = np.arange(n, dtype=np.int32)

    return {
        "code": code,
        "n": n,
        "bars_cal": bars_cal,
        "lut": lut,
        "open": open_.to_numpy(dtype=float),
        "close": close.to_numpy(dtype=float),
        "volume": volume.to_numpy(dtype=float),
        "score": score,
    }


def _loads(text) -> dict:
    try:
        return json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}


def earliest_valid_date(conn: sqlite3.Connection) -> dict[str, str]:
    """由上市天数缓存反推「上市满 365 天」的最早可入选日期。

    返回 {code: 'YYYY-MM-DD'}，表示该股在该日期及之后才满足上市满 365 天。
    缓存中缺失的股票不在此字典内，调用方降级为K线根数判定。
    """
    raw = _loads(db.meta_get(conn, "listing_days_map"))
    today = datetime.now()
    out: dict[str, str] = {}
    for code, days in raw.items():
        try:
            d = int(days)
        except (TypeError, ValueError):
            continue
        if d <= 0:
            continue
        listing_date = today - timedelta(days=d)
        out[str(code)] = (listing_date + timedelta(days=365)).strftime("%Y-%m-%d")
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_backtest(
    conn: sqlite3.Connection,
    params: BacktestParams | None = None,
    progress=None,
) -> dict:
    """执行一次完整回测并落库，返回结构化摘要。"""
    params = params or BacktestParams()
    bt_data.ensure_tables(conn)
    t0 = time.time()

    def say(msg: str) -> None:
        logger.info(msg)
        if progress:
            progress(msg)

    say("载入回测行情面板…")
    panel = bt_data.load_bt_panel(conn)
    if not panel:
        raise RuntimeError("回测行情数据为空，请先运行 scripts/build_backtest_data.py")

    index_df = bt_data.load_index_series(conn)
    if index_df.empty:
        raise RuntimeError("基准指数数据为空，请先运行 scripts/build_backtest_data.py")

    calendar = list(index_df["trade_date"])
    cal_pos = {d: i for i, d in enumerate(calendar)}
    n_cal = len(calendar)

    # 基础信息：行业用于热点行业加分，名称用于 ST 过滤
    profiles = {
        str(r["code"]): (r["industry"], str(r["name"] or ""))
        for r in conn.execute("SELECT code, industry, name FROM stocks")
    }

    say(f"预计算 {len(panel)} 只股票的滚动窗口指标…")
    stocks: dict[str, dict] = {}
    excluded_st = 0
    for code, df in panel.items():
        industry, name = profiles.get(code, (None, ""))
        # 与看板基础过滤完全一致：剔除 ST / *ST / 退市整理
        # （口径同 scanner 预筛，按最新股票名称判定）
        if any(kw in name.upper() for kw in config.EXCLUDE_NAME_KEYWORDS):
            excluded_st += 1
            continue
        st = _prepare_stock(df, code, industry, cal_pos, n_cal)
        if st is not None:
            stocks[code] = st
    say(f"  可用股票 {len(stocks)} 只（剔除 ST/*ST/退市整理 {excluded_st} 只）")

    horizons = tuple(sorted(params.horizons))
    hmax = max(horizons)

    i0 = bisect_left(calendar, params.start_date) if params.start_date else 0
    i1 = n_cal - hmax - 1
    if params.end_date:
        i1 = min(i1, bisect_right(calendar, params.end_date) - 1)
    if i1 < i0:
        raise RuntimeError("回测区间内可用交易日不足，请检查数据范围或缩短持仓周期")
    obs_idx = list(range(i0, i1 + 1, max(1, params.step)))
    say(f"调仓观察点 {len(obs_idx)} 个（{calendar[i0]} ~ {calendar[i1]}，每 {params.step} 个交易日一个）")

    min_valid = earliest_valid_date(conn)
    idx_open = index_df["open"].to_numpy(dtype=float)
    idx_close = index_df["close"].to_numpy(dtype=float)

    records: list[dict] = []
    period: dict[tuple[str, int], list[tuple[str, float]]] = {
        (g, h): [] for g in GROUP_ORDER for h in horizons
    }
    bench_period: dict[int, list[tuple[str, float]]] = {h: [] for h in horizons}
    skipped_gap = 0

    for i in obs_idx:
        T = calendar[i]
        bench_entry = idx_open[i + 1] if i + 1 < n_cal else np.nan
        bench_entry_ok = bool(np.isfinite(bench_entry) and bench_entry > 0)

        # 先确认基准可用，保证三组与基准在同一批调仓点上可比
        usable = {}
        for h in horizons:
            j = i + h
            usable[h] = bool(
                bench_entry_ok and j < n_cal
                and np.isfinite(idx_close[j]) and idx_close[j] > 0
            )

        point_rows: list[dict] = []
        for code, st in stocks.items():
            bi = int(st["lut"][i])
            if bi < 0:
                continue                                  # 观察日停牌或尚未上市
            if not (st["volume"][bi] > 0):
                continue                                  # 无成交，视为停牌
            if bi + 1 >= st["n"] or st["bars_cal"][bi + 1] != i + 1:
                continue                                  # 次日停牌，无法按开盘价入场
            if bi + 1 < params.warmup_bars:
                continue                                  # 指标预热不足

            mv = min_valid.get(code)
            if mv is not None:
                if T < mv:
                    continue                              # 上市不满 365 天
            elif bi + 1 < config.BT_MIN_LISTING_BARS:
                continue

            entry = float(st["open"][bi + 1])
            if not np.isfinite(entry) or entry <= 0:
                continue

            score = int(st["score"][bi])
            row = {
                "obs_date": T,
                "code": code,
                "score": score,
                "group": _group_of(score),
                "entry_date": calendar[i + 1],
                "entry_price": entry,
            }
            for h in horizons:
                if not usable[h]:
                    row[f"ret_{h}"] = None
                    continue
                if bi + h >= st["n"] or st["bars_cal"][bi + h] != i + h:
                    row[f"ret_{h}"] = None       # 持有期内停牌，交易日不连续，样本剔除
                    skipped_gap += 1
                    continue
                ex = float(st["close"][bi + h])
                row[f"ret_{h}"] = (ex / entry - 1.0) if (np.isfinite(ex) and ex > 0) else None
            point_rows.append(row)

        if not point_rows:
            continue

        # 组合期收益：组内等权平均。
        # 每期对三组与基准同时登记（某组当期无有效样本时记 0），
        # 保证三组净值与基准净值在**同一批调仓点**上逐期严格对齐、可比。
        for h in horizons:
            if not usable[h]:
                continue
            bench_period[h].append((T, float(idx_close[i + h] / bench_entry - 1.0)))
            for grp in GROUP_ORDER:
                vals = [r[f"ret_{h}"] for r in point_rows
                        if r["group"] == grp and r[f"ret_{h}"] is not None]
                period[(grp, h)].append((T, float(np.mean(vals)) if vals else 0.0))

        records.extend(point_rows)

    say(f"样本明细 {len(records)} 条（因区间停牌剔除 {skipped_gap} 个周期样本）")
    if not records:
        raise RuntimeError("回测未产生任何样本，请检查数据完整性")

    df_tr = pd.DataFrame(records)

    # 实际生效的调仓点序列（预热期不足的观察点不产生样本，不计入区间）
    ref_series = period[(GROUP_ORDER[0], horizons[0])]
    effective_points = len(ref_series)
    eff_start = ref_series[0][0] if ref_series else calendar[i0]
    eff_end = ref_series[-1][0] if ref_series else calendar[i1]
    say(f"有效调仓点 {effective_points} 个（{eff_start} ~ {eff_end}）")

    # ---------------- 绩效统计 ----------------
    bench_nav_map: dict[int, tuple[list[str], np.ndarray]] = {}
    for h in horizons:
        dates = [d for d, _ in bench_period[h]]
        rets = [r for _, r in bench_period[h]]
        bench_nav_map[h] = (dates, bt_stats.build_nav(rets))

    stats_rows: list[dict] = []
    equity_rows: list[dict] = []
    summary_stats: list[dict] = []
    equity_out: dict[str, dict[str, list[dict]]] = {g: {} for g in GROUP_ORDER}

    for h in horizons:
        b_dates, b_nav = bench_nav_map[h]
        b_by_date = {d: float(b_nav[k]) for k, d in enumerate(b_dates)}
        for grp in GROUP_ORDER:
            series = period[(grp, h)]
            if not series:
                continue
            dates = [d for d, _ in series]
            rets = [r for _, r in series]
            nav = bt_stats.build_nav(rets)
            # 基准净值对齐到该组的调仓点序列，保证同期可比
            bench_aligned = np.array(
                [b_by_date.get(d, np.nan) for d in dates], dtype=float)
            if np.isfinite(bench_aligned).any():
                first = bench_aligned[np.isfinite(bench_aligned)][0]
                bench_aligned = np.where(np.isfinite(bench_aligned),
                                         bench_aligned, first)
            else:
                bench_aligned = np.ones_like(nav)

            mask = df_tr["group"] == grp
            sub = df_tr[mask]
            col = f"ret_{h}"
            arr = pd.to_numeric(sub[col], errors="coerce").dropna().to_numpy(dtype=float)

            stat = bt_stats.summarize_group(arr, rets, nav, bench_aligned)
            stat["group"] = grp
            stat["horizon"] = h
            stat["avg_score"] = float(sub["score"].mean()) if len(sub) else None
            stats_rows.append(stat)
            summary_stats.append({
                "group": grp,
                "group_label": GROUP_LABELS[grp],
                "horizon": h,
                "samples": stat["samples"],
                "win_rate": bt_stats.safe_float(stat["win_rate"], 4),
                "avg_return": bt_stats.safe_float(stat["avg_return"], 6),
                "median_return": bt_stats.safe_float(stat["median_return"], 6),
                "excess_return": bt_stats.safe_float(stat["excess_return"], 6),
                "max_drawdown": bt_stats.safe_float(stat["max_drawdown"], 6),
                "pl_ratio": bt_stats.safe_float(stat["pl_ratio"], 4),
                "avg_win": bt_stats.safe_float(stat["avg_win"], 6),
                "avg_loss": bt_stats.safe_float(stat["avg_loss"], 6),
                "total_return": bt_stats.safe_float(stat["total_return"], 6),
                "benchmark_return": bt_stats.safe_float(stat["benchmark_return"], 6),
                "avg_score": bt_stats.safe_float(stat["avg_score"], 2),
            })

            eq: list[dict] = []
            for k, d in enumerate(dates):
                nav_k = float(nav[k])
                b_k = float(bench_aligned[k]) if np.isfinite(bench_aligned[k]) else None
                excess = (nav_k / b_k) if (b_k and b_k > 0) else None
                eq.append({
                    "date": d,
                    "nav": round(nav_k, 6),
                    "excess_nav": round(excess, 6) if excess else None,
                })
                equity_rows.append({
                    "run_id": None, "group_name": grp, "horizon": h,
                    "obs_date": d, "nav": round(nav_k, 6),
                    "excess_nav": round(excess, 6) if excess else None,
                })
            equity_out[grp][str(h)] = eq

    # 基准净值曲线（供前端叠加对比）
    benchmark_out: dict[str, list[dict]] = {}
    for h in horizons:
        dates, nav = bench_nav_map[h]
        benchmark_out[str(h)] = [
            {"date": d, "nav": round(float(nav[k]), 6)} for k, d in enumerate(dates)
        ]
        for k, d in enumerate(dates):
            equity_rows.append({
                "run_id": None, "group_name": BENCHMARK_KEY, "horizon": h,
                "obs_date": d, "nav": round(float(nav[k]), 6), "excess_nav": 1.0,
            })

    # ---------------- 落库 ----------------
    run_id = _save_run(conn, params, stocks, records, stats_rows, equity_rows, df_tr)
    say(f"回测结果已落库 run_id={run_id}")

    summary = {
        "available": True,
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "start_date": eff_start,
        "end_date": eff_end,
        "step": params.step,
        "horizons": list(horizons),
        "rebalance_points": effective_points,
        "planned_points": len(obs_idx),
        "stock_count": len(stocks),
        "excluded_st": excluded_st,
        "obs_count": len(records),
        "trade_count": len(df_tr),
        "score_max": config.BT_SCORE_MAX,
        "score_thresholds": {
            "high": config.HIGH_SCORE_THRESHOLD,
            "mid": config.MID_SCORE_THRESHOLD,
        },
        "benchmark": {
            "symbol": config.BT_INDEX_SYMBOL,
            "name": config.BT_INDEX_NAME,
        },
        "groups": [
            {"key": g, "label": GROUP_LABELS[g]} for g in GROUP_ORDER
        ],
        "stats": summary_stats,
        "stats_by_horizon": {
            str(h): [s for s in summary_stats if s["horizon"] == h] for h in horizons
        },
        "equity": equity_out,
        "benchmark_equity": benchmark_out,
        "unavailable_items": [
            {"key": k, "points": p, "reason": r}
            for k, p, r in config.BT_UNAVAILABLE_ITEMS
        ],
        "method_notes": _method_notes(),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    # 摘要整体落库：接口与简报直接读取，避免每次请求重算。
    # 同时用实际生效区间覆盖落库的起止日期（入参可能为空）。
    conn.execute(
        "UPDATE bt_runs SET summary=?, start_date=?, end_date=?, obs_count=? WHERE id=?",
        (json.dumps(summary, ensure_ascii=False), eff_start, eff_end,
         len(records), run_id),
    )
    conn.commit()
    return summary


def _method_notes() -> list[str]:
    """写进接口与简报的方法学说明（保证结论可复现）。"""
    return [
        f"回测区间：近 {config.BT_CALENDAR_YEARS} 年，价格口径为前复权日线。",
        f"调仓频率：每 {config.BT_REBALANCE_STEP} 个交易日形成一个观察截面，"
        f"使 {config.BT_HORIZONS[0]}/{config.BT_HORIZONS[1]}/{config.BT_HORIZONS[2]} "
        "三个持仓周期的样本区间互不重叠，避免重叠样本造成的自相关。",
        "观察点判定仅使用观察日当天及之前的滚动窗口数据；入场价取次一交易日开盘价，"
        "出场价取持有期满当日收盘价，全程不使用未来信息。",
        f"基础过滤与看板一致：剔除 ST/*ST/退市整理、剔除北交所、剔除上市不满 365 天、"
        f"剔除停牌个股。上市日由上市天数缓存反推，缺失时降级为K线根数判定。",
        f"筹码集中度（8 分）、PE 行业分位（6 分）、换手率（5 分）三项在历史时点"
        f"无免费可复现数据，统一计 0 分，故回测可复现满分 {config.BT_SCORE_MAX} 分，"
        f"分组阈值沿用看板口径 {config.HIGH_SCORE_THRESHOLD} / {config.MID_SCORE_THRESHOLD} 分。",
        "行业归属使用当前申万一级行业映射，未回溯历史行业变更，属轻微前视，"
        "因行业分类变动频率低，对结论影响有限。",
        "ST 状态按最新股票名称判定，未回溯历史 ST 变更记录。",
        "超额收益 = 分组区间累计收益率 − 沪深300 同期累计收益率。",
        "最大回撤基于分组等权组合的逐期复利净值序列计算。",
        "历史数据统计结果不代表未来表现。",
    ]


def _save_run(
    conn: sqlite3.Connection,
    params: BacktestParams,
    stocks: dict,
    records: list[dict],
    stats_rows: list[dict],
    equity_rows: list[dict],
    df_tr: pd.DataFrame,
) -> int:
    """把回测结果写入 bt_runs / bt_stats / bt_equity / bt_trades。"""
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO bt_runs (created_at, start_date, end_date, step, horizons, "
        "stock_count, obs_count, score_max, params, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now, params.start_date, params.end_date, params.step,
         ",".join(str(h) for h in params.horizons), len(stocks), len(records),
         config.BT_SCORE_MAX,
         json.dumps({"step": params.step,
                     "horizons": list(params.horizons),
                     "warmup_bars": params.warmup_bars}, ensure_ascii=False),
         "success"),
    )
    run_id = int(cur.lastrowid)

    conn.executemany(
        "INSERT OR REPLACE INTO bt_stats (run_id, group_name, horizon, samples, "
        "win_rate, avg_return, median_return, excess_return, max_drawdown, pl_ratio, "
        "avg_win, avg_loss, total_return, avg_score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(run_id, s["group"], s["horizon"], s["samples"], s["win_rate"],
          s["avg_return"], s["median_return"], s["excess_return"], s["max_drawdown"],
          s["pl_ratio"], s["avg_win"], s["avg_loss"], s["total_return"],
          s["avg_score"]) for s in stats_rows],
    )

    conn.executemany(
        "INSERT OR REPLACE INTO bt_equity (run_id, group_name, horizon, obs_date, "
        "nav, excess_nav) VALUES (?,?,?,?,?,?)",
        [(run_id, r["group_name"], r["horizon"], r["obs_date"],
          r["nav"], r["excess_nav"]) for r in equity_rows],
    )

    names = {str(r["code"]): (r["name"], r["board"], r["industry"])
             for r in conn.execute("SELECT code, name, board, industry FROM stocks")}
    trade_rows = []
    for r in records:
        name, board, industry = names.get(r["code"], (None, None, None))
        trade_rows.append((
            run_id, r["obs_date"], r["code"], name, board, industry,
            r["score"], r["group"], r["entry_date"], r["entry_price"],
            r.get("ret_5"), r.get("ret_10"), r.get("ret_20"),
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO bt_trades (run_id, obs_date, code, name, board, "
        "industry, score, group_name, entry_date, entry_price, ret_5, ret_10, ret_20) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        trade_rows,
    )
    conn.commit()
    return run_id


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def latest_bt_run(conn: sqlite3.Connection):
    """最近一次成功的回测批次。"""
    return conn.execute(
        "SELECT * FROM bt_runs WHERE status='success' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def load_bt_stats(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """读取回测绩效统计（分档 × 周期）。"""
    rows = conn.execute(
        "SELECT * FROM bt_stats WHERE run_id=? ORDER BY horizon, group_name", (run_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["group_label"] = GROUP_LABELS.get(d["group_name"], d["group_name"])
        out.append(d)
    return out


def load_bt_equity(conn: sqlite3.Connection, run_id: int) -> dict:
    """读取净值曲线，返回 {group: {horizon: [{date, nav, excess_nav}]}}。"""
    rows = conn.execute(
        "SELECT group_name, horizon, obs_date, nav, excess_nav FROM bt_equity "
        "WHERE run_id=? ORDER BY horizon, obs_date", (run_id,)
    ).fetchall()
    out: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        g = str(r["group_name"])
        h = str(r["horizon"])
        out.setdefault(g, {}).setdefault(h, []).append({
            "date": r["obs_date"], "nav": r["nav"], "excess_nav": r["excess_nav"],
        })
    return out

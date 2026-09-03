# -*- coding: utf-8 -*-
"""全市场扫描编排器：数据更新 -> 指标计算 -> 基础过滤 -> 打分 -> 落库/导出。

流程说明：
1. 刷新股票列表 / 上市天数 / 行业分类（带时效缓存，避免重复拉取）
2. 拉取全市场行情快照（单次请求），先做轻量预筛（板块代码、ST/退、停牌）
3. 逐股维护日K缓存（增量追加当日K线；检测到前复权漂移时全量重拉）
4. 基于 K 线计算 MACD/KDJ/均线等指标，构建快照后执行基础过滤与打分
5. 可选：对初筛后匹配分>0 的个股抓取筹码集中度并重算（缺失计 0 分）
6. 结果写入 SQLite，支持导出 CSV；全程数据缺失只降级、不中断
"""

from __future__ import annotations

import csv
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable

import pandas as pd

from app import config, data_source, db
from app.indicators import hist_peak_stats, kdj, macd, ma, volume_stats
from app.scoring import StockSnapshot, base_filter_reject_reason, derive_display_fields, evaluate

logger = logging.getLogger(__name__)

BOARD_LABELS = {
    "60": "沪市主板",
    "68": "科创板",
    "00": "深市主板",
    "30": "创业板",
}


def board_of(code: str) -> str:
    return BOARD_LABELS.get(code[:2], "其他")


# ---------------------------------------------------------------------------
# 股票列表 / 行业 / 上市天数 维护
# ---------------------------------------------------------------------------
def refresh_universe(conn) -> dict[str, pd.Series]:
    """刷新并返回全市场快照（预筛前）。行业与上市天数按缓存时效更新。"""
    spot = data_source.fetch_spot()
    spot["code"] = spot["code"].astype(str)

    # 行业分类：超过缓存时效才重新拉取（约 86 次请求）
    if _stale(conn, "industry_updated_at", config.INDUSTRY_CACHE_HOURS):
        try:
            industry_map = data_source.fetch_industry_map()
            db.meta_set(conn, "industry_map", _dumps(industry_map))
            db.meta_set(
                conn, "industry_updated_at", datetime.now().isoformat(timespec="seconds")
            )
            logger.info("行业分类已刷新：%s 个行业", len(set(industry_map.values())))
        except data_source.DataSourceError as exc:
            logger.warning("行业分类刷新失败，沿用本地缓存: %s", exc)

    # 上市天数：带 7 天缓存，失败时降级为 K 线窗口估计
    if _stale(conn, "listing_days_updated_at", config.INDUSTRY_CACHE_HOURS):
        listing = data_source.fetch_listing_dates()
        if listing:
            db.meta_set(conn, "listing_days_map", _dumps(listing))
            db.meta_set(
                conn, "listing_days_updated_at",
                datetime.now().isoformat(timespec="seconds"),
            )

    return spot


def _stale(conn, key: str, hours: float) -> bool:
    """meta 时间戳判断：不存在或超过 hours 小时即视为过期。"""
    stamp = db.meta_get(conn, key)
    if not stamp:
        return True
    delta = datetime.now() - datetime.fromisoformat(stamp)
    return delta.total_seconds() > hours * 3600


def _dumps(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def _loads(text: str | None) -> dict:
    import json

    try:
        return json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# 单股 K 线缓存维护（增量）
#
# 线程模型：SQLite 连接仅允许主线程读写；
# plan_kline_fetch（只读）在主线程决定抓取方式，网络请求在
# 线程池执行，结果统一回主线程落库，避免并发写同一连接。
# ---------------------------------------------------------------------------
def plan_kline_fetch(conn, code: str, spot_row: pd.Series, full_refresh: bool = False):
    """决定单股K线更新方式，返回 ("full",) | ("append", row_dict) | ("skip",)。

    - 无缓存或显式全量刷新：全量拉取近 config.HIST_CALENDAR_DAYS 自然日前复权日K
    - 有缓存：优先用行情快照直接构造当日K线（避免逐股历史请求）；
      若快照「昨收」与缓存最近收盘价出现超容差漂移（多为除权除息导致的
      前复权价跳变），则改为全量重拉以保持口径统一
    - 若快照收盘与缓存最近收盘一致且昨收与次近收盘一致，视为未产生新K线
    """
    last = db.last_kline(conn, code)
    if full_refresh or last is None:
        return ("full",)

    close = _f(spot_row.get("close"))
    prev_close = _f(spot_row.get("prev_close"))
    last_close = float(last["close"])
    prev_cached = conn.execute(
        "SELECT close FROM klines WHERE code=? ORDER BY trade_date DESC LIMIT 1 OFFSET 1",
        (code,),
    ).fetchone()

    tol = config.QFQ_DRIFT_TOLERANCE
    same_as_last = close is not None and abs(close - last_close) <= tol * max(last_close, 1e-9)
    same_prev = (
        prev_close is not None
        and prev_cached is not None
        and abs(prev_close - float(prev_cached["close"])) <= tol * max(float(prev_cached["close"]), 1e-9)
    )
    if same_as_last and same_prev:
        return ("skip",)  # 尚未产生新K线（盘前/快照仍为上一交易日）

    if prev_close is not None and abs(prev_close - last_close) > tol * max(last_close, 1e-9):
        return ("full",)  # 复权因子变化，全量重拉

    trade_date = datetime.now().strftime("%Y-%m-%d")
    row = {
        "code": code,
        "trade_date": trade_date,
        "open": _f(spot_row.get("open")),
        "high": _f(spot_row.get("high")),
        "low": _f(spot_row.get("low")),
        "close": close,
        "volume": _f(spot_row.get("volume")),
        "amount": _f(spot_row.get("amount")),
        "turnover_rate": _f(spot_row.get("turnover_rate")),
    }
    return ("append", row)


def fetch_full_klines(code: str) -> list[dict]:
    """线程池内执行：全量拉取单股前复权日K并整理为落库行（无数据库操作）。"""
    start = (datetime.now() - pd.Timedelta(days=config.HIST_CALENDAR_DAYS)).strftime("%Y%m%d")
    df = data_source.fetch_hist(code, start, datetime.now().strftime("%Y%m%d"))
    return [
        {
            "code": code,
            "trade_date": str(r["trade_date"]),
            "open": _f(r.get("open")), "high": _f(r.get("high")),
            "low": _f(r.get("low")), "close": _f(r.get("close")),
            "volume": _f(r.get("volume")), "amount": _f(r.get("amount")),
            "turnover_rate": _f(r.get("turnover_rate")),
        }
        for _, r in df.iterrows()
    ]


def _f(v) -> float | None:
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 指标计算与打分
# ---------------------------------------------------------------------------
def compute_stock(conn, code: str, name: str, spot_row: pd.Series,
                  listing_days: int | None, chip: float | None) -> dict | None:
    """计算单股指标并打分。返回 None 表示被基础过滤剔除或数据不足。"""
    rows = db.load_klines(conn, code)
    if len(rows) < 30:  # 指标预热样本不足（含上市天数过滤的情形）
        return None

    df = pd.DataFrame(
        [
            {
                "open": float(r["open"] or 0), "high": float(r["high"] or 0),
                "low": float(r["low"] or 0), "close": float(r["close"] or 0),
                "volume": float(r["volume"] or 0), "amount": float(r["amount"] or 0),
            }
            for r in rows
        ]
    )

    dif_s, dea_s, hist_s = macd(df["close"])
    k_s, d_s, j_s = kdj(df["high"], df["low"], df["close"])
    vol_prev_mean, vol_today = volume_stats(df["volume"])

    amount_today = float(df["amount"].iloc[-1])
    if amount_today <= 0:
        return None  # 停牌或无成交

    snapshot = StockSnapshot(
        code=code,
        name=name,
        listing_days=listing_days,
        amount=amount_today,
        turnover_rate=_f(spot_row.get("turnover_rate")),
        dif=float(dif_s.iloc[-1]),
        dea=float(dea_s.iloc[-1]),
        hist=float(hist_s.iloc[-1]),
        k=float(k_s.iloc[-1]),
        d=float(d_s.iloc[-1]),
        j=float(j_s.iloc[-1]),
        volume_prev_mean=vol_prev_mean,
        volume_today=vol_today,
        chip_concentration=chip,
        range_high=float(df["high"].iloc[-config.RANGE_LOOKBACK_DAYS:].max()),
        range_low=float(df["low"].iloc[-config.RANGE_LOOKBACK_DAYS:].min()),
    )
    if base_filter_reject_reason(snapshot):
        return None

    bd = evaluate(snapshot)
    display = derive_display_fields(
        hist_s, df["high"], df["low"], df["volume"],
        snapshot.turnover_rate, snapshot.j,
    )
    return {
        "code": code,
        "name": name,
        "board": board_of(code),
        "pattern_score": bd.pattern_score,
        "bonus_score": bd.bonus_score,
        "total_score": bd.total_score,
        "breakdown": bd.to_dict(),
        "metrics": {
            "close": float(df["close"].iloc[-1]),
            "dif": snapshot.dif, "dea": snapshot.dea, "hist": snapshot.hist,
            "k": snapshot.k, "d": snapshot.d, "j": snapshot.j,
            "volume_today": snapshot.volume_today,
            "volume_prev_mean": snapshot.volume_prev_mean,
            "chip_concentration": chip,
        },
        "display": display,
    }


# ---------------------------------------------------------------------------
# 全市场扫描
# ---------------------------------------------------------------------------
def run_scan(
    progress: Callable[[str], None] | None = None,
    with_chips: bool = False,
    limit: int | None = None,
    full_refresh: bool = False,
) -> dict:
    """执行全市场扫描，返回运行统计。progress 用于 CLI/接口输出进度。"""
    def say(msg: str) -> None:
        logger.info(msg)
        if progress:
            progress(msg)

    with db.get_conn() as conn:
        run_id = db.create_run(conn)
        say(f"扫描 #{run_id} 启动")

        # 1. 股票列表与行业/上市天数
        spot = refresh_universe(conn)
        industry_map = _loads(db.meta_get(conn, "industry_map"))
        listing_map = _loads(db.meta_get(conn, "listing_days_map"))

        # 2. 轻量预筛：板块代码 / ST与退市整理 / 停牌
        candidates: list[tuple[str, pd.Series]] = []
        for _, row in spot.iterrows():
            code = row["code"]
            if not code.startswith(config.KEEP_CODE_PREFIXES):
                continue
            name = str(row.get("name") or "")
            if any(kw in name.upper() for kw in config.EXCLUDE_NAME_KEYWORDS):
                continue
            amount = _f(row.get("amount"))
            if not amount or amount <= 0:
                continue
            candidates.append((code, row))
        if limit:
            candidates = candidates[:limit]
        say(f"预筛后待计算 {len(candidates)} 只（全市场快照 {len(spot)} 条）")

        # 3. K线缓存维护：主线程规划 -> 线程池抓取 -> 主线程落库 -> 主线程计算
        results: list[dict] = []
        errors = 0
        plans: dict[str, tuple] = {}
        for code, row in candidates:
            plans[code] = plan_kline_fetch(conn, code, row, full_refresh=full_refresh)

        full_codes = [c for c, p in plans.items() if p[0] == "full"]
        say(f"全量拉取 {len(full_codes)} 只，快照直填 {sum(1 for p in plans.values() if p[0] == 'append')} 只")

        fetched: dict[str, list[dict]] = {}
        if full_codes:
            with ThreadPoolExecutor(max_workers=config.FETCH_WORKERS) as pool:
                futures = {pool.submit(fetch_full_klines, c): c for c in full_codes}
                done = 0
                for fut in as_completed(futures):
                    done += 1
                    if done % 500 == 0:
                        say(f"K线拉取进度 {done}/{len(full_codes)}")
                    code = futures[fut]
                    try:
                        fetched[code] = fut.result()
                    except data_source.DataSourceError as exc:
                        errors += 1
                        logger.warning("K线拉取失败，跳过: %s - %s", code, exc)

        # 落库（主线程）
        for code, rows in fetched.items():
            if rows:
                db.upsert_klines(conn, rows)
        for code, plan in plans.items():
            if plan[0] == "append":
                db.upsert_klines(conn, [plan[1]])

        # 指标计算与打分（主线程，纯 CPU）
        stocks_table = db.load_stocks(conn)
        for code, row in candidates:
            try:
                listing_days = listing_map.get(code)
                if listing_days is None:
                    # 降级估计：以缓存最早K线日期起算（窗口>=365天时等同通过过滤）
                    first = conn.execute(
                        "SELECT MIN(trade_date) AS d FROM klines WHERE code=?", (code,)
                    ).fetchone()
                    if first and first["d"]:
                        listing_days = (
                            datetime.now()
                            - datetime.strptime(str(first["d"]), "%Y-%m-%d")
                        ).days
                    else:
                        listing_days = None
                name = str(row.get("name") or stocks_table.get(code, {"name": ""})["name"])
                stock_row = stocks_table.get(code)
                industry = stock_row["industry"] if stock_row else industry_map.get(code)
                item = compute_stock(conn, code, name, row, listing_days, chip=None)
            except data_source.DataSourceError as exc:
                errors += 1
                logger.warning("个股处理失败，跳过: %s", exc)
                continue
            if item:
                item["industry"] = industry
                results.append(item)

        # 4. 可选筹码集中度：仅对匹配分>0 的个股抓取，随后重算
        chip_fetched = 0
        if with_chips:
            targets = [r for r in results if r["total_score"] > 0]
            say(f"抓取筹码集中度：{len(targets)} 只")
            with ThreadPoolExecutor(max_workers=config.FETCH_WORKERS) as pool:
                futures = {pool.submit(data_source.fetch_chip_concentration, r["code"]): r for r in targets}
                for fut in as_completed(futures):
                    res = fut.result()
                    item = futures[fut]
                    if res:
                        chip_fetched += 1
                        _, value = res
                        db.upsert_chips(conn, [{"code": item["code"], "trade_date": res[0], "concentration": value}])
            # 用最新筹码缓存重算（含此前缺失筹码的个股）
            results = _rescore_with_chips(conn, spot, listing_map, industry_map, results)
        say(f"筹码数据成功抓取 {chip_fetched} 只")

        # 5. 统计与落库
        results.sort(key=lambda r: (-r["total_score"], r["code"]))
        high = sum(1 for r in results if r["total_score"] >= config.HIGH_SCORE_THRESHOLD)
        mid = sum(
            1 for r in results
            if config.MID_SCORE_THRESHOLD <= r["total_score"] < config.HIGH_SCORE_THRESHOLD
        )
        db.save_results(conn, run_id, results)
        db.finish_run(
            conn, run_id, "success", len(candidates), len(results), high, mid,
            message=f"errors={errors}",
        )
        say(
            f"扫描完成：候选 {len(results)}，高匹配分(≥{config.HIGH_SCORE_THRESHOLD}) {high}，"
            f"中匹配分(≥{config.MID_SCORE_THRESHOLD}) {mid}，处理失败 {errors}"
        )
        return {
            "run_id": run_id,
            "total_scanned": len(candidates),
            "candidates": len(results),
            "high_count": high,
            "mid_count": mid,
            "chip_fetched": chip_fetched,
            "errors": errors,
        }


def _rescore_with_chips(conn, spot, listing_map, industry_map, results) -> list[dict]:
    """抓取筹码后，结合本地筹码缓存对全部结果重算一遍分数。"""
    spot_index = spot.set_index("code")
    recomputed: list[dict] = []
    for item in results:
        code = item["code"]
        chip = db.latest_chip(conn, code)
        if chip is None:
            recomputed.append(item)  # 无筹码数据：维持原分数（对应项计 0 分）
            continue
        row = spot_index.loc[code] if code in spot_index.index else None
        if row is None:
            recomputed.append(item)
            continue
        listing_days = listing_map.get(code)
        item2 = compute_stock(
            conn, code, item["name"], row, listing_days, chip=chip
        )
        if item2:
            item2["industry"] = item.get("industry") or industry_map.get(code)
            recomputed.append(item2)
        else:
            recomputed.append(item)
    recomputed.sort(key=lambda r: (-r["total_score"], r["code"]))
    return recomputed


# ---------------------------------------------------------------------------
# CSV 导出
# ---------------------------------------------------------------------------
EXPORT_COLUMNS = [
    "代码", "名称", "板块", "行业",
    "形态匹配分", "增强加分", "综合匹配分",
    "MACD金叉且红柱大于0", "KDJ金叉且J值小于100", "当日放量", "筹码集中度不高于18%",
    "收盘价", "DIF", "DEA", "MACD柱", "K", "D", "J",
    "MACD红柱相对前高收缩", "MACD红柱前高", "当日MACD红柱",
    "筹码集中度", "当日换手率", "近40日最高价", "近40日最低价",
]


def export_csv(results: list[dict], path=None) -> str:
    """将候选池结果导出为 CSV（UTF-8 with BOM，Excel 打开不乱码）。"""
    if path is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = config.OUTPUT_DIR / f"候选池_{stamp}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(EXPORT_COLUMNS)
        writer.writerow(["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
                         config.DISPLAY_FIELD_NOTE, "", "", "", "", "", ""])
        for r in results:
            bd, m, d = r["breakdown"], r["metrics"], r["display"]
            writer.writerow([
                r["code"], r["name"], r.get("board", ""), r.get("industry") or "",
                r["pattern_score"], r["bonus_score"], r["total_score"],
                int(bd["pattern"]["macd_gold_red"]),
                int(bd["pattern"]["kdj_gold_j_under_100"]),
                int(bd["pattern"]["volume_surge"]),
                int(bd["pattern"]["chip_concentrated"]),
                m.get("close"), m.get("dif"), m.get("dea"), m.get("hist"),
                m.get("k"), m.get("d"), m.get("j"),
                int(bool(d.get("macd_hist_shrink"))),
                d.get("macd_hist_peak_prev"), d.get("macd_hist_today"),
                m.get("chip_concentration"), d.get("turnover_rate"),
                d.get("range_high_40d"), d.get("range_low_40d"),
            ])
    return str(path)

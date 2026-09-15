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

# 板块标签统一按代码前缀判定，保证 100% 有值、与前端筛选项一致
BOARD_LABELS = {
    "60": "沪主板",
    "68": "科创板",
    "00": "深主板",
    "30": "创业板",
}

# 行业字段兜底值：对照表与行业映射都匹配不到时统一标注，不允许出现空值
INDUSTRY_FALLBACK = "其他"


def board_of(code: str) -> str:
    """按代码前缀判定板块：60 沪主板 / 00 深主板 / 30 创业板 / 68 科创板。"""
    return BOARD_LABELS.get(code[:2], INDUSTRY_FALLBACK)


# ---------------------------------------------------------------------------
# 股票列表 / 行业 / 上市天数 维护
# ---------------------------------------------------------------------------
def refresh_universe(conn) -> dict[str, pd.Series]:
    """刷新并返回全市场快照（预筛前）。行业与上市天数按缓存时效更新。"""
    spot = data_source.fetch_spot()
    spot["code"] = spot["code"].astype(str)

    # 独立的「代码-名称-行业-板块」基础信息对照表：
    # 行业来自新浪行业板块成分接口（独立于K线/行情接口），板块按代码前缀判定，
    # 全量落库 stocks 表；候选池的行业/板块一律以该表为准。
    if _stale(conn, "stock_profiles_updated_at", config.INDUSTRY_CACHE_HOURS):
        try:
            n = refresh_stock_profiles(conn)
            db.meta_set(
                conn, "stock_profiles_updated_at",
                datetime.now().isoformat(timespec="seconds"),
            )
            logger.info("股票基础信息对照表已刷新：%s 只", n)
        except data_source.DataSourceError as exc:
            logger.warning("股票基础信息对照表刷新失败，沿用本地缓存: %s", exc)

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


def refresh_stock_profiles(conn) -> int:
    """独立拉取并落库全A股「代码-名称-行业-板块」对照表。

    数据来源（均与K线接口无关）：
    - 代码/名称：新浪 hs_a 全市场列表
    - 行业：新浪行业板块成分映射（匹配不到的统一标注「其他」）
    - 板块：按代码前缀判定（60 沪主板 / 00 深主板 / 30 创业板 / 68 科创板）
    """
    industry_map = data_source.fetch_industry_map()
    spot = data_source.fetch_spot()
    # 行业映射同步写入 meta 缓存：扫描打分与历史回填共用，避免重复拉取
    db.meta_set(conn, "industry_map", _dumps(industry_map))
    rows = []
    for _, r in spot.iterrows():
        code = str(r["code"])
        rows.append(
            {
                "code": code,
                "name": str(r.get("name") or ""),
                "board": board_of(code),
                "industry": industry_map.get(code) or INDUSTRY_FALLBACK,
                "listing_days": None,
            }
        )
    db.upsert_stocks(conn, rows)
    conn.commit()
    return len(rows)


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


def append_spot_if_missing(conn, code: str, spot_row: pd.Series) -> None:
    """备用源日K的当日数据可能延迟收录：全量落库后若当日K线缺失，用快照补一根。

    判定条件：缓存最近K线日期不是今天，且快照收盘价与缓存最近收盘价不一致
    （一致说明快照仍停留在上一交易日，不补）。
    """
    last = db.last_kline(conn, code)
    if last is None:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if str(last["trade_date"]) >= today:
        return
    close = _f(spot_row.get("close"))
    if close is None:
        return
    tol = config.QFQ_DRIFT_TOLERANCE
    if abs(close - float(last["close"])) <= tol * max(float(last["close"]), 1e-9):
        return
    db.upsert_klines(
        conn,
        [
            {
                "code": code,
                "trade_date": today,
                "open": _f(spot_row.get("open")),
                "high": _f(spot_row.get("high")),
                "low": _f(spot_row.get("low")),
                "close": close,
                "volume": _f(spot_row.get("volume")),
                "amount": _f(spot_row.get("amount")),
                "turnover_rate": _f(spot_row.get("turnover_rate")),
            }
        ],
    )


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
                  listing_days: int | None, chip: float | None,
                  industry: str | None = None, pe: float | None = None,
                  pe_percentile: float | None = None,
                  use_kline_turnover: bool = False) -> dict | None:
    """计算单股指标并打分（v1.1 三模块，总分 100）。

    返回 None 表示被基础过滤剔除或数据不足。
    use_kline_turnover=True 时换手率取自 K 线缓存（离线重算场景），
    否则取行情快照（实时扫描场景）。
    """
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

    # 近5日涨幅（%）：最新收盘 / 5个交易日前收盘 - 1（样本不足时不计）
    return_5d = None
    if len(df) >= 6 and float(df["close"].iloc[-6]) > 0:
        return_5d = round(
            (float(df["close"].iloc[-1]) / float(df["close"].iloc[-6]) - 1) * 100, 2
        )

    # 换手率来源：K线缓存（离线重算）或行情快照（实时扫描）
    if use_kline_turnover:
        turnover = _f(rows[-1]["turnover_rate"])
    else:
        turnover = _f(spot_row.get("turnover_rate"))

    snapshot = StockSnapshot(
        code=code,
        name=name,
        listing_days=listing_days,
        amount=amount_today,
        turnover_rate=turnover,
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
        industry=industry,
        pe=pe,
        pe_percentile=pe_percentile,
        return_5d_pct=return_5d,
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
            "pe": pe,
            "pe_percentile": pe_percentile,
            "return_5d_pct": return_5d,
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
    boards: list[str] | None = None,
) -> dict:
    """执行全市场/指定板块扫描，返回运行统计。progress 用于 CLI/接口输出进度。

    boards: 需要扫描的板块代码前缀列表（如 ["30", "68"] 表示创业板+科创板），
    None 表示全市场。范围越小请求量越少，适合先小范围验证。
    """
    def say(msg: str) -> None:
        logger.info(msg)
        if progress:
            progress(msg)

    board_label = "+".join(boards) if boards else "全市场"

    with db.get_conn() as conn:
        run_id = db.create_run(conn)
        say(f"扫描 #{run_id} 启动（范围：{board_label}）")

        # 1. 股票列表与行业/上市天数
        spot = refresh_universe(conn)
        industry_map = _loads(db.meta_get(conn, "industry_map"))
        listing_map = _loads(db.meta_get(conn, "listing_days_map"))

        # 2. 轻量预筛：板块代码 / ST与退市整理 / 停牌 / 指定板块范围
        candidates: list[tuple[str, pd.Series]] = []
        for _, row in spot.iterrows():
            code = row["code"]
            if not code.startswith(config.KEEP_CODE_PREFIXES):
                continue
            if boards and not code.startswith(tuple(boards)):
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
                    code = futures[fut]
                    try:
                        rows = fut.result()
                    except data_source.DataSourceError as exc:
                        errors += 1
                        logger.warning("K线拉取失败，跳过: %s - %s", code, exc)
                        continue
                    # 边拉边落库并定期提交：中途异常/中断时已拉取的缓存仍在，
                    # 重跑时 plan_kline_fetch 识别到缓存即走增量路径，无需从头再来
                    if rows:
                        db.upsert_klines(conn, rows)
                        fetched[code] = rows
                    if done % config.DB_COMMIT_EVERY == 0:
                        conn.commit()
                        say(f"K线拉取进度 {done}/{len(full_codes)}")

        # 快照直填当日K线（主线程）
        for code, plan in plans.items():
            if plan[0] == "append":
                db.upsert_klines(conn, [plan[1]])
        # 备用源当日K线可能延迟：全量落库后若当日缺失，用快照补一根
        spot_index = spot.set_index("code")
        for code in fetched:
            if code in spot_index.index:
                append_spot_if_missing(conn, code, spot_index.loc[code])

        # 指标计算与打分（主线程，纯 CPU）；PE 行业分位需联网预取
        stocks_table = db.load_stocks(conn)
        industry_by_code: dict[str, str] = {}
        for code, row in candidates:
            stock_row = stocks_table.get(code)
            # 行业取值优先级：stocks 对照表 > 行业映射缓存 > 兜底「其他」，不允许空值
            industry_by_code[code] = (
                (stock_row["industry"] if stock_row else None)
                or industry_map.get(code)
                or INDUSTRY_FALLBACK
            )

        pe_pct_map, pe_value_map = prepare_pe_percentiles(
            conn, [c for c, _ in candidates], industry_by_code, progress=say
        )

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
                industry = industry_by_code.get(code, INDUSTRY_FALLBACK)
                item = compute_stock(
                    conn, code, name, row, listing_days, chip=None,
                    industry=industry, pe=pe_value_map.get(code),
                    pe_percentile=pe_pct_map.get(code),
                )
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
            results = _rescore_with_chips(
                conn, spot, listing_map, industry_map, results,
                pe_value_map=pe_value_map, pe_pct_map=pe_pct_map,
            )
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


def prepare_pe_percentiles(
    conn, codes: list[str], industry_by_code: dict[str, str],
    progress=None,
) -> tuple[dict[str, float], dict[str, float]]:
    """批量获取 PE 并计算行业内截面分位（v1.1 PE行业分位项）。

    返回 (分位表, PE值表)。分位口径：免费源无行业 3 年历史 PE 序列，
    采用「同行业当日截面分位」近似（行业内有效 PE>0 样本数不足时计 0 分，
    见 config.PE_MIN_INDUSTRY_PEERS）。行情源不可用时返回空表，
    对应项全部计 0 分（设计内降级）。
    """
    try:
        pe_map = data_source.fetch_pe_map(list(codes), progress=progress)
    except Exception as exc:  # noqa: BLE001 —— fetch_pe_map 内部已按批容错
        logger.warning("PE行情整体失败，PE分位项全部计 0 分: %s", exc)
        return {}, {}
    if not pe_map:
        return {}, {}

    # 行业 -> 有效 PE 列表（PE>0 才有分位意义）
    peers: dict[str, list[float]] = {}
    for code, pe in pe_map.items():
        if pe and pe > 0:
            peers.setdefault(industry_by_code.get(code, INDUSTRY_FALLBACK), []).append(pe)

    out: dict[str, float] = {}
    from app.scoring import compute_pe_percentile

    for code, pe in pe_map.items():
        pct = compute_pe_percentile(pe, peers.get(industry_by_code.get(code, ""), []))
        if pct is not None:
            out[code] = pct
    say = progress or (lambda m: None)
    say(f"PE行业分位计算完成：覆盖 {len(out)} 只")
    return out, pe_map


def _rescore_with_chips(
    conn, spot, listing_map, industry_map, results,
    pe_value_map: dict[str, float] | None = None,
    pe_pct_map: dict[str, float] | None = None,
) -> list[dict]:
    """抓取筹码后，结合本地筹码缓存对全部结果重算一遍分数。"""
    pe_value_map = pe_value_map or {}
    pe_pct_map = pe_pct_map or {}
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
            conn, code, item["name"], row, listing_days, chip=chip,
            industry=item.get("industry") or industry_map.get(code) or INDUSTRY_FALLBACK,
            pe=pe_value_map.get(code), pe_percentile=pe_pct_map.get(code),
        )
        if item2:
            item2["industry"] = (
                item.get("industry")
                or industry_map.get(code)
                or INDUSTRY_FALLBACK
            )
            item2["board"] = board_of(code)
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
    "核心形态分(50)", "筹码基本面分(20)", "行业板块分(30)", "综合匹配分(100)",
    "MACD金叉且红柱大于0", "KDJ金叉且J值小于100",
    "放量超5日均量1.3倍", "放量超5日均量2倍", "换手率3%-15%", "近40日振幅不超1.8",
    "筹码集中度不高于18%", "筹码集中度大于20%", "PE行业分位", "近5日涨幅5%-20%",
    "创业板或科创板", "热点行业",
    "收盘价", "PE(TTM)", "近5日涨幅%", "当日换手率", "K", "D", "J", "筹码集中度",
    "MACD红柱相对前高收缩", "MACD红柱前高", "当日MACD红柱",
    "近40日最高价", "近40日最低价",
]

# 命中项与展示字段的说明行（与 EXPORT_COLUMNS 一一对应，共 33 列）
_EXPORT_NOTE_ROW = [
    "", "", "", "", "", "", "", "",            # 1-8  标识与分数列
    "", "", "", "", "", "", "", "",            # 9-16 命中项列
    "low=低于行业30%分位; mid=30%-70%; "
    "high=高于70%; missing=样本不足或缺失",     # 17  PE行业分位口径
    "", "", "", "", "", "", "", "",            # 18-25
    "", "", "",                                # 26-28
    config.DISPLAY_FIELD_NOTE,                 # 29  红柱收缩（历史见顶相关）
    "", "", "",                                # 30-33
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
        writer.writerow(_EXPORT_NOTE_ROW)
        for r in results:
            bd, m, d = r["breakdown"], r["metrics"], r["display"]
            core = bd.get("core", {})
            fund = bd.get("fund", {})
            ind = bd.get("industry", {})
            writer.writerow([
                r["code"], r["name"],
                r.get("board") or board_of(r["code"]),
                r.get("industry") or INDUSTRY_FALLBACK,
                bd.get("core_score", 0), bd.get("fund_score", 0),
                bd.get("industry_score", 0), r["total_score"],
                int(bool(core.get("macd_gold_red"))),
                int(bool(core.get("kdj_gold_j_under_100"))),
                int(bool(core.get("volume_surge_1_3x"))),
                int(bool(core.get("volume_surge_2x"))),
                int(bool(core.get("turnover_healthy_3_15"))),
                int(bool(core.get("range_compact_40d"))),
                int(bool(fund.get("chip_concentrated_le_18"))),
                int(bool(fund.get("chip_loose_gt_20"))),
                fund.get("pe_tier", "missing"),
                int(bool(fund.get("return5_healthy_5_20"))),
                int(bool(ind.get("growth_board"))),
                int(bool(ind.get("hot_industry"))),
                m.get("close"), m.get("pe"), m.get("return_5d_pct"),
                d.get("turnover_rate"),
                m.get("k"), m.get("d"), m.get("j"),
                m.get("chip_concentration"),
                int(bool(d.get("macd_hist_shrink"))),
                d.get("macd_hist_peak_prev"), d.get("macd_hist_today"),
                d.get("range_high_40d"), d.get("range_low_40d"),
            ])
    return str(path)

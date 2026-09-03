# -*- coding: utf-8 -*-
"""AKShare 数据源封装层：统一重试与容错。

设计要点：
- 所有接口失败时抛出 DataSourceError 或返回 None，由上层决定降级策略，
  保证「部分数据缺失时整体流程不中断」。
- 筹码集中度接口（stock_cyq_em）按单只股票逐日返回，全市场抓取成本高，
  因此仅在显式开启时对通过初筛的个股抓取，失败返回 None。
- 本项目仅使用免费公开接口，不对接任何付费数据源。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import akshare as ak
import pandas as pd

from app import config

logger = logging.getLogger(__name__)


class DataSourceError(RuntimeError):
    """数据源请求失败（重试后仍失败）。"""


def _retry(func, *args, retries: int | None = None, **kwargs):
    """带重试的数据源调用：网络抖动时退避重试，最终失败抛 DataSourceError。"""
    retries = config.FETCH_RETRY if retries is None else retries
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 —— akshare 内部异常类型不固定
            last_exc = exc
            logger.warning(
                "数据源请求第 %s/%s 次失败: %s(%s) - %s",
                attempt, retries, func.__name__, args[:1], exc,
            )
            if attempt < retries:
                time.sleep(config.FETCH_RETRY_DELAY * attempt)
    raise DataSourceError(f"数据源请求失败: {func.__name__} {args[:1]} ({last_exc})")


# ---------------------------------------------------------------------------
# 全市场行情快照（单次请求，含当日 开/高/低/收/量/额/换手）
# ---------------------------------------------------------------------------
def fetch_spot() -> pd.DataFrame:
    """全A股实时快照（东财口径）。失败抛 DataSourceError。"""
    df = _retry(ak.stock_zh_a_spot_em)
    df = df.rename(
        columns={
            "代码": "code", "名称": "name", "最新价": "close", "昨收": "prev_close",
            "今开": "open", "最高": "high", "最低": "low",
            "成交量": "volume", "成交额": "amount", "换手率": "turnover_rate",
        }
    )
    return df


# ---------------------------------------------------------------------------
# 单只股票日K（前复权）
# ---------------------------------------------------------------------------
def fetch_hist(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """前复权日K。start/end 格式 YYYYMMDD。失败抛 DataSourceError。"""
    df = _retry(
        ak.stock_zh_a_hist,
        symbol=code, period="daily",
        start_date=start_date, end_date=end_date, adjust="qfq",
    )
    return df.rename(
        columns={
            "日期": "trade_date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "amount", "换手率": "turnover_rate",
        }
    )


# ---------------------------------------------------------------------------
# 上市日期（沪/深两个交易所各一次请求；失败返回空表，由上层降级）
# ---------------------------------------------------------------------------
def fetch_listing_dates() -> dict[str, int]:
    """返回 {code: 上市天数}。任一交易所接口失败时跳过该交易所。"""
    today = datetime.now()
    out: dict[str, int] = {}

    try:
        sh = _retry(ak.stock_info_sh_name_code, symbol="主板A股")
        for _, row in sh.iterrows():
            code = str(row["证券代码"])
            listed = pd.to_datetime(row["上市日期"])
            out[code] = (today - listed).days
    except DataSourceError as exc:
        logger.warning("沪市上市日期获取失败，将降级为窗口估计: %s", exc)

    try:
        sz = _retry(ak.stock_info_sz_name_code, symbol="A股列表")
        for _, row in sz.iterrows():
            code = str(row["A股代码"])
            listed = pd.to_datetime(row["A股上市日期"])
            out[code] = (today - listed).days
    except DataSourceError as exc:
        logger.warning("深市上市日期获取失败，将降级为窗口估计: %s", exc)

    return out


# ---------------------------------------------------------------------------
# 行业分类（东财行业板块成分，约 86 次请求，建议低频缓存）
# ---------------------------------------------------------------------------
def fetch_industry_map() -> dict[str, str]:
    """返回 {股票代码: 行业名称}。单个板块失败跳过，不影响其余板块。"""
    boards = _retry(ak.stock_board_industry_name_em)
    out: dict[str, str] = {}
    for _, board in boards.iterrows():
        board_name = str(board["板块名称"])
        try:
            cons = _retry(
                ak.stock_board_industry_cons_em, symbol=board_name, retries=2
            )
        except DataSourceError as exc:
            logger.warning("行业板块 %s 成分获取失败，跳过: %s", board_name, exc)
            continue
        for _, row in cons.iterrows():
            out[str(row["代码"])] = board_name
    return out


# ---------------------------------------------------------------------------
# 筹码集中度（可选数据：接口按单股逐日返回，缺失时上层计 0 分）
# ---------------------------------------------------------------------------
def fetch_chip_concentration(code: str) -> tuple[str, float] | None:
    """返回 (最新交易日, 90日筹码集中度%)；失败或无数据返回 None。

    90集中度 = (90成本-高 - 90成本-低) / (90成本-高 + 90成本-低)，越小越集中。
    """
    try:
        df = _retry(ak.stock_cyq_em, symbol=code, adjust="", retries=2)
    except DataSourceError as exc:
        logger.info("筹码数据获取失败（对应项计 0 分）: %s - %s", code, exc)
        return None
    if df is None or df.empty or "90集中度" not in df.columns:
        return None
    last = df.iloc[-1]
    value = last.get("90集中度")
    if value is None or pd.isna(value):
        return None
    return str(last["日期"]), float(value) * (100.0 if float(value) <= 1.0 else 1.0)


# ---------------------------------------------------------------------------
# 宏观指数快照
# ---------------------------------------------------------------------------
INDEX_WATCHLIST = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000688": "科创50",
}


def fetch_index_snapshot() -> list[dict]:
    """核心指数快照（代码/名称/最新点位/涨跌幅）。失败返回空列表。

    使用东财「沪深重要指数」，为空时回退到上证/深证系列指数两次请求。
    """
    frames = []
    for symbol in ("沪深重要指数", "上证系列指数", "深证系列指数"):
        try:
            df = _retry(ak.stock_zh_index_spot_em, symbol=symbol, retries=2)
        except DataSourceError as exc:
            logger.warning("指数快照 %s 获取失败: %s", symbol, exc)
            continue
        if df is not None and not df.empty:
            frames.append(df)
        if any(r["code"] in INDEX_WATCHLIST for r in _parse_index_rows(df)):
            break
    rows: dict[str, dict] = {}
    for df in frames:
        for item in _parse_index_rows(df):
            rows.setdefault(item["code"], item)
    return list(rows.values())


def _parse_index_rows(df) -> list[dict]:
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        code = str(r.get("代码", ""))
        if code in INDEX_WATCHLIST:
            out.append(
                {
                    "code": code,
                    "name": INDEX_WATCHLIST[code],
                    "close": _to_float(r.get("最新价")),
                    "change_pct": _to_float(r.get("涨跌幅")),
                    "amount": _to_float(r.get("成交额")),
                }
            )
    return out


def _to_float(v) -> float | None:
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None

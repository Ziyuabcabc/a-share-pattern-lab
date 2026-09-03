# -*- coding: utf-8 -*-
"""行情数据源封装层：统一重试与容错，主源 AKShare（东财）+ 腾讯/新浪备用源。

设计要点：
- 所有接口失败时抛出 DataSourceError 或返回 None，由上层决定降级策略，
  保证「部分数据缺失时整体流程不中断」。
- 部分本机网络环境无法直连东财行情域（push2 系列子域），此时自动回退：
    * 全市场快照   → 新浪财经 hs_a 分页列表（含开高低收/量/额/换手）
    * 单股日K      → 腾讯 fqkline 前复权日K（OHLC + 成交量）
    * 指数快照     → 腾讯批量指数行情
    * 行业分类     → 新浪行业板块成分
- 筹码集中度接口（stock_cyq_em）依赖东财 push2his，接口不可达时返回 None，
  上层对应打分项计 0 分。
- 本项目仅使用免费公开接口，不对接任何付费数据源。
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime

import akshare as ak
import pandas as pd
import requests

from app import config

# 本机环境若注入了本地代理变量，会导致行情域名连接异常；
# 行情接口均为国内直连可达的公开地址，启动时统一剥离代理设置。
for _proxy_key in (
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
    "all_proxy", "ALL_PROXY",
):
    os.environ.pop(_proxy_key, None)
os.environ.setdefault("NO_PROXY", "*")

logger = logging.getLogger(__name__)

# 备用源共享会话：禁用系统代理探测，携带浏览器 UA
_session = requests.Session()
_session.trust_env = False
_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})


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
_SINA_LIST_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
_SINA_PAGE_SIZE = 100
_SINA_PAGE_DELAY = 0.25  # 新浪列表接口限频保护

_SPOT_COLUMNS = [
    "code", "name", "close", "prev_close", "open",
    "high", "low", "volume", "amount", "turnover_rate",
]


def fetch_spot() -> pd.DataFrame:
    """全A股实时快照。主源东财，异常时回退新浪分页接口。失败抛 DataSourceError。"""
    try:
        df = _retry(ak.stock_zh_a_spot_em, retries=2)
        df = df.rename(
            columns={
                "代码": "code", "名称": "name", "最新价": "close", "昨收": "prev_close",
                "今开": "open", "最高": "high", "最低": "low",
                "成交量": "volume", "成交额": "amount", "换手率": "turnover_rate",
            }
        )
        logger.info("全市场快照使用主源（东财）: %s 只", len(df))
        return df
    except DataSourceError as exc:
        logger.warning("东财快照不可达，回退新浪列表源: %s", exc)
    df = _spot_from_sina()
    logger.info("全市场快照使用备用源（新浪）: %s 只", len(df))
    return df


def _spot_from_sina() -> pd.DataFrame:
    """新浪 hs_a 分页拉取全市场快照。成交量单位统一为手（与东财口径一致）。"""
    rows: list[dict] = []
    page = 1
    while True:
        last_exc: Exception | None = None
        items = None
        for attempt in range(1, 4):
            try:
                resp = _session.get(
                    _SINA_LIST_URL,
                    params={
                        "page": page, "num": _SINA_PAGE_SIZE,
                        "sort": "symbol", "asc": 1, "node": "hs_a",
                    },
                    timeout=20,
                )
                items = resp.json()
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(1.0 * attempt)
        if items is None:
            if not rows:  # 首页即失败 → 整体失败
                raise DataSourceError(f"新浪列表接口失败: {last_exc}")
            logger.warning("新浪列表第 %s 页失败，使用已获取的 %s 只", page, len(rows))
            break
        if not items:
            break
        for it in items:
            vol = _f(it.get("volume"))          # 新浪单位: 股
            rows.append(
                {
                    "code": str(it.get("code", "")),
                    "name": str(it.get("name", "")),
                    "close": _f(it.get("trade")),
                    "prev_close": _f(it.get("settlement")),
                    "open": _f(it.get("open")),
                    "high": _f(it.get("high")),
                    "low": _f(it.get("low")),
                    "volume": (vol / 100.0) if vol else vol,  # 股 → 手
                    "amount": _f(it.get("amount")),
                    "turnover_rate": _f(it.get("turnoverratio")),
                }
            )
        if len(items) < _SINA_PAGE_SIZE:
            break
        page += 1
        time.sleep(_SINA_PAGE_DELAY)
    if not rows:
        raise DataSourceError("新浪列表接口返回空数据")
    return pd.DataFrame(rows, columns=_SPOT_COLUMNS)


# ---------------------------------------------------------------------------
# 单只股票日K（前复权）
# ---------------------------------------------------------------------------
# 东财日K熔断器：本机网络可能整体屏蔽东财行情域，逐股重试白白浪费
# 时间（每股约 4 秒）。连续失败达到阈值后，冷却期内直接跳过东财，
# 冷却结束后放行一次试探请求，成功即恢复。
_HIST_EM_FAIL_STREAK = 0
_HIST_EM_BLOCKED_UNTIL = 0.0
_HIST_EM_BLOCK_THRESHOLD = 3   # 连续失败次数达到该值触发熔断
_HIST_EM_COOLDOWN_SEC = 900    # 熔断冷却时长（秒）


def fetch_hist(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """前复权日K。主源东财（带熔断），异常时回退腾讯 fqkline。start/end YYYYMMDD。"""
    global _HIST_EM_FAIL_STREAK, _HIST_EM_BLOCKED_UNTIL
    if time.time() >= _HIST_EM_BLOCKED_UNTIL:
        try:
            df = _retry(
                ak.stock_zh_a_hist,
                symbol=code, period="daily",
                start_date=start_date, end_date=end_date, adjust="qfq",
                retries=2,
            )
            _HIST_EM_FAIL_STREAK = 0
            _HIST_EM_BLOCKED_UNTIL = 0.0
            return df.rename(
                columns={
                    "日期": "trade_date", "开盘": "open", "收盘": "close",
                    "最高": "high", "最低": "low", "成交量": "volume",
                    "成交额": "amount", "换手率": "turnover_rate",
                }
            )
        except DataSourceError as exc:
            _HIST_EM_FAIL_STREAK += 1
            if _HIST_EM_FAIL_STREAK >= _HIST_EM_BLOCK_THRESHOLD:
                _HIST_EM_BLOCKED_UNTIL = time.time() + _HIST_EM_COOLDOWN_SEC
                logger.warning(
                    "东财日K连续失败 %s 次，熔断 %s 秒（期间直接走腾讯源）",
                    _HIST_EM_FAIL_STREAK, _HIST_EM_COOLDOWN_SEC,
                )
            logger.info("东财日K不可达，回退腾讯K线源: %s - %s", code, exc)
    return _hist_from_tencent(code, start_date, end_date)


def _tencent_symbol(code: str) -> str:
    """股票代码 → 腾讯带市场前缀代码（沪 sh / 深 sz / 北 bj）。"""
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    return f"bj{code}"


# 同一腾讯K线 API 的多个镜像域名：单个域名被限流（如返回 501 挑战页）时
# 自动切换下一个，避免整轮扫描被单一域名拖死。
_TENCENT_KLINE_URLS = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
)
_tencent_url_index = 0


def _hist_from_tencent(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """腾讯前复权日K：OHLC + 成交量（手）。成交额/换手率该源不提供，置为 None。"""
    global _tencent_url_index
    sym = _tencent_symbol(code)
    param = (
        f"{sym},day,{_fmt_dashes(start_date)},{_fmt_dashes(end_date)},640,qfq"
    )
    last_exc: Exception | None = None
    for attempt in range(1, config.FETCH_RETRY + 1):
        url = _TENCENT_KLINE_URLS[_tencent_url_index % len(_TENCENT_KLINE_URLS)]
        try:
            resp = _session.get(url, params={"param": param}, timeout=20)
            payload = resp.json()
            data = (payload.get("data") or {}).get(sym) or {}
            arr = data.get("qfqday") or data.get("day") or []
            if not arr:
                return pd.DataFrame(
                    columns=["trade_date", "open", "close", "high", "low",
                             "volume", "amount", "turnover_rate"]
                )
            rows = [
                {
                    "trade_date": str(k[0]),
                    "open": _f(k[1]), "close": _f(k[2]),
                    "high": _f(k[3]), "low": _f(k[4]),
                    "volume": _f(k[5]),          # 腾讯单位: 手（与东财一致）
                    "amount": None,
                    "turnover_rate": None,
                }
                for k in arr
            ]
            return pd.DataFrame(rows)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            # 当前域名连续异常（限流挑战页/超时）时切换镜像域名再试
            _tencent_url_index = (_tencent_url_index + 1) % len(_TENCENT_KLINE_URLS)
            time.sleep(config.FETCH_RETRY_DELAY * attempt)
    raise DataSourceError(f"腾讯日K获取失败: {code} ({last_exc})")


def _fmt_dashes(yyyymmdd: str) -> str:
    s = str(yyyymmdd)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


# ---------------------------------------------------------------------------
# 上市日期（沪/深两个交易所各一次请求；失败返回空表，由上层降级）
# ---------------------------------------------------------------------------
def fetch_listing_dates() -> dict[str, int]:
    """返回 {code: 上市天数}。任一交易所接口失败时跳过该交易所。"""
    today = datetime.now()
    out: dict[str, int] = {}

    try:
        for symbol in ("主板A股", "科创板"):
            sh = _retry(ak.stock_info_sh_name_code, symbol=symbol, retries=2)
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
_SINA_INDUSTRY_URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
_SINA_INDUSTRY_DELAY = 0.15


def fetch_industry_map() -> dict[str, str]:
    """返回 {股票代码: 行业名称}。主源东财板块，异常时回退新浪行业板块。"""
    try:
        boards = _retry(ak.stock_board_industry_name_em, retries=2)
    except DataSourceError as exc:
        logger.warning("东财行业板块不可达，回退新浪行业源: %s", exc)
        return _industry_from_sina()
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


def _industry_from_sina() -> dict[str, str]:
    """新浪行业板块成分：板块列表 1 次请求 + 逐板块分页拉取成分股。"""
    try:
        resp = _session.get(_SINA_INDUSTRY_URL, timeout=20)
        resp.encoding = "gbk"
        match = re.search(r"\{.*\}", resp.text, re.S)
        if not match:
            raise DataSourceError("新浪行业板块列表格式异常")
        import json

        raw = json.loads(match.group(0))
        boards = []
        for node, info in raw.items():
            parts = str(info).split(",")
            if len(parts) >= 3:
                boards.append((node, parts[1], int(float(parts[2]))))
    except DataSourceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DataSourceError(f"新浪行业板块列表失败: {exc}")

    out: dict[str, str] = {}
    for node, name, num in boards:
        pages = max(1, -(-num // _SINA_PAGE_SIZE))
        got = 0
        for page in range(1, pages + 1):
            try:
                resp = _session.get(
                    _SINA_LIST_URL,
                    params={
                        "page": page, "num": _SINA_PAGE_SIZE,
                        "sort": "symbol", "asc": 1, "node": node,
                    },
                    timeout=20,
                )
                items = resp.json() or []
            except Exception as exc:  # noqa: BLE001
                logger.warning("新浪行业 %s 第 %s 页失败: %s", name, page, exc)
                break
            for it in items:
                symbol = str(it.get("symbol", ""))
                if len(symbol) > 2:
                    out.setdefault(symbol[2:], name)
                    got += 1
            if len(items) < _SINA_PAGE_SIZE:
                break
            time.sleep(_SINA_INDUSTRY_DELAY)
        time.sleep(_SINA_INDUSTRY_DELAY)
    logger.info("新浪行业分类完成: %s 只 / %s 个板块", len(out), len(boards))
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

    主源东财「沪深重要指数」，异常时回退腾讯批量指数行情。
    """
    frames = []
    em_ok = True
    for symbol in ("沪深重要指数", "上证系列指数", "深证系列指数"):
        try:
            df = _retry(ak.stock_zh_index_spot_em, symbol=symbol, retries=2)
        except DataSourceError as exc:
            logger.warning("指数快照 %s 获取失败: %s", symbol, exc)
            em_ok = False
            continue
        if df is not None and not df.empty:
            frames.append(df)
        if any(r["code"] in INDEX_WATCHLIST for r in _parse_index_rows(df)):
            break
    rows: dict[str, dict] = {}
    for df in frames:
        for item in _parse_index_rows(df):
            rows.setdefault(item["code"], item)
    if em_ok and len(rows) >= len(INDEX_WATCHLIST) - 2:
        return list(rows.values())
    # 主源缺失大部分指数时，用腾讯行情补齐
    tencent = _index_from_tencent()
    for item in tencent:
        rows.setdefault(item["code"], item)
    return list(rows.values())


def _index_from_tencent() -> list[dict]:
    """腾讯批量指数行情（上证/深成/创业板指/科创50）。失败返回空列表。"""
    symbols = {
        "000001": "sh000001", "399001": "sz399001",
        "399006": "sz399006", "000688": "sh000688",
    }
    try:
        resp = _session.get(
            "https://qt.gtimg.cn/q=" + ",".join(symbols.values()), timeout=15
        )
        resp.encoding = "gbk"
    except Exception as exc:  # noqa: BLE001
        logger.warning("腾讯指数快照失败: %s", exc)
        return []
    out = []
    for line in resp.text.split(";"):
        line = line.strip()
        if "=" not in line or '"' not in line:
            continue
        fields = line.split('"')[1].split("~")
        if len(fields) < 38:
            continue
        code = fields[2]
        if code not in INDEX_WATCHLIST:
            continue
        close = _f(fields[3])
        prev = _f(fields[4])
        change = _f(fields[32])
        if change is None and close is not None and prev:
            change = round((close - prev) / prev * 100, 2)
        amount = _f(fields[37])
        out.append(
            {
                "code": code,
                "name": INDEX_WATCHLIST[code],
                "close": close,
                "change_pct": change,
                "amount": amount * 1e4 if amount else None,  # 腾讯单位: 万元
            }
        )
    return out


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
        if v is None or v == "" or pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


_f = _to_float  # 内部简写

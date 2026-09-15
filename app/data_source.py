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
    """返回 {股票代码: 行业名称}。

    覆盖优先级：申万一级行业（主源，31 个行业基本覆盖全市场）>
    东财行业板块 > 新浪行业板块（仅补缺，不覆盖已有行业）。
    单一来源部分失败不影响整体，匹配不到的由调用方标注「其他」。
    """
    out: dict[str, str] = _industry_from_sw()
    sw_hits = len(out)
    # 备源一：东财行业板块（逐板块失败仅跳过该板块）
    try:
        boards = _retry(ak.stock_board_industry_name_em, retries=2)
    except DataSourceError as exc:
        logger.warning("东财行业板块列表不可达，仅使用申万+新浪行业源: %s", exc)
        boards = None
    if boards is not None:
        for _, board in boards.iterrows():
            board_name = str(board["板块名称"])
            sw_name = _normalize_industry(board_name)
            if sw_name is None:
                # 板块名无法归入申万一级（风格/概念类板块），不写入行业字段
                logger.info("行业板块「%s」非申万一级口径，跳过", board_name)
                continue
            try:
                cons = _retry(
                    ak.stock_board_industry_cons_em, symbol=board_name, retries=1
                )
            except DataSourceError as exc:
                logger.warning("行业板块 %s 成分获取失败，跳过: %s", board_name, exc)
                continue
            for _, row in cons.iterrows():
                out.setdefault(str(row["代码"]).zfill(6), sw_name)
    # 备源二：新浪行业板块，仅补充前两源未覆盖的代码
    for code, name in _industry_from_sina().items():
        sw_name = _normalize_industry(name)
        if sw_name is not None:
            out.setdefault(code, sw_name)
    # 备源三：北交所交易所名单（证监会行业口径 → 申万一级），最后兜底
    for code, name in _industry_from_bj().items():
        out.setdefault(code, name)
    logger.info(
        "行业分类合并完成: 覆盖 %s 只（申万一级 %s + 东财/新浪/北交所补缺）",
        len(out), sw_hits,
    )
    return out


# ---------------------------------------------------------------------------
# 行业口径归一化
#
# 行业字段统一为「申万一级行业（31 个）」口径，否则行业分布图会混入
# 风格/概念类板块名（如新浪的「次新股」），失去参考价值。
# 东财/新浪等补缺源的板块名先经 _normalize_industry 归一化，归不进去的
# 直接丢弃（该股行业字段保持「其他」，由调用方兜底），绝不写入非申万口径。
# ---------------------------------------------------------------------------
SW_LEVEL1 = frozenset({
    "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "家用电器",
    "食品饮料", "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输",
    "房地产", "商贸零售", "社会服务", "综合", "建筑材料", "建筑装饰",
    "电力设备", "机械设备", "国防军工", "汽车", "计算机", "传媒", "通信",
    "银行", "非银金融", "美容护理", "石油石化", "煤炭", "环保",
})

# 常见行业名关键词 → 申万一级（按顺序匹配，靠前的规则优先）
_INDUSTRY_ALIASES: tuple[tuple[str, str], ...] = (
    ("计算机、通信", "电子"),        # 证监会口径中的电子设备大类
    ("半导体", "电子"), ("电子", "电子"), ("光学", "电子"), ("面板", "电子"),
    ("计算机", "计算机"), ("软件", "计算机"), ("互联网", "计算机"),
    ("IT设备", "计算机"), ("信息技术", "计算机"),
    ("通信", "通信"), ("通讯", "通信"),
    ("医药", "医药生物"), ("中药", "医药生物"), ("生物制品", "医药生物"),
    ("医疗", "医药生物"), ("制药", "医药生物"),
    ("证券", "非银金融"), ("券商", "非银金融"), ("保险", "非银金融"),
    ("多元金融", "非银金融"), ("信托", "非银金融"),
    ("银行", "银行"),
    ("金融", "非银金融"),
    ("房地产", "房地产"), ("地产", "房地产"),
    ("建筑装饰", "建筑装饰"), ("工程建设", "建筑装饰"), ("装修", "建筑装饰"),
    ("建筑材料", "建筑材料"), ("水泥", "建筑材料"), ("玻璃", "建筑材料"),
    ("钢铁", "钢铁"),
    ("有色", "有色金属"), ("贵金属", "有色金属"), ("小金属", "有色金属"),
    ("煤炭", "煤炭"),
    ("石油", "石油石化"), ("油气", "石油石化"), ("油服", "石油石化"),
    ("电力", "电力设备"), ("电气", "电力设备"), ("输配", "电力设备"),
    ("光伏", "电力设备"), ("风电", "电力设备"), ("电池", "电力设备"),
    ("新能源", "电力设备"), ("电源", "电力设备"),
    ("机械", "机械设备"), ("设备", "机械设备"), ("仪器仪表", "机械设备"),
    ("金属制品", "机械设备"), ("工程机械", "机械设备"),
    ("军工", "国防军工"), ("航天", "国防军工"), ("航空", "国防军工"),
    ("汽车", "汽车"), ("交运设备", "汽车"),
    ("家电", "家用电器"), ("白色家电", "家用电器"),
    ("食品", "食品饮料"), ("饮料", "食品饮料"), ("酿酒", "食品饮料"), ("白酒", "食品饮料"),
    ("纺织", "纺织服饰"), ("服装", "纺织服饰"), ("服饰", "纺织服饰"),
    ("轻工", "轻工制造"), ("造纸", "轻工制造"), ("包装", "轻工制造"), ("家具", "轻工制造"),
    ("化工", "基础化工"), ("化学", "基础化工"), ("化纤", "基础化工"),
    ("塑料", "基础化工"), ("橡胶", "基础化工"), ("农药", "基础化工"), ("化肥", "基础化工"),
    ("农业", "农林牧渔"), ("农牧", "农林牧渔"), ("养殖", "农林牧渔"),
    ("种植", "农林牧渔"), ("渔业", "农林牧渔"), ("饲料", "农林牧渔"),
    ("物流", "交通运输"), ("港口", "交通运输"), ("公路", "交通运输"),
    ("铁路", "交通运输"), ("机场", "交通运输"), ("水运", "交通运输"), ("运输", "交通运输"),
    ("商业", "商贸零售"), ("百货", "商贸零售"), ("贸易", "商贸零售"),
    ("零售", "商贸零售"), ("批发", "商贸零售"),
    ("旅游", "社会服务"), ("酒店", "社会服务"), ("餐饮", "社会服务"),
    ("教育", "社会服务"), ("专业服务", "社会服务"), ("服务", "社会服务"),
    ("传媒", "传媒"), ("影视", "传媒"), ("游戏", "传媒"), ("广告", "传媒"), ("文化", "传媒"),
    ("环保", "环保"), ("环境治理", "环保"),
    ("公用", "公用事业"), ("燃气", "公用事业"), ("水务", "公用事业"), ("供水", "公用事业"),
    ("美容", "美容护理"), ("化妆", "美容护理"), ("个护", "美容护理"),
    ("综合", "综合"),
)


def _normalize_industry(name: str | None) -> str | None:
    """把任意来源的行业/板块名归一到申万一级；无法归类时返回 None。

    注意：风格/概念类板块名（次新股、ST股、融资融券等）不会命中任何
    关键词，因此会被判定为 None 并丢弃，避免污染行业分布口径。
    """
    if not name:
        return None
    text = str(name).strip()
    if not text:
        return None
    if text in SW_LEVEL1:
        return text
    for keyword, sw_name in _INDUSTRY_ALIASES:
        if keyword in text:
            return sw_name
    return None


def _industry_from_sw() -> dict[str, str]:
    """申万一级行业成分：行业列表 1 次请求 + 逐行业成分 1 次请求（约 31 次）。

    申万分类对沪深个股覆盖率接近全市场，作为行业主源；
    单个行业成分失败仅跳过，不影响其他行业。
    """
    try:
        first = _retry(ak.sw_index_first_info, retries=2)
    except DataSourceError as exc:
        logger.warning("申万一级行业列表不可达，跳过申万源: %s", exc)
        return {}
    out: dict[str, str] = {}
    failed = 0
    for _, row in first.iterrows():
        # 行业代码形如 801010.SI，成分接口接受纯数字代码
        sw_code = str(row["行业代码"]).split(".")[0]
        industry_name = str(row["行业名称"])
        try:
            cons = _retry(ak.index_component_sw, symbol=sw_code, retries=2)
        except DataSourceError as exc:
            logger.warning("申万行业 %s 成分获取失败，跳过: %s", industry_name, exc)
            failed += 1
            continue
        for _, item in cons.iterrows():
            # 证券代码统一 6 位字符串（不足补前导零）
            out.setdefault(str(item["证券代码"]).zfill(6), industry_name)
        time.sleep(0.2)
    logger.info("申万一级行业成分完成: %s 只 / 失败行业 %s 个", len(out), failed)
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
    failed_boards = 0
    for node, name, num in boards:
        pages = max(1, -(-num // _SINA_PAGE_SIZE))
        got = 0
        for page in range(1, pages + 1):
            items = None
            # 页面级重试：限流/抖动导致的单页失败不应造成该行业大面积缺股
            for attempt in range(1, 4):
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
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "新浪行业 %s 第 %s 页第 %s 次尝试失败: %s",
                        name, page, attempt, exc,
                    )
                    time.sleep(1.0 * attempt)
            if items is None:
                break
            for it in items:
                symbol = str(it.get("symbol", ""))
                if len(symbol) > 2:
                    out.setdefault(symbol[2:], name)
                    got += 1
            if len(items) < _SINA_PAGE_SIZE:
                break
            time.sleep(_SINA_INDUSTRY_DELAY)
        if got < num:
            failed_boards += 1
        time.sleep(_SINA_INDUSTRY_DELAY)
    logger.info(
        "新浪行业分类完成: %s 只 / %s 个板块（成分不全板块 %s 个）",
        len(out), len(boards), failed_boards,
    )
    return out


# ---------------------------------------------------------------------------
# 北交所行业（交易所名单接口，最后兜底源）
#
# 北交所股票不在申万一级成分/东财板块的常规覆盖范围内，此前统一落到
# 「其他」，使行业字段出现缺口。交易所公开名单提供证监会行业门类口径，
# 这里统一映射为申万一级，保证全市场行业口径一致、无空值。
# 注：北交所个股按项目基础过滤规则不参与候选池打分，本映射只用于
# 基础信息对照表与行业分布口径的完整性。
# ---------------------------------------------------------------------------
_CSRC_TO_SW: dict[str, str] = {
    "农业": "农林牧渔",
    "畜牧业": "农林牧渔",
    "农、林、牧、渔专业及辅助性活动": "农林牧渔",
    "农副食品加工业": "农林牧渔",
    "食品制造业": "食品饮料",
    "酒、饮料和精制茶制造业": "食品饮料",
    "纺织业": "纺织服饰",
    "纺织服装、服饰业": "纺织服饰",
    "家具制造业": "轻工制造",
    "造纸和纸制品业": "轻工制造",
    "印刷和记录媒介复制业": "轻工制造",
    "文教、工美、体育和娱乐用品制造业": "轻工制造",
    "化学原料和化学制品制造业": "基础化工",
    "化学纤维制造业": "基础化工",
    "橡胶和塑料制品业": "基础化工",
    "非金属矿物制品业": "建筑材料",
    "有色金属冶炼和压延加工业": "有色金属",
    "金属制品业": "机械设备",
    "通用设备制造业": "机械设备",
    "专用设备制造业": "机械设备",
    "仪器仪表制造业": "机械设备",
    "电气机械和器材制造业": "电力设备",
    "汽车制造业": "汽车",
    "铁路、船舶、航空航天和其他运输设备制造业": "国防军工",
    "计算机、通信和其他电子设备制造业": "电子",
    "软件和信息技术服务业": "计算机",
    "电信、广播电视和卫星传输服务": "通信",
    "互联网和相关服务": "传媒",
    "医药制造业": "医药生物",
    "生态保护和环境治理业": "环保",
    "土木工程建筑业": "建筑装饰",
    "房屋建筑业": "建筑装饰",
    "批发业": "商贸零售",
    "零售业": "商贸零售",
    "道路运输业": "交通运输",
    "水上运输业": "交通运输",
    "航空运输业": "交通运输",
    "多式联运和运输代理业": "交通运输",
    "装卸搬运和仓储业": "交通运输",
    "商务服务业": "社会服务",
    "专业技术服务业": "社会服务",
    "科技推广和应用服务业": "社会服务",
    "公共设施管理业": "社会服务",
    "电力、热力生产和供应业": "公用事业",
    "燃气生产和供应业": "公用事业",
    "水的生产和供应业": "公用事业",
    "开采专业及辅助性活动": "石油石化",
    "石油、煤炭及其他燃料加工业": "石油石化",
    "煤炭开采和洗选业": "煤炭",
    "货币金融服务": "银行",
    "资本市场服务": "非银金融",
    "保险业": "非银金融",
    "研究和试验发展": "综合",
}

# 关键词兜底规则（应对行业名称口径微调，按顺序前缀匹配）
_CSRC_SW_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("计算机、通信", "电子"),
    ("软件和信息技术", "计算机"),
    ("电气机械", "电力设备"),
    ("专用设备", "机械设备"),
    ("通用设备", "机械设备"),
    ("仪器仪表", "机械设备"),
    ("医药", "医药生物"),
    ("化学", "基础化工"),
    ("汽车", "汽车"),
    ("食品", "食品饮料"),
    ("纺织", "纺织服饰"),
    ("有色金属", "有色金属"),
    ("运输", "交通运输"),
    ("生态保护和环境", "环保"),
    ("电力", "公用事业"),
    ("建筑", "建筑装饰"),
    ("零售", "商贸零售"),
    ("农业", "农林牧渔"),
    ("畜牧", "农林牧渔"),
    ("燃气", "公用事业"),
    ("造纸", "轻工制造"),
    ("家具", "轻工制造"),
    ("印刷", "轻工制造"),
    ("金属制品", "机械设备"),
    ("专业技术服务", "社会服务"),
    ("商务服务", "社会服务"),
)


def _csrc_industry_to_sw(raw: str) -> str | None:
    """证监会行业名称 → 申万一级行业名称（精确表 → 专用规则 → 通用别名）。"""
    if not raw:
        return None
    name = raw.strip()
    if name in _CSRC_TO_SW:
        return _CSRC_TO_SW[name]
    for prefix, sw in _CSRC_SW_KEYWORDS:
        if name.startswith(prefix):
            return sw
    return _normalize_industry(name)


def _industry_from_bj() -> dict[str, str]:
    """北交所个股行业映射（最后兜底源）。接口不可达时返回空表、不中断流程。"""
    try:
        df = _retry(ak.stock_info_bj_name_code, retries=2)
    except DataSourceError as exc:
        logger.warning("北交所名单接口不可达，跳过该行业源: %s", exc)
        return {}
    out: dict[str, str] = {}
    skipped = 0
    for _, row in df.iterrows():
        code = str(row.get("证券代码", "")).zfill(6)
        sw = _csrc_industry_to_sw(str(row.get("所属行业", "")))
        if len(code) == 6 and sw:
            out.setdefault(code, sw)
        else:
            skipped += 1
    logger.info("北交所行业映射完成: %s 只（未映射 %s 只）", len(out), skipped)
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
# 批量实时行情（腾讯）：市盈率 TTM（v1.1 PE行业分位数据源）
# ---------------------------------------------------------------------------
_QT_BATCH_SIZE = 60   # 腾讯批量行情单次请求的代码数上限（保守值）


def fetch_pe_map(codes: list[str], progress=None) -> dict[str, float]:
    """批量获取个股市盈率 TTM，返回 {code: pe}。单批失败跳过（不中断）。

    腾讯行情字段 [39] 为市盈率（TTM 口径，亏损为负值，由上层过滤）。
    """
    out: dict[str, float] = {}
    total_batches = -(-len(codes) // _QT_BATCH_SIZE)
    for i in range(0, len(codes), _QT_BATCH_SIZE):
        batch = codes[i:i + _QT_BATCH_SIZE]
        symbols = ",".join(_tencent_symbol(c) for c in batch)
        batch_no = i // _QT_BATCH_SIZE + 1
        if progress:
            progress(f"PE行情 {batch_no}/{total_batches} 批")
        for attempt in range(1, 3):
            try:
                resp = _session.get(
                    "https://qt.gtimg.cn/q=" + symbols, timeout=15
                )
                resp.encoding = "gbk"
                for line in resp.text.split(";"):
                    line = line.strip()
                    if "=" not in line or '"' not in line:
                        continue
                    fields = line.split('"')[1].split("~")
                    if len(fields) <= 39:
                        continue
                    code = fields[2]
                    pe = _f(fields[39])
                    if pe is not None:
                        out[code] = pe
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("腾讯PE行情第 %s 批第 %s 次失败: %s",
                               batch_no, attempt, exc)
                time.sleep(1.0 * attempt)
        time.sleep(0.2)
    logger.info("PE行情获取完成: %s/%s 只", len(out), len(codes))
    return out


# ---------------------------------------------------------------------------
# 宏观指数快照
# ---------------------------------------------------------------------------
INDEX_WATCHLIST = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000688": "科创50",
}

# 东财指数源冷却：主源不可达时在冷却期内直接走腾讯备用源，
# 避免每次打开首页都白等主源重试（详见 fetch_index_snapshot）
_INDEX_EM_BLOCKED_UNTIL = 0.0
_INDEX_EM_COOLDOWN_SEC = 900


def fetch_index_snapshot() -> list[dict]:
    """核心指数快照（代码/名称/最新点位/涨跌幅）。失败返回空列表。

    主源东财「沪深重要指数」，异常时回退腾讯批量指数行情。
    本机网络可能整体屏蔽东财行情域：主源首个分组失败即视为整体不可达，
    直接走备用源（否则 3 个分组 × 2 次重试要白等近 30 秒，首页会被拖住）；
    并对主源加冷却，冷却期内不再尝试。
    """
    global _INDEX_EM_BLOCKED_UNTIL
    frames = []
    em_ok = True
    if time.time() < _INDEX_EM_BLOCKED_UNTIL:
        em_ok = False
    else:
        for symbol in ("沪深重要指数", "上证系列指数", "深证系列指数"):
            try:
                df = _retry(ak.stock_zh_index_spot_em, symbol=symbol, retries=2)
            except DataSourceError as exc:
                logger.warning("指数快照 %s 获取失败: %s", symbol, exc)
                em_ok = False
                _INDEX_EM_BLOCKED_UNTIL = time.time() + _INDEX_EM_COOLDOWN_SEC
                break  # 主源整体不可达，不必再试其它分组
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


# 海外/港股参考指数（仅展示，不参与个股打分）：腾讯行情代码
OVERSEAS_WATCHLIST = {
    "usIXIC": "纳斯达克",
    "usINX": "标普500",
    "hkHSI": "恒生指数",
}


def fetch_overseas_indices() -> list[dict]:
    """海外隔夜与港股指数快照（名称/点位/涨跌幅）。失败返回空列表。

    注意：腾讯返回的指数代码与请求代码不同（usIXIC -> .IXIC、hkHSI -> HSI），
    因此按请求顺序与响应行一一对应取名。
    """
    codes = list(OVERSEAS_WATCHLIST)
    try:
        resp = _session.get(
            "https://qt.gtimg.cn/q=" + ",".join(codes), timeout=15
        )
        resp.encoding = "gbk"
    except Exception as exc:  # noqa: BLE001
        logger.warning("海外指数快照失败: %s", exc)
        return []
    lines = [
        ln.strip() for ln in resp.text.split(";")
        if "=" in ln and '"' in ln
    ]
    out = []
    for req_code, line in zip(codes, lines):
        fields = line.split('"')[1].split("~")
        if len(fields) < 33:
            continue
        close = _f(fields[3])
        change = _f(fields[32])
        if close is None:
            continue
        out.append(
            {
                "code": req_code,
                "name": OVERSEAS_WATCHLIST[req_code],
                "close": close,
                "change_pct": change,
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

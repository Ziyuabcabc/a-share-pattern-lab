# -*- coding: utf-8 -*-
"""回测数据层：独立维护近 3 年前复权日线（bt_klines）与基准指数日线（bt_index）。

设计要点
--------
1. **物理隔离**：扫描链路使用 klines 表（约 400 个自然日），回测使用
   bt_klines 表（近 3 年）。两者互不读写，回测数据准备无论成功与否都
   不会影响 v1.5 的扫描、打分与看板功能。
2. **复权口径一致**：前复权价以「最新交易日收盘」为基准，因此整段历史
   必须一次拉全。分次拼接会因复权基准滑动产生跳空，故本模块只做
   「一次性全量写入」，不做增量追加。
3. **断点续传**：已缓存根数达到 BT_MIN_BARS_FOR_READY 的股票直接跳过，
   中断后重跑只补缺失部分。

数据源：腾讯 fqkline（前复权）。该源不提供成交额与换手率，
回测中换手率项计 0 分（见 config.BT_UNAVAILABLE_ITEMS）。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd

from app import config
from app.data_source import DataSourceError, _session, _to_float

logger = logging.getLogger(__name__)

# 腾讯K线镜像域名：单个域名被限流时自动切换
_BT_URLS = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
)
_url_index = 0

# 回测区间起点（自然日）：近 BT_CALENDAR_YEARS 年
def default_start_date() -> str:
    """回测数据起点（YYYY-MM-DD）。"""
    today = datetime.now()
    try:
        start = today.replace(year=today.year - config.BT_CALENDAR_YEARS)
    except ValueError:  # 2月29日
        start = today.replace(year=today.year - config.BT_CALENDAR_YEARS, day=28)
    return start.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------
_DDL = (
    """
    CREATE TABLE IF NOT EXISTS bt_klines (
        code       TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        open       REAL, high REAL, low REAL, close REAL,
        volume     REAL,
        PRIMARY KEY (code, trade_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bt_klines_date ON bt_klines(trade_date)",
    """
    CREATE TABLE IF NOT EXISTS bt_index (
        symbol     TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        open       REAL, high REAL, low REAL, close REAL,
        PRIMARY KEY (symbol, trade_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bt_runs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at   TEXT,
        start_date   TEXT,
        end_date     TEXT,
        step         INTEGER,
        horizons     TEXT,
        stock_count  INTEGER,
        obs_count INTEGER,
        score_max    INTEGER,
        params       TEXT,
        summary      TEXT,
        status       TEXT,
        message      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bt_stats (
        run_id        INTEGER NOT NULL,
        group_name    TEXT NOT NULL,
        horizon       INTEGER NOT NULL,
        samples       INTEGER,
        win_rate      REAL,
        avg_return    REAL,
        median_return REAL,
        excess_return REAL,
        max_drawdown  REAL,
        pl_ratio      REAL,
        avg_win       REAL,
        avg_loss      REAL,
        total_return  REAL,
        avg_score     REAL,
        PRIMARY KEY (run_id, group_name, horizon)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bt_equity (
        run_id     INTEGER NOT NULL,
        group_name TEXT NOT NULL,
        horizon    INTEGER NOT NULL,
        obs_date TEXT NOT NULL,
        nav        REAL,
        excess_nav REAL,
        PRIMARY KEY (run_id, group_name, horizon, obs_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bt_trades (
        run_id      INTEGER NOT NULL,
        obs_date TEXT NOT NULL,
        code        TEXT NOT NULL,
        name        TEXT,
        board       TEXT,
        industry    TEXT,
        score       INTEGER,
        group_name  TEXT,
        entry_date  TEXT,
        entry_price REAL,
        ret_5       REAL,
        ret_10      REAL,
        ret_20      REAL,
        PRIMARY KEY (run_id, obs_date, code)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bt_trades_run ON bt_trades(run_id, group_name)",
    # ---------------- v1.7.0 研究深度升级新增表（全部为二次统计结果） ----------------
    """
    CREATE TABLE IF NOT EXISTS bt_industry (
        run_id     INTEGER NOT NULL,
        industry   TEXT NOT NULL,
        group_name TEXT NOT NULL,
        horizon    INTEGER NOT NULL,
        samples    INTEGER,
        win_rate   REAL,
        avg_return REAL,
        max_drawdown REAL,
        PRIMARY KEY (run_id, industry, group_name, horizon)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bt_industry_run ON bt_industry(run_id, industry)",
    """
    CREATE TABLE IF NOT EXISTS bt_resonance (
        run_id    INTEGER NOT NULL,
        obs_date  TEXT NOT NULL,
        code      TEXT NOT NULL,
        weekly_ok INTEGER,
        PRIMARY KEY (run_id, obs_date, code)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bt_resonance_run ON bt_resonance(run_id, weekly_ok)",
    """
    CREATE TABLE IF NOT EXISTS bt_sensitivity (
        run_id       INTEGER NOT NULL,
        generated_at TEXT,
        payload      TEXT,
        PRIMARY KEY (run_id)
    )
    """,
)


def ensure_tables(conn: sqlite3.Connection) -> None:
    """建立回测相关表（幂等）。"""
    for ddl in _DDL:
        conn.execute(ddl)
    conn.commit()


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def _symbol_of(code: str) -> str:
    """股票代码 → 腾讯带市场前缀代码。"""
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    return f"bj{code}"


def _fetch_tencent(symbol: str, start_date: str, end_date: str, bars: int) -> list[list]:
    """向腾讯 fqkline 拉取前复权日线原始数组（线程内执行，无数据库操作）。"""
    global _url_index
    param = f"{symbol},day,{start_date},{end_date},{bars},qfq"
    last_exc: Exception | None = None
    for attempt in range(1, config.FETCH_RETRY + 1):
        url = _BT_URLS[_url_index % len(_BT_URLS)]
        try:
            resp = _session.get(url, params={"param": param}, timeout=25)
            payload = resp.json()
            node = (payload.get("data") or {}).get(symbol) or {}
            arr = node.get("qfqday") or node.get("day") or []
            if not arr:
                raise DataSourceError(f"{symbol} 返回空数据")
            return arr
        except Exception as exc:  # noqa: BLE001 —— 网络层异常统一重试
            last_exc = exc
            _url_index += 1  # 换镜像域名
            if attempt < config.FETCH_RETRY:
                time.sleep(config.FETCH_RETRY_DELAY)
    raise DataSourceError(f"{symbol} 抓取失败: {last_exc}")


def fetch_stock_klines(code: str, start_date: str, end_date: str) -> list[dict]:
    """线程池内执行：拉取单只股票近 3 年前复权日线并整理为落库行。"""
    arr = _fetch_tencent(_symbol_of(code), start_date, end_date, config.BT_KLINE_BARS)
    rows: list[dict] = []
    for item in arr:
        try:
            date = str(item[0])
            o, c, h, low = (_to_float(item[1]), _to_float(item[2]),
                            _to_float(item[3]), _to_float(item[4]))
            vol = _to_float(item[5]) if len(item) > 5 else None
        except (IndexError, TypeError):
            continue
        if not date or c is None or c <= 0:
            continue
        rows.append({
            "code": code, "trade_date": date,
            "open": o, "high": h, "low": low, "close": c, "volume": vol,
        })
    return rows


def fetch_index_klines(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """拉取基准指数日线（沪深300）。指数不复权，qfq 与原始价一致。"""
    arr = _fetch_tencent(symbol, start_date, end_date, config.BT_KLINE_BARS)
    rows: list[dict] = []
    for item in arr:
        try:
            date = str(item[0])
            o, c, h, low = (_to_float(item[1]), _to_float(item[2]),
                            _to_float(item[3]), _to_float(item[4]))
        except (IndexError, TypeError):
            continue
        if not date or c is None or c <= 0:
            continue
        rows.append({"symbol": symbol, "trade_date": date,
                     "open": o, "high": h, "low": low, "close": c})
    return rows


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
def _upsert_bt_klines(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO bt_klines "
        "(code, trade_date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
        [(r["code"], r["trade_date"], r["open"], r["high"], r["low"],
          r["close"], r["volume"]) for r in rows],
    )
    return len(rows)


def _upsert_bt_index(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO bt_index "
        "(symbol, trade_date, open, high, low, close) VALUES (?,?,?,?,?,?)",
        [(r["symbol"], r["trade_date"], r["open"], r["high"], r["low"],
          r["close"]) for r in rows],
    )
    return len(rows)


def _cached_bar_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """各股票已缓存的K线根数（用于断点续传判断）。"""
    return {
        str(r[0]): int(r[1])
        for r in conn.execute("SELECT code, COUNT(*) FROM bt_klines GROUP BY code")
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_bt_data(
    conn: sqlite3.Connection,
    codes: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
    progress=None,
) -> dict:
    """构建回测数据集：全量拉取股票日线与基准指数，返回覆盖率报告。

    codes 为空时取 stocks 表中全部符合板块要求的股票（剔除北交所，
    与看板基础过滤的口径一致）。
    """
    from app.db import load_stocks  # 延迟导入，避免循环依赖

    ensure_tables(conn)
    start_date = start_date or default_start_date()
    end_date = end_date or datetime.now().strftime("%Y-%m-%d")

    if codes is None:
        stocks = load_stocks(conn)
        # 与看板基础过滤一致：仅保留沪深主板/创业/科创，且剔除 ST/*ST/退市整理
        codes = [
            c for c, row in stocks.items()
            if c.startswith(config.KEEP_CODE_PREFIXES)
            and not any(kw in str(row["name"] or "").upper()
                        for kw in config.EXCLUDE_NAME_KEYWORDS)
        ]
    codes = sorted(set(codes))

    cached = {} if force else _cached_bar_counts(conn)
    todo = [c for c in codes
            if cached.get(c, 0) < config.BT_MIN_BARS_FOR_READY]
    skipped = len(codes) - len(todo)

    if progress:
        progress(f"回测数据准备：共 {len(codes)} 只，需拉取 {len(todo)} 只"
                 f"（已缓存 {skipped} 只跳过），区间 {start_date} ~ {end_date}")

    ok = 0
    empty = 0
    failed: list[str] = []
    total_rows = 0
    t0 = time.time()

    def work(code: str) -> tuple[str, list[dict] | None, str | None]:
        try:
            return code, fetch_stock_klines(code, start_date, end_date), None
        except Exception as exc:  # noqa: BLE001
            return code, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=config.BT_FETCH_WORKERS) as pool:
        futures = {pool.submit(work, c): c for c in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            code, rows, err = fut.result()
            if err is not None:
                failed.append(f"{code}({err})")
            elif not rows:
                empty += 1
            else:
                total_rows += _upsert_bt_klines(conn, rows)
                ok += 1
            if i % config.DB_COMMIT_EVERY == 0 or i == len(todo):
                conn.commit()
                if progress:
                    speed = i / max(time.time() - t0, 1e-6)
                    eta = (len(todo) - i) / max(speed, 1e-6)
                    progress(f"  股票日线 {i}/{len(todo)}  成功 {ok}  "
                             f"空 {empty}  失败 {len(failed)}  剩余约 {eta:.0f}s")
                    if failed:
                        progress(f"    最近失败示例: {failed[-1]}")

    # 基准指数（沪深300）
    index_rows = 0
    try:
        rows = fetch_index_klines(config.BT_INDEX_SYMBOL, start_date, end_date)
        index_rows = _upsert_bt_index(conn, rows)
        conn.commit()
        if progress:
            progress(f"  基准指数 {config.BT_INDEX_SYMBOL} 写入 {index_rows} 根")
    except Exception as exc:  # noqa: BLE001
        logger.warning("基准指数拉取失败: %s", exc)
        if progress:
            progress(f"  基准指数拉取失败：{exc}")

    report = {
        "start_date": start_date,
        "end_date": end_date,
        "codes": len(codes),
        "fetched": ok,
        "skipped": skipped,
        "empty": empty,
        "failed": len(failed),
        "rows": total_rows,
        "index_rows": index_rows,
        "elapsed_sec": round(time.time() - t0, 1),
        "failed_samples": failed[:20],
    }
    if progress:
        progress(f"回测数据准备完成：{report}")
    return report


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def load_bt_panel(conn: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    """一次性载入全部回测日线，返回 {code: DataFrame}（按日期升序）。

    只读取回测引擎需要的列，控制内存占用。
    """
    ensure_tables(conn)
    df = pd.read_sql_query(
        "SELECT code, trade_date, open, high, low, close, volume "
        "FROM bt_klines ORDER BY code, trade_date",
        conn,
    )
    if df.empty:
        return {}
    panel: dict[str, pd.DataFrame] = {}
    for code, grp in df.groupby("code", sort=False):
        g = grp.drop(columns=["code"]).reset_index(drop=True)
        g["trade_date"] = g["trade_date"].astype(str)
        panel[str(code)] = g
    return panel


def load_index_series(conn: sqlite3.Connection,
                      symbol: str | None = None) -> pd.DataFrame:
    """载入基准指数日线，返回 DataFrame（按日期升序）。"""
    ensure_tables(conn)
    symbol = symbol or config.BT_INDEX_SYMBOL
    df = pd.read_sql_query(
        "SELECT trade_date, open, high, low, close FROM bt_index "
        "WHERE symbol=? ORDER BY trade_date",
        conn, params=(symbol,),
    )
    if not df.empty:
        df["trade_date"] = df["trade_date"].astype(str)
    return df


def calendar_of(panel: dict[str, pd.DataFrame],
                index_df: pd.DataFrame | None = None) -> list[str]:
    """回测交易日历：优先用基准指数日期（最完整），否则取全样本并集。"""
    if index_df is not None and not index_df.empty:
        return list(index_df["trade_date"])
    dates: set[str] = set()
    for df in panel.values():
        dates.update(df["trade_date"].tolist())
    return sorted(dates)


def bt_data_status(conn: sqlite3.Connection) -> dict:
    """回测数据现状（供前端展示与简报引用）。"""
    ensure_tables(conn)
    cur = conn.cursor()
    n_codes = cur.execute("SELECT COUNT(DISTINCT code) FROM bt_klines").fetchone()[0]
    rng = cur.execute(
        "SELECT MIN(trade_date), MAX(trade_date) FROM bt_klines").fetchone()
    n_index = cur.execute(
        "SELECT COUNT(*) FROM bt_index WHERE symbol=?",
        (config.BT_INDEX_SYMBOL,)).fetchone()[0]
    ready = cur.execute(
        "SELECT COUNT(*) FROM (SELECT code FROM bt_klines GROUP BY code "
        "HAVING COUNT(*) >= ?)", (config.BT_MIN_BARS_FOR_READY,)).fetchone()[0]
    return {
        "codes": n_codes,
        "ready_codes": ready,
        "start_date": rng[0],
        "end_date": rng[1],
        "index_rows": n_index,
        "index_symbol": config.BT_INDEX_SYMBOL,
        "index_name": config.BT_INDEX_NAME,
        "ready": n_codes > 0 and n_index > 0,
    }

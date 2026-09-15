# -*- coding: utf-8 -*-
"""SQLite 本地数据缓存层。

设计要点：
- 全部数据落盘 SQLite（data/pattern_lab.db），支持增量更新，
  避免每次扫描重复拉取全市场历史数据。
- 增量策略：每日收盘数据优先用行情快照的当日 OHLC 直接追加；
  若快照「昨收」与缓存中最近收盘价出现相对误差超过容差
  （通常由除权除息引起的前复权价跳变），则对该股做全量前复权重拉。
- 使用标准库 sqlite3，不引入额外 ORM 依赖，降低部署门槛。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable

from app import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stocks (
    code         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    board        TEXT,
    industry     TEXT,
    listing_days INTEGER,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS klines (
    code          TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    open          REAL,
    high          REAL,
    low           REAL,
    close         REAL,
    volume        REAL,
    amount        REAL,
    turnover_rate REAL,
    PRIMARY KEY (code, trade_date)
);

CREATE TABLE IF NOT EXISTS chips (
    code            TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    concentration   REAL,
    updated_at      TEXT,
    PRIMARY KEY (code, trade_date)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT,
    finished_at   TEXT,
    status        TEXT,
    total_scanned INTEGER,
    candidates    INTEGER,
    high_count    INTEGER,
    mid_count     INTEGER,
    message       TEXT
);

CREATE TABLE IF NOT EXISTS scan_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL,
    scan_time     TEXT,
    code          TEXT,
    name          TEXT,
    industry      TEXT,
    board         TEXT,
    pattern_score INTEGER,
    bonus_score   INTEGER,
    total_score   INTEGER,
    breakdown     TEXT,
    metrics       TEXT,
    display       TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_results_run ON scan_results(run_id, total_score DESC);
CREATE INDEX IF NOT EXISTS idx_klines_code_date ON klines(code, trade_date);
"""


_SCHEMA_READY_FOR: str | None = None  # 已执行过建表语句的库路径（详见 connect 说明）


def connect(db_path=None) -> sqlite3.Connection:
    """建立 SQLite 连接（自动建库建表，开启 WAL 提升并发读写）。

    - WAL：允许扫描线程写库的同时接口线程读库，互不阻塞。
    - busy_timeout：写事务短暂持锁时等待而非立即报 locked（此前扫描期间
      点开个股详情偶发「明细数据加载失败」即由此导致）。
    - 建表语句：同一库文件仅在进程内首次连接时执行一次；每次连接都执行
      会在扫描写事务进行中因申请写锁而失败。
    """
    global _SCHEMA_READY_FOR
    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=8000")
    if _SCHEMA_READY_FOR != str(path):
        conn.executescript(_SCHEMA)
        _SCHEMA_READY_FOR = str(path)
    return conn


@contextmanager
def get_conn(db_path=None):
    """连接上下文管理器：用完自动提交并关闭。"""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# meta 表：键值元信息（如最近行业刷新时间）
# ---------------------------------------------------------------------------
def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------------------
# stocks 表：股票基础信息
# ---------------------------------------------------------------------------
def upsert_stocks(conn: sqlite3.Connection, rows: Iterable[dict]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO stocks(code, name, board, industry, listing_days, updated_at) "
        "VALUES(:code, :name, :board, :industry, :listing_days, :updated_at) "
        "ON CONFLICT(code) DO UPDATE SET "
        "name=excluded.name, board=excluded.board, industry=excluded.industry, "
        "listing_days=excluded.listing_days, updated_at=excluded.updated_at",
        [{**r, "updated_at": now} for r in rows],
    )


def load_stocks(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["code"]: r for r in conn.execute("SELECT * FROM stocks")}


# ---------------------------------------------------------------------------
# klines 表：日K缓存与增量维护
# ---------------------------------------------------------------------------
def upsert_klines(conn: sqlite3.Connection, rows: Iterable[dict]) -> None:
    conn.executemany(
        "INSERT INTO klines(code, trade_date, open, high, low, close, "
        "volume, amount, turnover_rate) "
        "VALUES(:code, :trade_date, :open, :high, :low, :close, "
        ":volume, :amount, :turnover_rate) "
        "ON CONFLICT(code, trade_date) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume, amount=excluded.amount, "
        "turnover_rate=excluded.turnover_rate",
        rows,
    )


def last_kline(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM klines WHERE code=? ORDER BY trade_date DESC LIMIT 1",
        (code,),
    ).fetchone()


def load_klines(conn: sqlite3.Connection, code: str) -> list[sqlite3.Row]:
    """按日期升序读取单只股票全部缓存K线。"""
    return conn.execute(
        "SELECT * FROM klines WHERE code=? ORDER BY trade_date", (code,)
    ).fetchall()


def kline_codes(conn: sqlite3.Connection) -> set[str]:
    return {r["code"] for r in conn.execute("SELECT DISTINCT code FROM klines")}


# ---------------------------------------------------------------------------
# chips 表：筹码集中度缓存
# ---------------------------------------------------------------------------
def upsert_chips(conn: sqlite3.Connection, rows: Iterable[dict]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO chips(code, trade_date, concentration, updated_at) "
        "VALUES(:code, :trade_date, :concentration, :updated_at) "
        "ON CONFLICT(code, trade_date) DO UPDATE SET concentration=excluded.concentration",
        [{**r, "updated_at": now} for r in rows],
    )


def latest_chip(conn: sqlite3.Connection, code: str) -> float | None:
    row = conn.execute(
        "SELECT concentration FROM chips WHERE code=? "
        "ORDER BY trade_date DESC LIMIT 1",
        (code,),
    ).fetchone()
    if row and row["concentration"] is not None:
        return float(row["concentration"])
    return None


# ---------------------------------------------------------------------------
# scan_runs / scan_results 表：扫描记录与结果
# ---------------------------------------------------------------------------
def create_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO scan_runs(started_at, status) VALUES(?, ?)",
        (datetime.now().isoformat(timespec="seconds"), "running"),
    )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    total_scanned: int,
    candidates: int,
    high_count: int,
    mid_count: int,
    message: str = "",
) -> None:
    conn.execute(
        "UPDATE scan_runs SET finished_at=?, status=?, total_scanned=?, "
        "candidates=?, high_count=?, mid_count=?, message=? WHERE id=?",
        (
            datetime.now().isoformat(timespec="seconds"),
            status,
            total_scanned,
            candidates,
            high_count,
            mid_count,
            message,
            run_id,
        ),
    )


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM scan_runs WHERE id=?", (run_id,)).fetchone()


def latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def latest_result_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """最近一次已成功完成（success）的批次。

    扫描启动时会先插入一条 running 记录但还没有任何结果，读接口若取
    「最新一条」会命中这个空批次，导致候选池 / 行业分布在扫描期间瞬间
    清空。因此所有读路径统一取最近一次已完成的批次。
    """
    return conn.execute(
        "SELECT * FROM scan_runs WHERE status='success' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def save_results(
    conn: sqlite3.Connection, run_id: int, results: list[dict]
) -> None:
    scan_time = datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO scan_results(run_id, scan_time, code, name, industry, board, "
        "pattern_score, bonus_score, total_score, breakdown, metrics, display) "
        "VALUES(:run_id, :scan_time, :code, :name, :industry, :board, "
        ":pattern_score, :bonus_score, :total_score, :breakdown, :metrics, :display)",
        [
            {
                "run_id": run_id,
                "scan_time": scan_time,
                "code": r["code"],
                "name": r["name"],
                "industry": r["industry"],
                "board": r["board"],
                "pattern_score": r["pattern_score"],
                "bonus_score": r["bonus_score"],
                "total_score": r["total_score"],
                "breakdown": json.dumps(r["breakdown"], ensure_ascii=False),
                "metrics": json.dumps(r["metrics"], ensure_ascii=False),
                "display": json.dumps(r["display"], ensure_ascii=False),
            }
            for r in results
        ],
    )


def query_results(
    conn: sqlite3.Connection,
    run_id: int | None = None,
    min_score: int | None = None,
    industry: str | None = None,
    board: str | None = None,
    order: str = "desc",
    limit: int = 500,
    offset: int = 0,
) -> list[dict]:
    """查询候选池结果，支持分数/行业/板块过滤与排序。"""
    if run_id is None:
        row = latest_run(conn)
        run_id = int(row["id"]) if row else 0
    sql = ["SELECT * FROM scan_results WHERE run_id=?"]
    params: list[Any] = [run_id]
    if min_score is not None:
        sql.append("AND total_score >= ?")
        params.append(min_score)
    if industry:
        sql.append("AND industry = ?")
        params.append(industry)
    if board:
        sql.append("AND board = ?")
        params.append(board)
    sql.append(f"ORDER BY total_score {'DESC' if order == 'desc' else 'ASC'}, code")
    sql.append("LIMIT ? OFFSET ?")
    params.extend([limit, offset])
    out = []
    for r in conn.execute(" ".join(sql), params):
        item = dict(r)
        item["breakdown"] = json.loads(item["breakdown"] or "{}")
        item["metrics"] = json.loads(item["metrics"] or "{}")
        item["display"] = json.loads(item["display"] or "{}")
        # 兜底：行业/板块不允许空值（历史批次数据缺失时在读取层补齐）
        item["industry"] = item.get("industry") or "其他"
        item["board"] = item.get("board") or _board_by_prefix(item.get("code", ""))
        out.append(item)
    return out


def result_for_code(
    conn: sqlite3.Connection, code: str, run_id: int | None = None
) -> dict | None:
    """取单只个股在指定批次（缺省最近批次）的档案与得分明细。

    供详情接口直接返回，避免详情展示依赖列表页缓存。
    """
    if run_id is None:
        row = latest_result_run(conn)
        run_id = int(row["id"]) if row else 0
    if not run_id:
        return None
    r = conn.execute(
        "SELECT * FROM scan_results WHERE run_id=? AND code=?", (run_id, code)
    ).fetchone()
    if not r:
        return None
    item = dict(r)
    item["breakdown"] = json.loads(item["breakdown"] or "{}")
    item["metrics"] = json.loads(item["metrics"] or "{}")
    item["display"] = json.loads(item["display"] or "{}")
    item["industry"] = item.get("industry") or "其他"
    item["board"] = item.get("board") or _board_by_prefix(item.get("code", ""))
    return item


def _board_by_prefix(code: str) -> str:
    """按代码前缀判定板块（与 scanner.board_of 同口径，避免循环导入）。"""
    return {
        "60": "沪主板", "68": "科创板", "00": "深主板", "30": "创业板",
    }.get(str(code)[:2], "其他")


def count_results(
    conn: sqlite3.Connection,
    run_id: int,
    min_score: int | None = None,
    industry: str | None = None,
    board: str | None = None,
) -> int:
    sql = ["SELECT COUNT(*) AS c FROM scan_results WHERE run_id=?"]
    params: list[Any] = [run_id]
    if min_score is not None:
        sql.append("AND total_score >= ?")
        params.append(min_score)
    if industry:
        sql.append("AND industry = ?")
        params.append(industry)
    if board:
        sql.append("AND board = ?")
        params.append(board)
    return int(conn.execute(" ".join(sql), params).fetchone()["c"])


def industry_stats(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """按行业聚合候选池统计：候选数、平均分、最高分。"""
    rows = conn.execute(
        "SELECT industry, COUNT(*) AS count, ROUND(AVG(total_score),1) AS avg_score, "
        "MAX(total_score) AS max_score "
        "FROM scan_results WHERE run_id=? GROUP BY industry "
        "ORDER BY count DESC",
        (run_id,),
    )
    return [dict(r) for r in rows]

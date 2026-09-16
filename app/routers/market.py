# -*- coding: utf-8 -*-
"""市场公共数据接口：宏观指数快照、板块统计、行业分布。"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from app import data_source, db
from app.config import DISCLAIMER
from app.scoring import load_hot_industries

router = APIRouter(prefix="/api/market", tags=["市场数据"])

_INDEX_CACHE_KEY = "index_snapshot_cache"
# 指数快照短时缓存（秒）：行情源偶发较慢，缓存可避免每次打开看板都等待。
#
# 缓存里区分两个时间：
#   cached_at   数据时间 —— 展示给用户看（沿用旧快照时保持旧时间，不伪装成刚更新）
#   fetched_at  重试基准 —— 决定这份快照还能用多久
# 完整性不同的快照用不同 TTL：国内/海外两侧都拿到时缓存 5 分钟；
# 只有一侧拿到时只缓存 1 分钟，让缺失的一侧尽快重试补齐
# （曾出现过的真实故障：单侧失败后空列表被锁 5 分钟，宏观面板一直空一行）。
_INDEX_CACHE_TTL_SEC = 300
_INDEX_PARTIAL_TTL_SEC = 60


def _cache_ttl(cached: dict) -> int:
    """完整快照用长 TTL，残缺快照用短 TTL。"""
    complete = bool(cached.get("items")) and bool(cached.get("overseas"))
    return _INDEX_CACHE_TTL_SEC if complete else _INDEX_PARTIAL_TTL_SEC


def _read_index_cache(conn) -> dict | None:
    """读取仍在有效期内的指数快照缓存；过期或损坏返回 None。"""
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?", (_INDEX_CACHE_KEY,)
    ).fetchone()
    if not row:
        return None
    try:
        cached = json.loads(row["value"])
        base = cached.get("fetched_at") or cached.get("cached_at")
        age = (datetime.now() - datetime.fromisoformat(base)).total_seconds()
    except (TypeError, ValueError, KeyError):
        return None
    return cached if age <= _cache_ttl(cached) else None


@router.get("/indices", summary="宏观参考面板：国内指数 + 海外/港股指数")
def indices():
    """国内指数与海外/港股参考指数快照（海外仅展示，不参与个股打分）。

    - 优先命中 3 分钟内的本地缓存，避免首页被慢行情源拖住；
    - 行情源不可用时回退最近一次成功快照（本地缓存兜底，
      保证前端宏观面板始终有数值可展示），并标注数据时间。
    """
    with db.get_conn() as conn:
        cached = _read_index_cache(conn)
        if cached:
            return {
                "items": cached.get("items") or [],
                "overseas": cached.get("overseas") or [],
                "cached_at": cached.get("cached_at"),
                "disclaimer": DISCLAIMER,
            }

    # 本次新抓到的内容（可能某一侧为空）
    fresh_rows: list[dict] = []
    try:
        fresh_rows = data_source.fetch_index_snapshot() or []
    except Exception:  # noqa: BLE001 —— 指数源异常时不让首页报错
        fresh_rows = []
    fresh_overseas: list[dict] = []
    try:
        fresh_overseas = data_source.fetch_overseas_indices() or []
    except Exception:  # noqa: BLE001
        fresh_overseas = []

    cached_at: str | None = None
    with db.get_conn() as conn:
        # 上一份快照，用于「单侧抓取失败」时补齐缺失的一侧
        prev: dict | None = None
        prev_row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (_INDEX_CACHE_KEY,)
        ).fetchone()
        if prev_row:
            try:
                prev = json.loads(prev_row["value"])
            except (TypeError, ValueError):
                prev = None
        prev = prev or {}

        # 关键：某一侧抓取失败时，用上一份成功快照补齐，
        # 绝不能把空列表写进缓存（否则该侧会在整个 TTL 内持续为空）。
        rows = fresh_rows or (prev.get("items") or [])
        overseas = fresh_overseas or (prev.get("overseas") or [])
        reused_prev = bool((not fresh_rows and rows) or (not fresh_overseas and overseas))

        if rows or overseas:
            # 数据时间：只要有一侧沿用了旧快照，整体就标旧时间戳
            # （宁可标旧，也不能把旧数据标成刚刚更新）
            cached_at = prev.get("cached_at") if reused_prev else None
            cached_at = cached_at or _now()
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    _INDEX_CACHE_KEY,
                    json.dumps(
                        {
                            "items": rows,
                            "overseas": overseas,
                            "cached_at": cached_at,
                            "fetched_at": _now(),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
    return {
        "items": rows,
        "overseas": overseas,
        "cached_at": cached_at,
        "disclaimer": DISCLAIMER,
    }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@router.get("/industry-stats", summary="候选池按行业统计")
def industry_stats(run_id: int | None = None):
    """候选池行业分布：各行业候选数/平均分/最高分 + 行业覆盖情况。

    行业口径为申万一级（31 个）。覆盖统计用于自查行业匹配完整度：
    未匹配到行业的样本统一归入「其他」，该比例越低说明行业口径越完整。
    """
    with db.get_conn() as conn:
        if run_id is None:
            latest = db.latest_result_run(conn)
            run_id = int(latest["id"]) if latest else 0
        if run_id == 0:
            raise HTTPException(status_code=404, detail="暂无扫描批次，请先执行扫描")
        items = db.industry_stats(conn, run_id)
    total = sum(int(x["count"]) for x in items)
    other = sum(int(x["count"]) for x in items if x["industry"] == "其他")
    return {
        "run_id": run_id,
        "items": items,
        "total": total,
        "industry_count": sum(1 for x in items if x["industry"] != "其他"),
        "other_count": other,
        "other_pct": round(other / total * 100, 2) if total else 0.0,
        # 热点行业名单随统计一并下发，前端据此在分布图中高亮，
        # 避免前端重复维护一份名单（配置改动只需改 config/hot_industries.json）
        "hot_industries": load_hot_industries(),
        "source_note": "行业分类口径：申万一级行业",
        "disclaimer": DISCLAIMER,
    }


@router.get("/board-stats", summary="候选池按板块统计")
def board_stats(run_id: int | None = None):
    with db.get_conn() as conn:
        if run_id is None:
            latest = db.latest_result_run(conn)
            run_id = int(latest["id"]) if latest else 0
        if run_id == 0:
            raise HTTPException(status_code=404, detail="暂无扫描批次，请先执行扫描")
        rows = conn.execute(
            "SELECT board, COUNT(*) AS count, ROUND(AVG(total_score),1) AS avg_score, "
            "MAX(total_score) AS max_score FROM scan_results WHERE run_id=? "
            "GROUP BY board ORDER BY count DESC",
            (run_id,),
        ).fetchall()
    return {"run_id": run_id, "items": [dict(r) for r in rows], "disclaimer": DISCLAIMER}

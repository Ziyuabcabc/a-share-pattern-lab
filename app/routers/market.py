# -*- coding: utf-8 -*-
"""市场公共数据接口：宏观指数快照、板块统计、行业分布。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query

from app import data_source, db
from app.config import DISCLAIMER

router = APIRouter(prefix="/api/market", tags=["市场数据"])

_INDEX_CACHE_KEY = "index_snapshot_cache"


@router.get("/indices", summary="宏观参考面板：国内指数 + 海外/港股指数")
def indices():
    """国内指数与海外/港股参考指数快照（海外仅展示，不参与个股打分）。

    行情源不可用时回退最近一次成功快照（本地缓存兜底，
    保证前端宏观面板始终有数值可展示），并标注数据时间。
    """
    rows: list[dict] = []
    try:
        rows = data_source.fetch_index_snapshot() or []
    except Exception:  # noqa: BLE001 —— 指数源异常时不让首页报错
        rows = []
    overseas: list[dict] = []
    try:
        overseas = data_source.fetch_overseas_indices() or []
    except Exception:  # noqa: BLE001
        overseas = []

    cached_at: str | None = None
    with db.get_conn() as conn:
        if rows or overseas:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    _INDEX_CACHE_KEY,
                    json.dumps(
                        {
                            "items": rows,
                            "overseas": overseas,
                            "cached_at": _now(),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        else:
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?", (_INDEX_CACHE_KEY,)
            ).fetchone()
            if row:
                try:
                    cached = json.loads(row["value"])
                    rows = cached.get("items") or []
                    overseas = cached.get("overseas") or []
                    cached_at = cached.get("cached_at")
                except (TypeError, ValueError):
                    rows, overseas = [], []
    return {
        "items": rows,
        "overseas": overseas,
        "cached_at": cached_at,
        "disclaimer": DISCLAIMER,
    }


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


@router.get("/industry-stats", summary="候选池按行业统计")
def industry_stats(run_id: int | None = None):
    with db.get_conn() as conn:
        if run_id is None:
            latest = db.latest_run(conn)
            run_id = int(latest["id"]) if latest else 0
        if run_id == 0:
            raise HTTPException(status_code=404, detail="暂无扫描批次，请先执行扫描")
        items = db.industry_stats(conn, run_id)
    return {"run_id": run_id, "items": items, "disclaimer": DISCLAIMER}


@router.get("/board-stats", summary="候选池按板块统计")
def board_stats(run_id: int | None = None):
    with db.get_conn() as conn:
        if run_id is None:
            latest = db.latest_run(conn)
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

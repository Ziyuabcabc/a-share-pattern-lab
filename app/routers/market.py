# -*- coding: utf-8 -*-
"""市场公共数据接口：宏观指数快照、板块统计、行业分布。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app import data_source, db
from app.config import DISCLAIMER

router = APIRouter(prefix="/api/market", tags=["市场数据"])


@router.get("/indices", summary="宏观指数快照（上证/深成/创业板/科创50）")
def indices():
    rows = data_source.fetch_index_snapshot()
    return {"items": rows, "disclaimer": DISCLAIMER}


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

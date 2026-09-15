# -*- coding: utf-8 -*-
"""候选池接口：列表查询、CSV 导出、单股指标明细。

所有返回均为纯统计数据与指标展示，不含任何操作指引类描述。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app import config, db, scanner

router = APIRouter(prefix="/api/pool", tags=["候选池"])


@router.get("/", summary="候选池列表（支持排序与过滤）")
def list_pool(
    run_id: int | None = Query(None, description="扫描批次，缺省取最近一次"),
    min_score: int | None = Query(None, ge=0, le=100, description="综合匹配分下限"),
    industry: str | None = Query(None, description="行业名称过滤"),
    board: str | None = Query(None, description="板块过滤，如 创业板/科创板"),
    order: str = Query("desc", pattern="^(asc|desc)$", description="按匹配分排序方向"),
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    with db.get_conn() as conn:
        if run_id is None:
            latest = db.latest_run(conn)
            run_id = int(latest["id"]) if latest else 0
        items = db.query_results(
            conn, run_id=run_id, min_score=min_score, industry=industry,
            board=board, order=order, limit=limit, offset=offset,
        )
        total = db.count_results(
            conn, run_id=run_id, min_score=min_score, industry=industry, board=board
        )
    return {"run_id": run_id, "total": total, "count": len(items), "items": items}


@router.get("/export", summary="导出候选池 CSV")
def export_pool(
    run_id: int | None = None,
    min_score: int | None = Query(None, ge=0, le=100),
):
    with db.get_conn() as conn:
        if run_id is None:
            latest = db.latest_run(conn)
            run_id = int(latest["id"]) if latest else 0
        items = db.query_results(conn, run_id=run_id, min_score=min_score, limit=100000)
    if not items:
        raise HTTPException(status_code=404, detail="当前批次暂无结果，请先执行扫描")
    path = scanner.export_csv(items)
    return FileResponse(
        path, filename=path.split("/")[-1].split("\\")[-1],
        media_type="text/csv",
    )


@router.get("/industries", summary="候选池覆盖的行业列表")
def pool_industries(run_id: int | None = None):
    with db.get_conn() as conn:
        if run_id is None:
            latest = db.latest_run(conn)
            run_id = int(latest["id"]) if latest else 0
        rows = conn.execute(
            "SELECT DISTINCT industry FROM scan_results "
            "WHERE run_id=? AND industry IS NOT NULL ORDER BY industry",
            (run_id,),
        ).fetchall()
    return {"run_id": run_id, "industries": [r["industry"] for r in rows]}


@router.get("/{code}", summary="单只个股指标明细（K线 + 指标序列）")
def stock_detail(code: str, days: int = Query(120, ge=30, le=400)):
    """返回缓存的日K与对应指标序列，供前端图表展示。

    指标展示口径：MACD(3,6,3)、KDJ(9,3,3)、前复权价格。
    """
    with db.get_conn() as conn:
        rows = db.load_klines(conn, code)
        result = db.result_for_code(conn, code)
    if not rows:
        raise HTTPException(status_code=404, detail="本地缓存中无该个股数据，请先执行扫描")

    # 档案与得分随详情一并返回：抽屉不依赖列表页缓存，口径始终一致
    profile = None
    score = None
    if result:
        profile = {
            "code": result["code"], "name": result["name"],
            "board": result["board"], "industry": result["industry"],
        }
        score = {
            "pattern_score": result["pattern_score"],
            "bonus_score": result["bonus_score"],
            "total_score": result["total_score"],
            "breakdown": result["breakdown"],
            "metrics": result["metrics"],
            "display": result["display"],
        }

    import pandas as pd

    from app.indicators import kdj, macd, ma, volume_stats

    df = pd.DataFrame(
        [
            {
                "trade_date": r["trade_date"], "open": r["open"], "high": r["high"],
                "low": r["low"], "close": r["close"], "volume": r["volume"],
            }
            for r in rows
        ]
    ).tail(days)
    dif_s, dea_s, hist_s = macd(df["close"])
    k_s, d_s, j_s = kdj(df["high"], df["low"], df["close"])
    ma_map = ma(df["close"])
    vol_prev_mean, _ = volume_stats(df["volume"])

    def _series(s):
        return [None if pd.isna(v) else round(float(v), 4) for v in s]

    return {
        "code": code,
        "profile": profile,
        "score": score,
        "note": config.DISCLAIMER,
        "display_note": config.DISPLAY_FIELD_NOTE,
        "volume_prev_mean_5d": round(vol_prev_mean, 2) if vol_prev_mean == vol_prev_mean else None,
        "dates": df["trade_date"].tolist(),
        "kline": {
            "open": _series(df["open"]), "high": _series(df["high"]),
            "low": _series(df["low"]), "close": _series(df["close"]),
            "volume": _series(df["volume"]),
        },
        "indicators": {
            "dif": _series(dif_s), "dea": _series(dea_s), "hist": _series(hist_s),
            "k": _series(k_s), "d": _series(d_s), "j": _series(j_s),
            "ma5": _series(ma_map[5]), "ma10": _series(ma_map[10]),
            "ma20": _series(ma_map[20]), "ma60": _series(ma_map[60]),
        },
    }

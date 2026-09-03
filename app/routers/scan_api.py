# -*- coding: utf-8 -*-
"""扫描控制接口：触发全市场扫描、查询运行状态与统计。

扫描为计算密集型任务（首次全市场约数分钟），采用后台线程执行，
通过状态接口轮询进度。仅限本地调用。
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException

from app import db, scanner
from app.config import DISCLAIMER, HIGH_SCORE_THRESHOLD, MID_SCORE_THRESHOLD

router = APIRouter(prefix="/api/scan", tags=["扫描"])

_state_lock = threading.Lock()
_state: dict = {"running": False, "run_id": None, "progress": []}


@router.post("/run", summary="触发一次全市场扫描（后台执行）")
def run_scan(
    with_chips: bool = False,
    full_refresh: bool = False,
    limit: int | None = None,
):
    """启动扫描。with_chips=True 时对初筛个股抓取筹码集中度（耗时显著增加）。"""
    with _state_lock:
        if _state["running"]:
            raise HTTPException(status_code=409, detail="已有扫描任务在执行中，请稍候")
        _state.update({"running": True, "run_id": None, "progress": []})

    def _job():
        try:
            stats = scanner.run_scan(
                progress=lambda msg: _push(msg),
                with_chips=with_chips,
                limit=limit,
                full_refresh=full_refresh,
            )
            with _state_lock:
                _state.update({"running": False, "run_id": stats["run_id"]})
        except Exception as exc:  # noqa: BLE001 —— 后台线程兜底，保证状态复位
            with _state_lock:
                _state["running"] = False
                _push(f"扫描异常终止: {exc}")

    def _push(msg: str):
        with _state_lock:
            _state["progress"].append(msg)
            _state["progress"] = _state["progress"][-50:]

    threading.Thread(target=_job, daemon=True).start()
    return {"started": True, "note": "请通过 /api/scan/status 轮询进度"}


@router.get("/status", summary="扫描运行状态与最近批次统计")
def scan_status():
    with db.get_conn() as conn:
        latest = db.latest_run(conn)
    with _state_lock:
        running = _state["running"]
        progress = list(_state["progress"])
    return {
        "running": running,
        "progress": progress,
        "latest_run": dict(latest) if latest else None,
        "thresholds": {"high": HIGH_SCORE_THRESHOLD, "mid": MID_SCORE_THRESHOLD},
        "disclaimer": DISCLAIMER,
    }

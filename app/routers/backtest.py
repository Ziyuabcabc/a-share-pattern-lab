# -*- coding: utf-8 -*-
"""历史回测接口（v1.6.0）。

全部为本地历史数据统计结果，不代表未来表现，不构成任何操作指引。

接口一览：
- GET  /api/backtest/summary     回测摘要（分档绩效 + 净值曲线 + 方法学说明）
- GET  /api/backtest/trades.csv  回测样本明细导出
- GET  /api/backtest/status      回测数据与运行状态
- POST /api/backtest/run         重新执行回测（后台）
"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app import config, db
from app.backtest import data as bt_data
from app.backtest import engine as bt_engine

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backtest", tags=["backtest"])

# 回测执行状态（进程内单例：同一时刻只允许一个回测任务）
_run_state: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "message": None,
    "error": None,
}
_lock = threading.Lock()


def _summary_from_db(conn, row) -> dict:
    """读取回测摘要：优先使用落库的 JSON，缺失时由明细表重建。"""
    keys = row.keys() if hasattr(row, "keys") else []
    raw = row["summary"] if "summary" in keys else None
    if raw:
        try:
            payload = json.loads(raw)
            payload["available"] = True
            return payload
        except (TypeError, ValueError):
            logger.warning("回测摘要 JSON 解析失败，改为从明细表重建")

    run_id = int(row["id"])
    stats = bt_engine.load_bt_stats(conn, run_id)
    equity = bt_engine.load_bt_equity(conn, run_id)
    horizons = [int(x) for x in str(row["horizons"] or "").split(",") if x.strip()]
    groups = [{"key": g, "label": bt_engine.GROUP_LABELS[g]}
              for g in bt_engine.GROUP_ORDER]
    return {
        "available": True,
        "run_id": run_id,
        "generated_at": row["created_at"],
        "step": row["step"],
        "horizons": horizons,
        "stock_count": row["stock_count"],
        "obs_count": row["obs_count"],
        "score_max": row["score_max"],
        "benchmark": {"symbol": config.BT_INDEX_SYMBOL, "name": config.BT_INDEX_NAME},
        "groups": groups,
        "stats": stats,
        "stats_by_horizon": {
            str(h): [s for s in stats if int(s["horizon"]) == h] for h in horizons
        },
        "equity": {g: h for g, h in equity.items() if g != bt_engine.BENCHMARK_KEY},
        "benchmark_equity": equity.get(bt_engine.BENCHMARK_KEY, {}),
        "unavailable_items": [
            {"key": k, "points": p, "reason": r}
            for k, p, r in config.BT_UNAVAILABLE_ITEMS
        ],
        "method_notes": bt_engine._method_notes(),
    }


@router.get("/summary")
def summary(run_id: int | None = Query(default=None, description="指定回测批次")):
    """回测摘要：分档绩效统计、净值曲线、方法学说明。"""
    conn = db.connect()
    try:
        row = (conn.execute("SELECT * FROM bt_runs WHERE id=?", (run_id,)).fetchone()
               if run_id else bt_engine.latest_bt_run(conn))
        data_status = bt_data.bt_data_status(conn)
        if row is None:
            return {
                "available": False,
                "reason": "尚未执行历史回测",
                "data_status": data_status,
                "run_state": dict(_run_state),
            }
        payload = _summary_from_db(conn, row)
        payload["data_status"] = data_status
        payload["run_state"] = dict(_run_state)
        return payload
    except Exception as exc:  # noqa: BLE001 —— 接口层不向外抛裸异常
        logger.exception("读取回测摘要失败")
        raise HTTPException(status_code=500, detail=f"读取回测摘要失败: {exc}") from exc
    finally:
        conn.close()


@router.get("/status")
def status():
    """回测数据现状与执行状态。"""
    conn = db.connect()
    try:
        row = bt_engine.latest_bt_run(conn)
        return {
            "data_status": bt_data.bt_data_status(conn),
            "run_state": dict(_run_state),
            "latest_run_id": int(row["id"]) if row else None,
            "latest_created_at": row["created_at"] if row else None,
        }
    finally:
        conn.close()


@router.get("/trades.csv")
def trades_csv(
    run_id: int | None = Query(default=None),
    group: str | None = Query(default=None, description="high / mid / low"),
):
    """导出回测样本明细 CSV（UTF-8 BOM，Excel 直接打开不乱码）。"""
    conn = db.connect()
    try:
        row = (conn.execute("SELECT * FROM bt_runs WHERE id=?", (run_id,)).fetchone()
               if run_id else bt_engine.latest_bt_run(conn))
        if row is None:
            raise HTTPException(status_code=404, detail="尚未执行历史回测")
        rid = int(row["id"])

        sql = ("SELECT obs_date, code, name, board, industry, score, group_name, "
               "entry_date, entry_price, ret_5, ret_10, ret_20 FROM bt_trades "
               "WHERE run_id=?")
        params: list = [rid]
        if group in bt_engine.GROUP_ORDER:
            sql += " AND group_name=?"
            params.append(group)
        sql += " ORDER BY obs_date, score DESC LIMIT ?"
        params.append(config.BT_TRADES_CSV_LIMIT)
        rows = conn.execute(sql, params).fetchall()

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "观察日", "股票代码", "股票名称", "板块", "所属行业", "综合匹配分", "分档",
            "次交易日开盘价", "持有5日收益", "持有10日收益", "持有20日收益",
        ])

        def pct(v):
            return "" if v is None else f"{float(v) * 100:.2f}%"

        groups_cn = {"high": "高匹配分组", "mid": "中匹配分组", "low": "低匹配分组"}
        for r in rows:
            writer.writerow([
                r["obs_date"], r["code"], r["name"] or "", r["board"] or "",
                r["industry"] or "", r["score"], groups_cn.get(r["group_name"], ""),
                "" if r["entry_price"] is None else f"{float(r['entry_price']):.4f}",
                pct(r["ret_5"]), pct(r["ret_10"]), pct(r["ret_20"]),
            ])

        # 合规说明附在文件末尾，保证导出件自带口径提示
        writer.writerow([])
        writer.writerow([config.DISCLAIMER])
        writer.writerow([
            f"回测区间 {row['start_date'] or ''} ~ {row['end_date'] or ''}；"
            f"可复现满分 {row['score_max']}；"
            "筹码集中度、PE 行业分位、换手率三项在历史时点无免费可复现数据，统一计 0 分。"
        ])

        fname = f"backtest-trades-run{rid}.csv"
        return Response(
            content="\ufeff" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    finally:
        conn.close()


def _run_backtest_job(step: int, horizons: tuple[int, ...], start, end) -> None:
    """后台线程内执行回测，状态写入 _run_state。"""
    conn = None
    try:
        conn = db.connect()
        params = bt_engine.BacktestParams(
            start_date=start, end_date=end, step=step, horizons=horizons)
        bt_engine.run_backtest(
            conn, params=params,
            progress=lambda m: _run_state.update({"message": m}),
        )
        with _lock:
            _run_state.update({
                "running": False,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "message": "回测完成",
                "error": None,
            })
    except Exception as exc:  # noqa: BLE001
        logger.exception("回测执行失败")
        with _lock:
            _run_state.update({
                "running": False,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "message": "回测失败",
                "error": f"{type(exc).__name__}: {exc}",
            })
    finally:
        if conn is not None:
            conn.close()


@router.post("/run")
def run_now(
    step: int = Query(default=config.BT_REBALANCE_STEP, ge=1, le=250),
    horizons: str = Query(default=",".join(str(h) for h in config.BT_HORIZONS)),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    """重新执行回测（后台异步，接口立即返回）。"""
    with _lock:
        if _run_state["running"]:
            return {"started": False, "reason": "已有回测任务在执行中",
                    "run_state": dict(_run_state)}
        _run_state.update({
            "running": True,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "message": "回测任务已启动",
            "error": None,
        })

    try:
        hs = tuple(int(x) for x in horizons.split(",") if x.strip())
    except ValueError:
        hs = config.BT_HORIZONS

    threading.Thread(
        target=_run_backtest_job, args=(step, hs, start, end), daemon=True
    ).start()
    return {"started": True, "run_state": dict(_run_state)}

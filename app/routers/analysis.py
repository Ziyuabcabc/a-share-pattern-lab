# -*- coding: utf-8 -*-
"""v1.7.0 研究深度分析接口。

全部为对已有历史回测批次的二次统计结果，不代表未来表现，不构成任何操作指引。

接口一览：
- GET  /api/analysis/industry        分行业回测（全行业三组绩效 + 排名）
- GET  /api/analysis/industry.csv    分行业回测明细导出
- GET  /api/analysis/significance    t 检验与分年度稳健性
- GET  /api/analysis/resonance       日线 + 周线双共振对比
- GET  /api/analysis/sensitivity     参数敏感性分析
- POST /api/analysis/build           后台重建全部 v1.7 分析结果
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
from app.backtest import engine as bt_engine
from app.backtest import industry as bt_industry
from app.backtest import resonance as bt_resonance
from app.backtest import sensitivity as bt_sensitivity
from app.backtest import significance as bt_significance

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/analysis", tags=["analysis"])

_GROUP_CN = {"high": "高匹配分组", "mid": "中匹配分组", "low": "低匹配分组"}

# 后台重建任务状态（进程内单例）
_build_state: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "message": None,
    "error": None,
    "steps": [],
}
_lock = threading.Lock()


def _resolve_run(conn, run_id: int | None):
    """取回测批次（默认最新成功批次）。"""
    row = (conn.execute("SELECT * FROM bt_runs WHERE id=?", (run_id,)).fetchone()
           if run_id else bt_engine.latest_bt_run(conn))
    if row is None:
        raise HTTPException(status_code=404,
                            detail="尚未执行历史回测，无法进行二次分析")
    return row


def _run_horizons(row) -> list[int]:
    """批次摘要中的持仓周期；摘要缺失时回退到 bt_runs.horizons。"""
    try:
        summary = json.loads(row["summary"]) if row["summary"] else {}
    except (TypeError, ValueError):
        summary = {}
    hs = summary.get("horizons")
    if hs:
        return [int(h) for h in hs]
    return [int(x) for x in str(row["horizons"] or "").split(",") if x.strip()]


# ---------------------------------------------------------------------------
# 分行业回测
# ---------------------------------------------------------------------------
@router.get("/industry")
def industry(run_id: int | None = Query(default=None, description="回测批次 ID")):
    """分行业回测：31 个申万一级行业 × 高/中/低匹配分组 × 持仓周期的绩效。"""
    conn = db.connect()
    try:
        row = _resolve_run(conn, run_id)
        rid = int(row["id"])
        horizons = _run_horizons(row)
        rows = bt_industry.load_rows(conn, rid)
        if not rows:
            return {
                "available": False,
                "run_id": rid,
                "reason": "尚未计算分行业回测结果，请先运行 scripts/build_v17_analysis.py",
            }
        return bt_industry.build_summary(conn, rid, horizons, rows)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 —— 接口层不向外抛裸异常
        logger.exception("读取分行业回测失败")
        raise HTTPException(status_code=500, detail=f"读取分行业回测失败: {exc}") from exc
    finally:
        conn.close()


@router.get("/industry.csv")
def industry_csv(run_id: int | None = Query(default=None)):
    """导出分行业回测明细（UTF-8 BOM，Excel 直接打开不乱码）。"""
    conn = db.connect()
    try:
        row = _resolve_run(conn, run_id)
        rid = int(row["id"])
        rows = bt_industry.load_rows(conn, rid)
        if not rows:
            raise HTTPException(status_code=404,
                                detail="尚未计算分行业回测结果")

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["申万一级行业", "分组", "持仓周期（交易日）", "样本数量",
                         "上涨胜率", "平均收益率", "最大回撤"])

        def pct(v):
            return "" if v is None else f"{float(v) * 100:.2f}%"

        for r in sorted(rows, key=lambda x: (x["industry"], x["horizon"], x["group"])):
            writer.writerow([
                r["industry"], _GROUP_CN.get(r["group"], r["group"]), r["horizon"],
                r["samples"], pct(r["win_rate"]), pct(r["avg_return"]),
                pct(r["max_drawdown"]),
            ])

        # 合规说明与口径提示随文件导出，保证导出件自带上下文
        writer.writerow([])
        writer.writerow([config.DISCLAIMER])
        writer.writerow([
            f"回测批次 #{rid}；区间 {row['start_date'] or ''} ~ {row['end_date'] or ''}。"
            "涨幅统计口径：观察日 T 收盘后打分，T+1 开盘价入场，T+n 收盘价出场。"
            "最大回撤由行业内该组按调仓观察点等权聚合后复利累积的净值序列计算。"
            f"单格样本少于 {config.IND_MIN_SAMPLES} 条的组合仅供参考，不宜单独下结论。"
        ])

        fname = f"industry-backtest-run{rid}.csv"
        return Response(
            content="\ufeff" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except HTTPException:
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 统计显著性
# ---------------------------------------------------------------------------
@router.get("/significance")
def significance(run_id: int | None = Query(default=None)):
    """高匹配分组 vs 低匹配分组的 Welch t 检验 + 分年度稳健性。"""
    conn = db.connect()
    try:
        row = _resolve_run(conn, run_id)
        rid = int(row["id"])
        return bt_significance.build_summary(conn, rid, _run_horizons(row))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("读取显著性检验失败")
        raise HTTPException(status_code=500,
                            detail=f"读取显著性检验失败: {exc}") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 多周期共振
# ---------------------------------------------------------------------------
@router.get("/resonance")
def resonance(run_id: int | None = Query(default=None)):
    """日线 + 周线双共振分组与原单日线策略的对比。"""
    conn = db.connect()
    try:
        row = _resolve_run(conn, run_id)
        rid = int(row["id"])
        return bt_resonance.build_summary(conn, rid, _run_horizons(row))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("读取共振对比失败")
        raise HTTPException(status_code=500,
                            detail=f"读取共振对比失败: {exc}") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 参数敏感性
# ---------------------------------------------------------------------------
@router.get("/sensitivity")
def sensitivity(run_id: int | None = Query(default=None)):
    """参数敏感性分析：3 组核心参数各 2 个扰动档的对照结果。"""
    conn = db.connect()
    try:
        row = _resolve_run(conn, run_id)
        return bt_sensitivity.load_summary(conn, int(row["id"]))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("读取参数敏感性失败")
        raise HTTPException(status_code=500,
                            detail=f"读取参数敏感性失败: {exc}") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 后台重建
# ---------------------------------------------------------------------------
def _build_job(run_id: int, skip_sensitivity: bool) -> None:
    """后台线程内重建全部 v1.7 分析结果。"""
    conn = None
    try:
        conn = db.connect()
        row = conn.execute("SELECT * FROM bt_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"回测批次不存在: run_id={run_id}")
        horizons = _run_horizons(row)
        step = row["step"] or config.BT_REBALANCE_STEP

        def say(msg: str) -> None:
            with _lock:
                _build_state["message"] = msg
            logger.info("[v1.7 build] %s", msg)

        say("1/4 分行业回测…")
        rows = bt_industry.compute_rows(conn, run_id, horizons)
        bt_industry.save_rows(conn, run_id, rows)

        say("2/4 周线共振标记…")
        bt_resonance.mark_resonance(conn, run_id, progress=say)

        say("3/4 统计显著性检验（即时计算，无需落库）")
        bt_significance.build_summary(conn, run_id, horizons)

        if skip_sensitivity:
            say("4/4 跳过参数敏感性分析")
        else:
            say("4/4 参数敏感性分析（需跑多个全量回测变体，耗时较长）…")
            payload = bt_sensitivity.run_sensitivity(
                conn, run_id, horizons, step=step, progress=say)
            bt_sensitivity.save_summary(conn, run_id, payload)

        with _lock:
            _build_state.update({
                "running": False,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "message": "分析结果重建完成",
                "error": None,
            })
    except Exception as exc:  # noqa: BLE001
        logger.exception("v1.7 分析重建失败")
        with _lock:
            _build_state.update({
                "running": False,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "message": "分析重建失败",
                "error": f"{type(exc).__name__}: {exc}",
            })
    finally:
        if conn is not None:
            conn.close()


@router.post("/build")
def build(
    run_id: int | None = Query(default=None),
    skip_sensitivity: bool = Query(
        default=False, description="跳过参数敏感性（最耗时，需跑多个全量回测）"),
):
    """后台重建 v1.7 分析结果（接口立即返回，前端轮询 /api/analysis/status）。"""
    conn = db.connect()
    try:
        row = _resolve_run(conn, run_id)
        rid = int(row["id"])
    finally:
        conn.close()

    with _lock:
        if _build_state["running"]:
            return {"started": False, "reason": "已有分析任务在执行中",
                    "build_state": dict(_build_state)}
        _build_state.update({
            "running": True,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "message": "分析任务已启动",
            "error": None,
        })

    threading.Thread(target=_build_job, args=(rid, skip_sensitivity),
                     daemon=True).start()
    return {"started": True, "run_id": rid, "build_state": dict(_build_state)}


@router.get("/status")
def status():
    """分析结果现状与后台任务状态。"""
    conn = db.connect()
    try:
        row = bt_engine.latest_bt_run(conn)
        rid = int(row["id"]) if row else None
        counts = {}
        if rid is not None:
            for table in ("bt_industry", "bt_resonance", "bt_sensitivity"):
                try:
                    counts[table] = int(conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (rid,)
                    ).fetchone()[0])
                except Exception:  # noqa: BLE001 —— 表不存在视为未构建
                    counts[table] = 0
        return {
            "latest_run_id": rid,
            "counts": counts,
            "industry_ready": counts.get("bt_industry", 0) > 0,
            "resonance_ready": counts.get("bt_resonance", 0) > 0,
            "sensitivity_ready": counts.get("bt_sensitivity", 0) > 0,
            "build_state": dict(_build_state),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("读取分析状态失败")
        raise HTTPException(status_code=500, detail=f"读取分析状态失败: {exc}") from exc
    finally:
        conn.close()

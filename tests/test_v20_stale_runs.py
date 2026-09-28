# -*- coding: utf-8 -*-
"""v2.0 回归：扫描批次残留（僵尸 running）的清理与读路径口径统一。

背景：扫描在服务进程内的后台线程中执行，进程被强杀（关机 / 崩溃 / 掉线）时
`finish_run` 不会被调用，scan_runs 会留下永远停留在 running 的僵尸记录。
`/api/scan/status` 曾取「最后一条记录」，于是前端把僵尸批次的 id 当作当前批次
带入候选池 / 导出 / 简报请求，全部返回空数据（实测 total=0，看板首页空白）。

本文件锁定三件事：
1. 服务启动时清理残留批次（main.lifespan -> db.mark_stale_runs）；
2. scan/status 的 latest_run 必须是「最近一次成功批次」；
3. 缺省批次查询与 status 口径一致，不会命中空批次。
"""

from __future__ import annotations

import inspect

from fastapi.testclient import TestClient

from app import db
from app import main as main_mod
from app.main import app

client = TestClient(app)


def _make_run(conn, status: str = "success", **counts) -> int:
    """插入一条批次记录；status 非 running 时按传入值收尾。"""
    run_id = db.create_run(conn)
    if status != "running":
        db.finish_run(
            conn,
            run_id,
            status,
            counts.get("total_scanned", 10),
            counts.get("candidates", 4),
            counts.get("high_count", 2),
            counts.get("mid_count", 1),
            counts.get("message", "errors=0"),
        )
    return run_id


def _sample_result(code: str = "600100") -> dict:
    return {
        "code": code,
        "name": "形态样本A",
        "industry": "电子元件",
        "board": "沪主板",
        "pattern_score": 40.0,
        "bonus_score": 10.0,
        "total_score": 50.0,
        "breakdown": {"macd": 15},
        "metrics": {"close": 10.0},
        "display": {"pe": 15.0},
    }


# ---------------------------------------------------------------------------
# 1. mark_stale_runs：残留批次清理
# ---------------------------------------------------------------------------
def test_mark_stale_runs_marks_running_as_interrupted(patched_source):
    with db.get_conn() as conn:
        stale = _make_run(conn, "running")
        assert db.mark_stale_runs(conn) == 1
        row = db.get_run(conn, stale)
        assert row["status"] == "interrupted"
        assert row["finished_at"], "中断批次必须补上结束时间"
        assert "中断" in (row["message"] or "")


def test_mark_stale_runs_is_noop_without_running(patched_source):
    with db.get_conn() as conn:
        done = _make_run(conn, "success")
        assert db.mark_stale_runs(conn) == 0
        assert db.get_run(conn, done)["status"] == "success"


def test_mark_stale_runs_only_touches_running(patched_source):
    with db.get_conn() as conn:
        ok = _make_run(conn, "success")
        stale_a = _make_run(conn, "running")
        stale_b = _make_run(conn, "running")
        assert db.mark_stale_runs(conn) == 2
        assert db.get_run(conn, ok)["status"] == "success"
        assert db.get_run(conn, stale_a)["status"] == "interrupted"
        assert db.get_run(conn, stale_b)["status"] == "interrupted"


def test_latest_result_run_skips_stale_batch(patched_source):
    with db.get_conn() as conn:
        good = _make_run(conn, "success")
        _make_run(conn, "running")
        db.mark_stale_runs(conn)
        row = db.latest_result_run(conn)
        assert row is not None and int(row["id"]) == good


# ---------------------------------------------------------------------------
# 2. scan/status：回退到最近成功批次
# ---------------------------------------------------------------------------
def test_scan_status_ignores_stale_running_run(patched_source):
    """扫描进行中或进程被强杀后，僵尸批次不得被当作「最新批次」。"""
    with db.get_conn() as conn:
        good = _make_run(conn, "success", candidates=7)
        _make_run(conn, "running")  # 更晚插入、且没有任何结果
    body = client.get("/api/scan/status").json()
    assert body["latest_run"] is not None
    assert body["latest_run"]["id"] == good, "必须回退到最近一次成功批次"
    assert body["latest_run"]["status"] == "success"
    assert body["latest_run"]["candidates"] == 7


def test_scan_status_returns_none_when_no_success_run(patched_source):
    with db.get_conn() as conn:
        _make_run(conn, "running")
    body = client.get("/api/scan/status").json()
    assert body["latest_run"] is None


def test_scan_status_picks_latest_success_among_many(patched_source):
    with db.get_conn() as conn:
        _make_run(conn, "success", candidates=1)
        newest = _make_run(conn, "success", candidates=2)
        _make_run(conn, "running")
    body = client.get("/api/scan/status").json()
    assert body["latest_run"]["id"] == newest
    assert body["latest_run"]["candidates"] == 2


# ---------------------------------------------------------------------------
# 3. 缺省批次查询口径一致
# ---------------------------------------------------------------------------
def test_query_results_default_skips_stale_run(patched_source):
    """run_id 缺省时不能命中空批次，否则候选池会整体消失。"""
    with db.get_conn() as conn:
        good = _make_run(conn, "success")
        db.save_results(conn, good, [_sample_result()])
        _make_run(conn, "running")
    with db.get_conn() as conn:
        rows = db.query_results(conn, run_id=None)
    assert len(rows) == 1
    assert rows[0]["code"] == "600100"


# ---------------------------------------------------------------------------
# 4. 启动清理钩子已接线（结构断言，防止被误删）
# ---------------------------------------------------------------------------
def test_startup_cleanup_hook_is_wired():
    lifespan_src = inspect.getsource(main_mod.lifespan)
    assert "_cleanup_stale_runs()" in lifespan_src, (
        "服务启动必须执行残留批次清理，否则僵尸批次会再次让看板首页空白"
    )
    cleanup_src = inspect.getsource(main_mod._cleanup_stale_runs)
    assert "mark_stale_runs" in cleanup_src


def test_cleanup_stale_runs_helper_works(patched_source):
    """直接调用启动清理函数，确认它真的落库（而不是只写在文档里）。"""
    with db.get_conn() as conn:
        stale = _make_run(conn, "running")
    main_mod._cleanup_stale_runs()
    with db.get_conn() as conn:
        assert db.get_run(conn, stale)["status"] == "interrupted"

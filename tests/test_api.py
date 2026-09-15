# -*- coding: utf-8 -*-
"""API 接口测试：基于 FastAPI TestClient 与合成数据源。

覆盖：触发扫描、状态轮询、候选池查询/过滤/导出、行业与板块统计、
指数快照、个股明细。
"""

from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


def _wait_scan_done(timeout: float = 30.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get("/api/scan/status").json()
        if not body["running"]:
            return body
        time.sleep(0.2)
    raise TimeoutError("扫描未在限时内完成")


def test_scan_flow(patched_source):
    # 触发扫描
    resp = client.post("/api/scan/run?with_chips=true")
    assert resp.status_code == 200
    assert resp.json()["started"] is True

    # 重复触发应返回 409
    resp2 = client.post("/api/scan/run")
    assert resp2.status_code == 409

    # 等待完成
    status = _wait_scan_done()
    run = status["latest_run"]
    assert run is not None and run["status"] == "success"
    assert run["candidates"] == 4

    run_id = run["id"]

    # 候选池列表
    body = client.get(f"/api/pool/?run_id={run_id}").json()
    assert body["total"] == 4
    assert body["items"][0]["total_score"] >= body["items"][-1]["total_score"]

    # 过滤：行业 + 最低分
    body = client.get(
        f"/api/pool/?run_id={run_id}&industry=半导体&min_score=50"
    ).json()
    assert all(
        item["industry"] == "半导体" and item["total_score"] >= 50
        for item in body["items"]
    )

    # 行业列表
    body = client.get(f"/api/pool/industries?run_id={run_id}").json()
    assert "半导体" in body["industries"]

    # CSV 导出
    resp = client.get(f"/api/pool/export?run_id={run_id}")
    assert resp.status_code == 200
    assert "核心形态分(50)" in resp.text

    # 行业/板块统计
    body = client.get(f"/api/market/industry-stats?run_id={run_id}").json()
    assert body["items"], "行业统计不应为空"
    body = client.get(f"/api/market/board-stats?run_id={run_id}").json()
    boards = {item["board"] for item in body["items"]}
    assert "创业板" in boards

    # 指数快照（Mock 数据源；overseas 为空列表）
    body = client.get("/api/market/indices").json()
    assert {i["code"] for i in body["items"]} == {"000001", "399006"}
    assert body["overseas"] == []

    # 个股明细
    body = client.get("/api/pool/600100?days=60").json()
    assert len(body["dates"]) == 60
    assert len(body["indicators"]["hist"]) == 60
    assert body["display_note"]

    # 不存在的个股 -> 404
    assert client.get("/api/pool/999999").status_code == 404


def test_pool_empty_before_scan(patched_source):
    resp = client.get("/api/pool/export")
    assert resp.status_code == 404


def test_dashboard_page(patched_source):
    """看板页面与本地静态资源可访问。"""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "A股历史形态匹配研究看板" in html
    assert "历史数据不等于未来表现" in html
    assert "候选池" in html

    resp = client.get("/static/app.js")
    assert resp.status_code == 200
    resp = client.get("/static/vendor/echarts.min.js")
    assert resp.status_code == 200

    # 健康检查从 / 移至 /api/health
    resp = client.get("/api/health")
    assert resp.json()["status"] == "running"

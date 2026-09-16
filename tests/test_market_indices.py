# -*- coding: utf-8 -*-
"""宏观指数接口的缓存与降级行为测试。

背景（真实故障）：指数快照的国内/海外两部分分别来自不同行情源。
早期实现只要「任意一侧成功」就把整份结果（含失败一侧的空列表）
连同新时间戳写进缓存，结果某一侧源临时失败后，宏观面板会在整个
缓存 TTL（5 分钟）内持续空一行。本组用例锁定修复后的行为：

1. 单侧抓取失败 → 该侧沿用上一次成功快照，不得写入空列表；
2. 两侧都失败 → 整份回退到上一次成功快照；
3. 沿用旧快照时，`cached_at` 必须标注旧时间，不能伪装成刚刚更新。
"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import config, data_source, db
from app.main import app
from app.routers import market

client = TestClient(app)

DOMESTIC_A = [
    {"code": "000001", "name": "上证指数", "close": 3500.0, "change_pct": 0.5, "amount": 6e11},
    {"code": "399006", "name": "创业板指", "close": 2100.0, "change_pct": -0.3, "amount": 2e11},
]
OVERSEAS_A = [{"code": "IXIC", "name": "纳斯达克", "close": 20000.0, "change_pct": 1.2}]
OVERSEAS_B = [{"code": "IXIC", "name": "纳斯达克", "close": 20100.0, "change_pct": 1.5}]


def _expire_cache() -> str:
    """把指数缓存时间戳改到 TTL 之外，强制下一次请求重新抓取。

    返回改写后的时间戳，供调用方断言「沿用旧快照时不得刷新数据时间」。
    """
    import json

    past = (datetime.now() - timedelta(seconds=market._INDEX_CACHE_TTL_SEC + 60))
    stamp = past.isoformat(timespec="seconds")
    with db.get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (market._INDEX_CACHE_KEY,)).fetchone()
        assert row is not None, "前一步应已写入缓存"
        data = json.loads(row["value"])
        # cached_at 与 fetched_at 都要推到 TTL 之外：
        # 前者是展示用的数据时间，后者决定这份快照还能不能直接复用
        data["cached_at"] = stamp
        data["fetched_at"] = stamp
        conn.execute("UPDATE meta SET value=? WHERE key=?",
                     (json.dumps(data, ensure_ascii=False), market._INDEX_CACHE_KEY))
    return stamp


def test_partial_failure_keeps_previous_snapshot(patched_source, monkeypatch):
    """国内源失败时，宏观面板必须继续显示上一次的国内指数，而不是空一行。"""
    monkeypatch.setattr(data_source, "fetch_index_snapshot", lambda: DOMESTIC_A)
    monkeypatch.setattr(data_source, "fetch_overseas_indices", lambda: OVERSEAS_A)
    first = client.get("/api/market/indices").json()
    assert [x["code"] for x in first["items"]] == ["000001", "399006"]
    assert [x["name"] for x in first["overseas"]] == ["纳斯达克"]

    # 国内源挂掉，海外源正常更新（先让缓存过期，强制重新抓取）
    stale_stamp = _expire_cache()

    def boom():
        raise RuntimeError("东财与腾讯国内指数源均不可达")

    monkeypatch.setattr(data_source, "fetch_index_snapshot", boom)
    monkeypatch.setattr(data_source, "fetch_overseas_indices", lambda: OVERSEAS_B)

    second = client.get("/api/market/indices").json()
    assert [x["code"] for x in second["items"]] == ["000001", "399006"], \
        "单侧失败时不得把空列表写进缓存"
    # 成功的一侧用新数据
    assert [x["name"] for x in second["overseas"]] == ["纳斯达克"]
    assert second["overseas"][0]["close"] == 20100.0
    # 沿用了旧快照 → 数据时间必须仍是旧时间戳，不能伪装成刚刚更新
    assert second["cached_at"] == stale_stamp
    assert second["cached_at"] != first["cached_at"]


def test_both_sides_failing_falls_back_to_snapshot(patched_source, monkeypatch):
    """两侧源都失败时整体回退上一次快照，且接口不报错。"""
    monkeypatch.setattr(data_source, "fetch_index_snapshot", lambda: DOMESTIC_A)
    monkeypatch.setattr(data_source, "fetch_overseas_indices", lambda: OVERSEAS_A)
    client.get("/api/market/indices")
    _expire_cache()

    def boom():
        raise RuntimeError("全部行情源不可达")

    monkeypatch.setattr(data_source, "fetch_index_snapshot", boom)
    monkeypatch.setattr(data_source, "fetch_overseas_indices", boom)

    resp = client.get("/api/market/indices")
    assert resp.status_code == 200
    body = resp.json()
    assert [x["code"] for x in body["items"]] == ["000001", "399006"]
    assert [x["name"] for x in body["overseas"]] == ["纳斯达克"]


def test_partial_snapshot_expires_sooner_than_complete(patched_source, monkeypatch):
    """残缺快照只能短时复用，缺失的一侧要尽快重试补齐。"""
    import json

    monkeypatch.setattr(data_source, "fetch_index_snapshot", lambda: DOMESTIC_A)
    monkeypatch.setattr(data_source, "fetch_overseas_indices", lambda: OVERSEAS_A)
    client.get("/api/market/indices")

    def _age(seconds: int) -> None:
        stamp = (datetime.now() - timedelta(seconds=seconds)).isoformat(timespec="seconds")
        with db.get_conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?",
                               (market._INDEX_CACHE_KEY,)).fetchone()
            data = json.loads(row["value"])
            data["fetched_at"] = stamp
            conn.execute("UPDATE meta SET value=? WHERE key=?",
                         (json.dumps(data, ensure_ascii=False), market._INDEX_CACHE_KEY))

    with db.get_conn() as conn:
        # 完整快照：60 秒时仍然有效
        _age(market._INDEX_PARTIAL_TTL_SEC + 10)
        assert market._read_index_cache(conn) is not None

        # 残缺快照（模拟海外一侧丢失）：同样 70 秒后已过期，必须重新抓取
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (market._INDEX_CACHE_KEY,)).fetchone()
        data = json.loads(row["value"])
        data["overseas"] = []
        data["fetched_at"] = (datetime.now() - timedelta(
            seconds=market._INDEX_PARTIAL_TTL_SEC + 10)).isoformat(timespec="seconds")
        conn.execute("UPDATE meta SET value=? WHERE key=?",
                     (json.dumps(data, ensure_ascii=False), market._INDEX_CACHE_KEY))
        assert market._read_index_cache(conn) is None


def test_first_run_with_no_cache_returns_empty_without_error(patched_source, monkeypatch):
    """首次运行且源不可用时，接口返回空列表而不是 500。"""
    def boom():
        raise RuntimeError("行情源不可达")

    monkeypatch.setattr(data_source, "fetch_index_snapshot", boom)
    monkeypatch.setattr(data_source, "fetch_overseas_indices", boom)

    resp = client.get("/api/market/indices")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == [] and body["overseas"] == []
    assert body["disclaimer"] == config.DISCLAIMER

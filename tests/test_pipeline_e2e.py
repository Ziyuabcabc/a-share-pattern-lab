# -*- coding: utf-8 -*-
"""端到端管线测试：用合成数据替换数据源，验证 扫描->打分->落库->CSV 全流程。

覆盖：基础过滤剔除（ST/北交所/停牌/次新）、打分规则、筹码缺失容错、
CSV 导出、增量缓存幂等（二次扫描结果一致）。
"""

from app import db, scanner


def _run_scan(patched_source, **kwargs):
    return scanner.run_scan(**kwargs)


def _load_results(run_id):
    with db.get_conn() as conn:
        return db.query_results(conn, run_id=run_id, limit=1000)


def test_full_scan_pipeline(patched_source):
    stats = scanner.run_scan(with_chips=True)

    # 预筛剔除：ST / 北交所 / 停牌 → 剩 5 只（含次新股，进入基础过滤后剔除）
    assert stats["total_scanned"] == 5
    assert stats["candidates"] == 4
    codes = {r["code"] for r in _load_results(stats["run_id"])}
    assert codes == {"600100", "300100", "688100", "000100"}

    # 末日 2 倍量 → 放量 +15 与 +10 加分必然命中
    results = {r["code"]: r for r in _load_results(stats["run_id"])}
    for item in results.values():
        assert item["breakdown"]["pattern"]["volume_surge"] is True
        assert item["breakdown"]["bonus"]["volume_above_mean"] is True

    # 末日温和上行 → MACD金叉与KDJ金叉应命中
    for item in results.values():
        assert item["breakdown"]["pattern"]["macd_gold_red"] is True
        assert item["breakdown"]["pattern"]["kdj_gold_j_under_100"] is True

    # 筹码：600100=12 → 形态项命中；300100=25 → 松散加分；688100 缺失 → 双双为 False 且不崩溃
    assert results["600100"]["breakdown"]["pattern"]["chip_concentrated"] is True
    assert results["600100"]["breakdown"]["bonus"]["chip_loose"] is False
    assert results["300100"]["breakdown"]["bonus"]["chip_loose"] is True
    assert results["688100"]["breakdown"]["pattern"]["chip_concentrated"] is False
    assert results["688100"]["metrics"]["chip_concentration"] is None

    # 板块加分：300100/688100 各 +12
    assert results["300100"]["breakdown"]["bonus"]["growth_board"] is True
    assert results["688100"]["breakdown"]["bonus"]["growth_board"] is True
    assert results["600100"]["breakdown"]["bonus"]["growth_board"] is False

    # 总分范围 0-100
    for item in results.values():
        assert 0 <= item["total_score"] <= 100


def test_scan_is_idempotent_and_incremental(patched_source):
    """二次扫描应成功执行且结果一致（缓存命中，不重复全量拉取）。"""
    s1 = scanner.run_scan()
    s2 = scanner.run_scan()
    assert s1["candidates"] == s2["candidates"]
    r1 = {r["code"]: r["total_score"] for r in _load_results(s1["run_id"])}
    r2 = {r["code"]: r["total_score"] for r in _load_results(s2["run_id"])}
    assert r1 == r2


def test_csv_export(patched_source):
    stats = scanner.run_scan()
    items = _load_results(stats["run_id"])
    path = scanner.export_csv(items)
    text = open(path, encoding="utf-8-sig").read()
    assert "形态匹配分" in text and "600100" in text

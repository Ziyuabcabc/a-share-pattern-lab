# -*- coding: utf-8 -*-
"""端到端管线测试：用合成数据替换数据源，验证 扫描->打分->落库->CSV 全流程。

覆盖：基础过滤剔除（ST/北交所/停牌/次新）、v1.1 三模块打分、
筹码/PE 缺失容错、CSV 导出、增量缓存幂等（二次扫描结果一致）。
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

    results = {r["code"]: r for r in _load_results(stats["run_id"])}

    # 末日 2 倍量 → 量能 1.3x 命中（2.0 不严格大于 2，2x 叠加项不命中）
    for item in results.values():
        assert item["breakdown"]["core"]["volume_surge_1_3x"] is True
        assert item["breakdown"]["core"]["volume_surge_2x"] is False

    # 末日温和上行 → MACD金叉与KDJ金叉应命中
    for item in results.values():
        assert item["breakdown"]["core"]["macd_gold_red"] is True
        assert item["breakdown"]["core"]["kdj_gold_j_under_100"] is True

    # 换手率：合成数据换手率 1.5% < 3% → 换手项不命中
    for item in results.values():
        assert item["breakdown"]["core"]["turnover_healthy_3_15"] is False

    # 筹码：600100=12 → ≤18% 命中；300100=25 → >20% 加至满分8；688100 缺失 → False 且不崩溃
    assert results["600100"]["breakdown"]["fund"]["chip_concentrated_le_18"] is True
    assert results["600100"]["breakdown"]["fund"]["chip_loose_gt_20"] is False
    assert results["300100"]["breakdown"]["fund"]["chip_loose_gt_20"] is True
    assert results["688100"]["breakdown"]["fund"]["chip_concentrated_le_18"] is False
    assert results["688100"]["metrics"]["chip_concentration"] is None

    # 板块与热点：300100/688100 为创业板/科创板 + 行业半导体（热点名单）
    for code in ("300100", "688100"):
        assert results[code]["breakdown"]["industry"]["growth_board"] is True
        assert results[code]["breakdown"]["industry"]["hot_industry"] is True
    assert results["600100"]["breakdown"]["industry"]["growth_board"] is False

    # PE：mock 行业内有效样本数 < 5 → 分位缺失（missing，计 0 分），但 PE 值已入库
    assert results["600100"]["metrics"]["pe"] == 15.0
    assert results["600100"]["breakdown"]["fund"]["pe_tier"] == "missing"

    # 分数结构：pattern=核心形态分，bonus=后两模块合计，总分 0-100
    for item in results.values():
        assert 0 <= item["total_score"] <= 100
        assert item["pattern_score"] == item["breakdown"]["core_score"]
        assert item["bonus_score"] == (
            item["breakdown"]["fund_score"] + item["breakdown"]["industry_score"]
        )


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
    assert "核心形态分(50)" in text and "热点行业" in text and "600100" in text

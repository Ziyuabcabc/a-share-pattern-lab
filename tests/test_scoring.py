# -*- coding: utf-8 -*-
"""打分引擎单元测试：规则边界、筹码缺失容错、基础过滤。"""

import pytest

from app.scoring import (
    ScoreBreakdown,
    StockSnapshot,
    base_filter_reject_reason,
    evaluate,
)


def make_snapshot(**overrides) -> StockSnapshot:
    """构造一只命中全部形态项的默认快照（沪市主板、满量价条件）。"""
    base = dict(
        code="600001",
        name="正常股",
        listing_days=1000,
        amount=1.0e9,
        turnover_rate=3.5,
        dif=0.5,
        dea=0.3,
        hist=0.4,
        k=80.0,
        d=60.0,
        j=90.0,
        volume_prev_mean=100.0,
        volume_today=200.0,      # > 1.3 × 100 → 放量命中；同时 >= 均值 → 加分命中
        chip_concentration=15.0,  # <= 18% → 集中命中；不 > 20% → 松散不命中
        range_high=10.0,
        range_low=8.0,            # 10/8 = 1.25 <= 1.8 → 横盘命中
    )
    base.update(overrides)
    return StockSnapshot(**base)


def test_full_pattern_score_60():
    """四项形态全部命中 → 20+15+15+10 = 60。"""
    bd = evaluate(make_snapshot())
    assert bd.pattern_score == 60
    assert all([bd.pattern_macd, bd.pattern_kdj, bd.pattern_volume, bd.pattern_chip])


def test_full_bonus_28():
    """默认快照：成长板块不命中，其余三项命中 → 12+10+6? = 实际 10+6 = 16。"""
    bd = evaluate(make_snapshot())
    # 沪市主板无 +12；筹码 15 不满足 >20；放量(200>=100)+10；横盘+6
    assert bd.bonus_score == 16
    assert bd.bonus_volume and bd.bonus_range
    assert not bd.bonus_board and not bd.bonus_chip_loose


def test_growth_board_bonus():
    bd = evaluate(make_snapshot(code="300750"))
    assert bd.bonus_board is True
    assert bd.bonus_score == 16 + 12


def test_chip_missing_is_tolerated():
    """筹码数据缺失：对应项计 0 分且不抛异常。"""
    bd = evaluate(make_snapshot(chip_concentration=None))
    assert bd.pattern_chip is False
    assert bd.bonus_chip_loose is False
    assert bd.pattern_score == 50  # 60 - 10


def test_chip_boundary_18_vs_18_1():
    """边界：集中度 = 18 计入；18.1 不计入形态项。"""
    assert evaluate(make_snapshot(chip_concentration=18.0)).pattern_chip is True
    assert evaluate(make_snapshot(chip_concentration=18.1)).pattern_chip is False


def test_chip_loose_bonus_over_20():
    """集中度 > 20% 触发增强加分，且不触发形态项。"""
    bd = evaluate(make_snapshot(chip_concentration=25.0))
    assert bd.pattern_chip is False
    assert bd.bonus_chip_loose is True
    assert bd.bonus_score == 12 + 10 + 6


def test_volume_boundary_1_3x():
    """放量边界：1.3 倍整不计形态项（严格大于），1.31 倍计入。"""
    assert evaluate(make_snapshot(volume_today=130.0)).pattern_volume is False
    assert evaluate(make_snapshot(volume_today=131.0)).pattern_volume is True


def test_volume_equal_mean_bonus_only():
    """当日量恰等于均值：不满足放量，但满足增强加分项。"""
    bd = evaluate(make_snapshot(volume_today=100.0))
    assert bd.pattern_volume is False
    assert bd.bonus_volume is True


def test_macd_condition_requires_dif_above_dea_and_positive_hist():
    bd = evaluate(make_snapshot(dif=0.2, dea=0.3, hist=0.4))
    assert bd.pattern_macd is False
    assert bd.pattern_score == 40  # 60 - 20


def test_kdj_j_over_100_not_scored():
    bd = evaluate(make_snapshot(j=105.0))
    assert bd.pattern_kdj is False
    assert bd.pattern_score == 45  # 60 - 15


def test_range_compact_boundary():
    """近40日 高/低 = 1.8 计入横盘加分；1.9 不计入。"""
    assert evaluate(make_snapshot(range_high=18.0, range_low=10.0)).bonus_range is True
    assert evaluate(make_snapshot(range_high=19.0, range_low=10.0)).bonus_range is False


# ---------------------------------------------------------------------------
# 基础过滤
# ---------------------------------------------------------------------------
def test_base_filter_rejections():
    cases = [
        (make_snapshot(name="ST某股"), "name_contains_ST"),
        (make_snapshot(name="*ST某股"), "name_contains_*ST"),
        (make_snapshot(name="某退市股"), "name_contains_退"),
        (make_snapshot(listing_days=364), "listed_less_than_365_days"),
        (make_snapshot(amount=0.0), "suspended_or_no_trade"),
        (make_snapshot(code="830799"), "non_supported_board"),   # 北交所代码
        (make_snapshot(code="430047"), "non_supported_board"),
    ]
    for snap, expected in cases:
        assert base_filter_reject_reason(snap) == expected


def test_base_filter_pass():
    assert base_filter_reject_reason(make_snapshot()) is None


def test_total_score_range():
    """总分应落在 0-100 区间内。"""
    for chip in (None, 5.0, 25.0):
        bd = evaluate(make_snapshot(chip_concentration=chip))
        assert 0 <= bd.total_score <= 100

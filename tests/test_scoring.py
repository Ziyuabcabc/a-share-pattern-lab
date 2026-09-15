# -*- coding: utf-8 -*-
"""打分引擎单元测试（v1.1）：三模块规则边界、数据缺失容错、基础过滤。"""

import pytest

from app import scoring
from app.scoring import (
    StockSnapshot,
    base_filter_reject_reason,
    compute_pe_percentile,
    evaluate,
    is_hot_industry,
)


def make_snapshot(**overrides) -> StockSnapshot:
    """构造默认快照：命中全部核心形态项（沪主板、换手 3.5%、2 倍量）。

    默认得分：核心 15+12+8+5+5 = 45（量 200/100=2.0 不严格大于 2，双倍量不命中）
    筹码 15% → +3；PE/行业未传 → 0。总分 48。
    """
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
        volume_today=200.0,
        chip_concentration=15.0,
        range_high=10.0,
        range_low=8.0,            # 10/8 = 1.25 <= 1.8 → 横盘命中
    )
    base.update(overrides)
    return StockSnapshot(**base)


# ---------------------------------------------------------------------------
# 模块一：核心形态（0-50）
# ---------------------------------------------------------------------------
def test_default_core_score_45():
    """默认快照：MACD15 + KDJ12 + 量能8 + 换手5 + 横盘5 = 45。"""
    bd = evaluate(make_snapshot())
    assert bd.core_score == 45
    assert all([
        bd.core_macd, bd.core_kdj, bd.core_volume_surge,
        bd.core_turnover_healthy, bd.core_range_compact,
    ])
    assert bd.core_volume_double is False  # 2.0 不严格大于 2.0


def test_volume_double_extra_5():
    """量能 > 2× 均量 → 叠加 5 分，核心满分 50。"""
    bd = evaluate(make_snapshot(volume_today=201.0))
    assert bd.core_volume_double is True
    assert bd.core_score == 50


def test_volume_surge_only_8():
    """量能 1.4 倍：仅 8 分，无 2 倍叠加（其余项均命中）。"""
    bd = evaluate(make_snapshot(volume_today=140.0))
    assert bd.core_volume_surge is True and bd.core_volume_double is False
    assert bd.core_score == 45  # 15+12+8+5+5，量能档为 8 分档


def test_volume_boundary_1_3x_strict():
    """放量边界：1.3 倍整不计（严格大于），1.31 倍计入。"""
    assert evaluate(make_snapshot(volume_today=130.0)).core_volume_surge is False
    assert evaluate(make_snapshot(volume_today=131.0)).core_volume_surge is True


def test_volume_double_boundary_2x_strict():
    """2 倍边界：2.0 整不计叠加项，2.01 计入。"""
    assert evaluate(make_snapshot(volume_today=200.0)).core_volume_double is False
    assert evaluate(make_snapshot(volume_today=201.0)).core_volume_double is True


def test_turnover_healthy_band_3_15():
    """换手率边界：3% 与 15% 含端点命中，2.9/15.1 不命中。"""
    assert evaluate(make_snapshot(turnover_rate=3.0)).core_turnover_healthy is True
    assert evaluate(make_snapshot(turnover_rate=15.0)).core_turnover_healthy is True
    assert evaluate(make_snapshot(turnover_rate=2.9)).core_turnover_healthy is False
    assert evaluate(make_snapshot(turnover_rate=15.1)).core_turnover_healthy is False
    assert evaluate(make_snapshot(turnover_rate=None)).core_turnover_healthy is False


def test_turnover_missing_tolerated():
    """换手率缺失：换手项 0 分，其余照常。"""
    bd = evaluate(make_snapshot(turnover_rate=None))
    assert bd.core_score == 40  # 45 - 5


def test_macd_condition_requires_dif_above_dea_and_positive_hist():
    bd = evaluate(make_snapshot(dif=0.2, dea=0.3, hist=0.4))
    assert bd.core_macd is False
    assert bd.core_score == 30  # 45 - 15


def test_kdj_j_over_100_not_scored():
    bd = evaluate(make_snapshot(j=105.0))
    assert bd.core_kdj is False
    assert bd.core_score == 33  # 45 - 12


def test_range_compact_boundary():
    """近40日 高/低 = 1.8 计入；1.9 不计入。"""
    assert evaluate(make_snapshot(range_high=18.0, range_low=10.0)).core_range_compact is True
    assert evaluate(make_snapshot(range_high=19.0, range_low=10.0)).core_range_compact is False


# ---------------------------------------------------------------------------
# 模块二：筹码与基本面（0-20）
# ---------------------------------------------------------------------------
def test_chip_le_18_gives_3():
    """筹码 <= 18% → 3 分。"""
    bd = evaluate(make_snapshot(chip_concentration=15.0))
    assert bd.fund_chip_concentrated is True
    assert bd.fund_score == 3


def test_chip_over_20_gives_8():
    """筹码 > 20% → 在 3 分基础上再加 5 分，满分 8。"""
    bd = evaluate(make_snapshot(chip_concentration=25.0))
    assert bd.fund_chip_loose is True
    assert bd.fund_score == 8


def test_chip_between_18_and_20_gives_0():
    """筹码 (18%, 20%] 区间 → 0 分。"""
    bd = evaluate(make_snapshot(chip_concentration=19.0))
    assert bd.fund_chip_concentrated is False and bd.fund_chip_loose is False
    assert bd.fund_score == 0


def test_chip_missing_is_tolerated():
    """筹码缺失：计 0 分且不抛异常。"""
    bd = evaluate(make_snapshot(chip_concentration=None))
    assert bd.fund_chip_concentrated is False and bd.fund_chip_loose is False
    assert bd.fund_score == 0


def test_chip_boundary_18_vs_18_1():
    """边界：集中度 = 18 计入；18.1 不计入。"""
    assert evaluate(make_snapshot(chip_concentration=18.0)).fund_chip_concentrated is True
    assert evaluate(make_snapshot(chip_concentration=18.1)).fund_chip_concentrated is False


def test_pe_tier_low_mid_high():
    """PE 分位：<30% → 6 分；30%-70% → 3 分；>70% → 0 分。"""
    assert evaluate(make_snapshot(pe_percentile=20.0)).fund_pe_tier == "low"
    assert evaluate(make_snapshot(pe_percentile=20.0)).fund_score == 3 + 6
    assert evaluate(make_snapshot(pe_percentile=30.0)).fund_pe_tier == "low"
    assert evaluate(make_snapshot(pe_percentile=50.0)).fund_pe_tier == "mid"
    assert evaluate(make_snapshot(pe_percentile=50.0)).fund_score == 3 + 3
    assert evaluate(make_snapshot(pe_percentile=70.0)).fund_pe_tier == "mid"
    assert evaluate(make_snapshot(pe_percentile=80.0)).fund_pe_tier == "high"
    assert evaluate(make_snapshot(pe_percentile=80.0)).fund_score == 3


def test_pe_missing_tolerated():
    """PE 分位缺失：计 0 分。"""
    bd = evaluate(make_snapshot(pe_percentile=None))
    assert bd.fund_pe_tier == "missing"
    assert bd.fund_score == 3


def test_return5_healthy_band_5_20():
    """5日涨幅边界：5% 与 20% 含端点命中，区间外不命中。"""
    assert evaluate(make_snapshot(return_5d_pct=10.0)).fund_return5_healthy is True
    assert evaluate(make_snapshot(return_5d_pct=10.0)).fund_score == 3 + 6
    assert evaluate(make_snapshot(return_5d_pct=5.0)).fund_return5_healthy is True
    assert evaluate(make_snapshot(return_5d_pct=20.0)).fund_return5_healthy is True
    assert evaluate(make_snapshot(return_5d_pct=4.9)).fund_return5_healthy is False
    assert evaluate(make_snapshot(return_5d_pct=None)).fund_return5_healthy is False


def test_compute_pe_percentile():
    """PE 截面分位：越便宜分位越小；样本不足/PE 无效返回 None。"""
    peers = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    assert compute_pe_percentile(5.0, peers) == 0.0      # 最便宜
    assert compute_pe_percentile(35.0, peers) == 50.0    # 中位
    assert compute_pe_percentile(70.0, peers) == 100.0   # 最贵
    assert compute_pe_percentile(None, peers) is None
    assert compute_pe_percentile(-5.0, peers) is None    # 亏损无意义
    assert compute_pe_percentile(20.0, [10.0, 20.0]) is None  # 样本 < 5


# ---------------------------------------------------------------------------
# 模块三：行业与板块（0-30）
# ---------------------------------------------------------------------------
def test_growth_board_8():
    """创业板/科创板 → 8 分（替换旧规则 12 分）。"""
    bd = evaluate(make_snapshot(code="300750"))
    assert bd.ind_growth_board is True
    assert bd.ind_score == 8


def test_main_board_0():
    bd = evaluate(make_snapshot(code="600001"))
    assert bd.ind_growth_board is False and bd.ind_score == 0


def test_hot_industry_prefix_match(monkeypatch):
    """热点行业：名单前缀匹配命中 +22。"""
    monkeypatch.setattr(scoring, "_hot_industries_cache", ["半导体"])
    bd = evaluate(make_snapshot(code="300750", industry="半导体"))
    assert bd.ind_hot is True
    assert bd.ind_score == 8 + 22

    bd2 = evaluate(make_snapshot(industry="食品饮料"))
    assert bd2.ind_hot is False and bd2.ind_score == 0


def test_is_hot_industry_none_safe(monkeypatch):
    monkeypatch.setattr(scoring, "_hot_industries_cache", ["半导体"])
    assert is_hot_industry(None) is False
    assert is_hot_industry("") is False


# ---------------------------------------------------------------------------
# 总分与兼容字段
# ---------------------------------------------------------------------------
def test_total_score_range_and_compat():
    """总分 0-100；pattern_score/bonus_score 兼容旧表结构。"""
    bd = evaluate(make_snapshot(code="300750", industry="半导体", pe_percentile=10.0))
    assert 0 <= bd.total_score <= 100
    assert bd.pattern_score == bd.core_score
    assert bd.bonus_score == bd.fund_score + bd.ind_score
    d = bd.to_dict()
    assert d["version"] == "v1.1"
    assert d["total_score"] == bd.total_score


def test_max_possible_score_100():
    """极端满分：核心50 + 筹码8 + PE6 + 涨幅6 + 板块8 + 热点22 = 100。"""
    bd = evaluate(
        make_snapshot(
            code="300750", industry="半导体", pe_percentile=0.0,
            return_5d_pct=10.0, volume_today=201.0, chip_concentration=25.0,
        )
    )
    assert bd.core_score == 50
    assert bd.fund_score == 8 + 6 + 6
    assert bd.ind_score == 8 + 22
    assert bd.total_score == 100


# ---------------------------------------------------------------------------
# 基础过滤（与 v1.0 保持一致）
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

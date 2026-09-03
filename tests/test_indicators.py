# -*- coding: utf-8 -*-
"""指标计算单元测试：MACD / KDJ 与手工递推结果逐值对照。"""

import math

import pandas as pd

from app.indicators import macd, sma_cn, kdj, volume_stats, hist_peak_stats


def _s(values):
    return pd.Series(values, dtype=float)


def test_macd_hand_computed():
    """收盘 [10,10,10,12,12]：EMA(3) 系数 0.5、EMA(6) 系数 2/7 手工递推对照。"""
    close = _s([10, 10, 10, 12, 12])
    dif, dea, hist = macd(close, fast=3, slow=6, dea_span=3)

    # 手工递推：
    # ema3: 10, 10, 10, 11, 11.5
    # ema6: 10, 10, 10, 10.5714286, 10.9795918
    # dif : 0, 0, 0, 0.4285714, 0.5204082
    # dea : 0, 0, 0, 0.2142857, 0.3673469
    # hist: 0, 0, 0, 0.4285714, 0.3061224
    assert math.isclose(dif.iloc[4], 0.5204082, rel_tol=1e-6)
    assert math.isclose(dea.iloc[4], 0.3673469, rel_tol=1e-6)
    assert math.isclose(hist.iloc[4], 0.3061224, rel_tol=1e-6)
    assert math.isclose(hist.iloc[3], 0.4285714, rel_tol=1e-6)


def test_macd_constant_close_is_zero():
    """价格恒定时 DIF/DEA/柱均应为 0。"""
    close = _s([10.0] * 30)
    dif, dea, hist = macd(close)
    assert abs(dif.iloc[-1]) < 1e-9
    assert abs(dea.iloc[-1]) < 1e-9
    assert abs(hist.iloc[-1]) < 1e-9


def test_sma_cn_recursion():
    """通达信 SMA(X,N,M) 递推：Y = (M*X + (N-M)*Y') / N，首值初始化为 X[0]。"""
    x = _s([50, 100, 100])
    out = sma_cn(x, n=3, m=1)
    assert math.isclose(out.iloc[0], 50.0)
    assert math.isclose(out.iloc[1], (100 + 2 * 50) / 3)          # 66.6667
    assert math.isclose(out.iloc[2], (100 + 2 * (200 / 3)) / 3)   # 77.7778


def test_kdj_hand_computed():
    """单边上行且最高=最低=收盘的序列：RSV 依次 50/100/100，手工递推对照。"""
    close = _s([10, 11, 12])
    k, d, j = kdj(close, close, close, n=9, m1=3, m2=3)
    # k: 50, 66.6667, 77.7778
    # d: 50, 55.5556, 62.9630
    # j3 = 3k - 2d = 107.4074
    assert math.isclose(k.iloc[1], 200 / 3, rel_tol=1e-6)
    assert math.isclose(k.iloc[2], 77.777778, rel_tol=1e-4)
    assert math.isclose(d.iloc[2], 62.962963, rel_tol=1e-4)
    assert math.isclose(j.iloc[2], 107.407407, rel_tol=1e-4)


def test_kdj_flat_limit_board():
    """连续一字板（区间高低价相等）RSV 取中性 50，K=D=J=50。"""
    close = _s([10.0] * 12)
    k, d, j = kdj(close, close, close)
    assert math.isclose(k.iloc[-1], 50.0)
    assert math.isclose(d.iloc[-1], 50.0)
    assert math.isclose(j.iloc[-1], 50.0)


def test_volume_stats_excludes_today():
    """近5日成交量均值不应包含当日。"""
    vol = _s([100, 100, 100, 100, 100, 200])
    prev_mean, today = volume_stats(vol, lookback=5)
    assert math.isclose(prev_mean, 100.0)
    assert math.isclose(today, 200.0)


def test_hist_peak_stats_shrinking():
    """红柱前高收缩判定：窗口内前高 5，当日柱 3 → 收缩为 True。"""
    hist = _s([1, 2, 5, 4, 3])
    peak, today, shrink = hist_peak_stats(hist, lookback=3)
    assert math.isclose(peak, 5.0)
    assert math.isclose(today, 3.0)
    assert shrink is True


def test_hist_peak_stats_not_shrinking_when_negative():
    """当日为绿柱时不应判定收缩。"""
    hist = _s([1, 2, 5, 4, -1])
    _, _, shrink = hist_peak_stats(hist, lookback=3)
    assert shrink is False

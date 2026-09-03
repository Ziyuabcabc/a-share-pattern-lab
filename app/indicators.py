# -*- coding: utf-8 -*-
"""技术指标纯计算模块：MACD、KDJ、均线、成交量统计。

所有函数只做纯数学计算，不访问网络、不读写数据库，便于单独测试。
计算口径与项目历史样本统计保持一致：
- MACD(fast=3, slow=6, dea_span=3)，红绿柱 hist = 2 * (DIF - DEA)（国内行情软件惯例）
- KDJ(9, 3, 3)，其中 SMA 采用通达信口径 Y = (M*X + (N-M)*Y') / N
"""

from __future__ import annotations

import pandas as pd

from app.config import (
    KDJ_M1,
    KDJ_M2,
    KDJ_N,
    MACD_FAST,
    MACD_DEA_SPAN,
    MACD_SLOW,
    MA_WINDOWS,
)


def ema(series: pd.Series, span: int) -> pd.Series:
    """指数移动平均（adjust=False，与国内行情软件递推口径一致）。"""
    return series.ewm(span=span, adjust=False).mean()


def macd(
    close: pd.Series,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    dea_span: int = MACD_DEA_SPAN,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """计算 MACD 三线。

    返回 (dif, dea, hist)，hist 为红绿柱 = 2 * (DIF - DEA)。
    """
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, dea_span)
    hist = 2.0 * (dif - dea)
    return dif, dea, hist


def sma_cn(series: pd.Series, n: int, m: int) -> pd.Series:
    """通达信口径 SMA(X, N, M)：Y = (M * X + (N - M) * Y') / N。

    首值以序列第一个值初始化。Pandas 的 ewm 无法直接表达该口径，
    这里用递推循环实现（序列长度仅几百，性能可接受）。
    """
    values = series.astype(float).tolist()
    out: list[float] = []
    prev: float | None = None
    for x in values:
        if prev is None:
            prev = float(x)
        else:
            prev = (m * float(x) + (n - m) * prev) / n
        out.append(prev)
    return pd.Series(out, index=series.index, dtype=float)


def kdj(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    n: int = KDJ_N,
    m1: int = KDJ_M1,
    m2: int = KDJ_M2,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """计算 KDJ 三线，返回 (k, d, j)。

    RSV = (C - LLV(low, N)) / (HHV(high, N) - LLV(low, N)) * 100
    当区间最高价等于最低价（如连续一字板）时，RSV 取中性值 50。
    """
    llv = low.rolling(n, min_periods=1).min()
    hhv = high.rolling(n, min_periods=1).max()
    denom = (hhv - llv).replace(0.0, float("nan"))  # 区间高低价相等（一字板）时记为 NaN
    rsv = ((close - llv) / denom * 100.0).fillna(50.0)  # NaN 情形取中性值 50
    k = sma_cn(rsv, m1, 1)
    d = sma_cn(k, m2, 1)
    j = 3.0 * k - 2.0 * d
    return k, d, j


def ma(close: pd.Series, windows: tuple[int, ...] = MA_WINDOWS) -> dict[int, pd.Series]:
    """计算多周期简单均线，返回 {周期: 均线序列}。"""
    return {w: close.rolling(w, min_periods=1).mean() for w in windows}


def volume_stats(volume: pd.Series, lookback: int = 5) -> tuple[float, float]:
    """成交量统计。

    返回 (近 lookback 日成交量均值（不含当日）, 当日成交量)。
    样本不足 lookback 日时用现有样本均值，样本为空时返回 (nan, 当日值)。
    """
    vol = volume.astype(float)
    today_vol = float(vol.iloc[-1]) if len(vol) else float("nan")
    prev = vol.iloc[-(lookback + 1) : -1]
    prev_mean = float(prev.mean()) if len(prev) else float("nan")
    return prev_mean, today_vol


def hist_peak_stats(
    hist: pd.Series, lookback: int
) -> tuple[float, float, bool]:
    """红柱前高统计（附加展示字段，不参与打分）。

    定义：取回看窗口（不含当日）内的 MACD 柱最大值作为「前高」，
    当日柱 > 0 且前高 > 0 且当日柱 < 前高 时，判定为「相对前高收缩」。
    返回 (前高柱值, 当日柱值, 是否收缩)。
    """
    h = hist.astype(float)
    today_val = float(h.iloc[-1]) if len(h) else float("nan")
    prev_window = h.iloc[-(lookback + 1) : -1]
    peak = float(prev_window.max()) if len(prev_window) else float("nan")
    shrinking = bool(
        today_val == today_val  # 非 NaN
        and peak == peak
        and today_val > 0
        and peak > 0
        and today_val < peak
    )
    return peak, today_val, shrinking


def range_compact_ratio(
    high: pd.Series, low: pd.Series, lookback: int
) -> tuple[float, float, float]:
    """近 lookback 个交易日的区间振幅统计（含当日）。

    返回 (区间最高价, 区间最低价, 最高/最低比值)。最低价为 0 时返回 inf。
    """
    hh = float(high.iloc[-lookback:].max()) if len(high) else float("nan")
    ll = float(low.iloc[-lookback:].min()) if len(low) else float("nan")
    ratio = float("inf") if ll == 0 else hh / ll
    return hh, ll, ratio

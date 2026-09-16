# -*- coding: utf-8 -*-
"""回测绩效统计口径（纯函数，便于单独测试）。

所有函数只做数学计算，不访问网络与数据库。

口径说明
--------
- 收益率为单笔样本收益（小数，如 0.035 表示 +3.5%）
- 净值序列为按调仓周期复利累积的结果
- 最大回撤为净值序列相对历史高点的最大跌幅，返回**负值**（如 -0.18）
- 盈亏比 = 平均盈利 / |平均亏损|；无亏损样本时返回 None（前端展示为「—」）
"""

from __future__ import annotations

import math

import numpy as np


def _clean(returns) -> np.ndarray:
    """转为一维 float 数组并剔除 NaN / inf。"""
    arr = np.asarray(returns, dtype=float).ravel()
    if arr.size == 0:
        return arr
    return arr[np.isfinite(arr)]


def win_rate(returns) -> float | None:
    """上涨胜率：收益 > 0 的样本占比（0-1）。"""
    arr = _clean(returns)
    if arr.size == 0:
        return None
    return float(np.mean(arr > 0))


def avg_return(returns) -> float | None:
    """平均收益率（算术平均）。"""
    arr = _clean(returns)
    if arr.size == 0:
        return None
    return float(np.mean(arr))


def median_return(returns) -> float | None:
    """收益率中位数（对极端值更稳健）。"""
    arr = _clean(returns)
    if arr.size == 0:
        return None
    return float(np.median(arr))


def win_loss_ratio(returns) -> float | None:
    """盈亏比 = 平均盈利 / |平均亏损|；无亏损或无盈利样本时返回 None。"""
    arr = _clean(returns)
    if arr.size == 0:
        return None
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    if wins.size == 0 or losses.size == 0:
        return None
    avg_win = float(np.mean(wins))
    avg_loss = float(np.mean(losses))
    if avg_loss == 0:
        return None
    return float(avg_win / abs(avg_loss))


def avg_win_loss(returns) -> tuple[float | None, float | None]:
    """返回 (平均盈利, 平均亏损)。平均亏损为负值。"""
    arr = _clean(returns)
    if arr.size == 0:
        return None, None
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    return (float(np.mean(wins)) if wins.size else None,
            float(np.mean(losses)) if losses.size else None)


def build_nav(period_returns) -> np.ndarray:
    """按调仓周期复利构造净值序列。

    输入为按期排列的组合收益（每期等权平均），输出长度与输入相同，
    第 k 个元素 = prod(1 + r_i)，i <= k。
    """
    arr = np.asarray(period_returns, dtype=float).ravel()
    if arr.size == 0:
        return arr
    arr = np.where(np.isfinite(arr), arr, 0.0)
    return np.cumprod(1.0 + arr)


def max_drawdown(nav) -> float | None:
    """最大回撤（负值）。净值序列为空或全为 0 时返回 None。"""
    arr = np.asarray(nav, dtype=float).ravel()
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return None
    peak = np.maximum.accumulate(arr)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, arr / peak - 1.0, 0.0)
    dd = dd[np.isfinite(dd)]
    if dd.size == 0:
        return None
    return float(np.min(dd))


def total_return(nav) -> float | None:
    """区间累计收益率（净值末值 - 1）。"""
    arr = np.asarray(nav, dtype=float).ravel()
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return None
    return float(arr[-1] - 1.0)


def annualized(period_returns, periods_per_year: float) -> float | None:
    """年化收益率（按调仓周期数换算，仅作参考）。"""
    arr = _clean(period_returns)
    if arr.size == 0:
        return None
    nav = float(np.prod(1.0 + arr))
    years = arr.size / periods_per_year
    if years <= 0 or nav <= 0:
        return None
    return float(nav ** (1.0 / years) - 1.0)


def summarize_group(returns, period_returns, nav, benchmark_nav) -> dict:
    """汇总单组单周期的绩效指标。

    参数
    ----
    returns         单笔样本收益序列
    period_returns  按期聚合后的组合收益（等权）
    nav             组合净值序列
    benchmark_nav   基准净值序列（与 nav 同期同长度）
    """
    arr = _clean(returns)
    avg_w, avg_l = avg_win_loss(arr)
    out = {
        "samples": int(arr.size),
        "win_rate": win_rate(arr),
        "avg_return": avg_return(arr),
        "median_return": median_return(arr),
        "avg_win": avg_w,
        "avg_loss": avg_l,
        "pl_ratio": win_loss_ratio(arr),
        "max_drawdown": max_drawdown(nav),
        "total_return": total_return(nav),
        "nav_last": float(np.asarray(nav)[-1]) if len(np.asarray(nav)) else None,
    }
    # 超额收益：组合累计收益率 - 基准同期累计收益率（同一批调仓周期）
    b_nav = np.asarray(benchmark_nav, dtype=float).ravel()
    if b_nav.size and np.isfinite(b_nav[-1]) and b_nav[-1] > 0:
        bench_total = float(b_nav[-1] - 1.0)
        out["benchmark_return"] = bench_total
        if out["total_return"] is not None:
            out["excess_return"] = float(out["total_return"] - bench_total)
        else:
            out["excess_return"] = None
    else:
        out["benchmark_return"] = None
        out["excess_return"] = None
    return out


def safe_float(v, digits: int = 6):
    """把 numpy 浮点转成 JSON 友好的 Python 浮点（保留指定小数位）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, digits)

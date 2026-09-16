# -*- coding: utf-8 -*-
"""统计显著性检验（v1.7.0）：把描述性统计升级为具备推断能力的检验。

v1.6 的分档对比只报告了「高匹配分组的平均收益率高于低匹配分组」这一
**样本内观测差异**，未回答该差异是否可能由随机波动导致。本模块对此做两件事：

1. **独立样本 t 检验**（Welch 形式，不假设两组方差相等）
   对 5 / 10 / 20 个持仓周期，检验高匹配分组与低匹配分组的收益率均值是否
   存在统计显著差异，输出均值差异、t 值、自由度、p 值与该差异的 95% 置信区间。

2. **分年度稳健性检验**
   按自然年拆分回测样本，逐年重复分组对比，检查「高匹配分组优于低匹配分组」
   这一方向在各年度是否一致。用于判断整体结论是否由某一年的极端行情单独驱动。

实现说明（为什么不用 scipy）
----------------------------
本项目的运行依赖刻意保持精简（``requirements.txt`` 仅 4 项），因此这里用
numpy + 标准库 ``math`` 自行实现 t 分布的双尾 p 值：

- t 分布双尾 p 值可用正则化不完全贝塔函数直接表示：
  ``p = I_{ν/(ν+t²)}(ν/2, 1/2)``
- 不完全贝塔函数用连分式（Lentz 算法）数值求解，精度与主流统计库一致
  （测试中以 scipy 为对照基准逐点校验）。

口径与边界
----------
- 收益率序列为**单笔个股—周期样本收益**，非按期聚合的组合收益；
- 检验为**双尾**，显著性水平 ``config.SIG_ALPHA``（默认 5%）；
- 由于调仓观察点间隔不小于最长持仓周期，样本区间互不重叠，
  已尽量降低重叠样本造成的自相关；但同一年度内不同观察点的样本仍可能
  受共同的市场环境冲击影响，p 值应理解为**近似参考**而非严格推断。
"""

from __future__ import annotations

import logging
import math
import sqlite3

import numpy as np

from app import config
from app.backtest import industry as bt_industry
from app.backtest import stats as bt_stats

logger = logging.getLogger(__name__)

# 参与对比的分组（高匹配分组 vs 低匹配分组）
PAIR = ("high", "low")


# ---------------------------------------------------------------------------
# 不完全贝塔函数与 t 分布（纯数值实现）
# ---------------------------------------------------------------------------
def _betacf(a: float, b: float, x: float) -> float:
    """不完全贝塔函数的连分式展开（Lentz 算法），供 ``_betainc`` 调用。"""
    max_iter = 300
    eps = 3.0e-16
    fpmin = 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """正则化不完全贝塔函数 I_x(a, b)，返回 [0, 1]。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    if x < (a + 1.0) / (a + b + 2.0):
        front = math.exp(ln_beta + a * math.log(x) + b * math.log1p(-x))
        return front * _betacf(a, b, x) / a
    front = math.exp(ln_beta + b * math.log1p(-x) + a * math.log(x))
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: float) -> float | None:
    """t 分布双尾 p 值：P(|T| > |t|)，T ~ t(df)。"""
    if df <= 0 or not math.isfinite(t):
        return None
    t = abs(float(t))
    # p = I_{ν/(ν+t²)}(ν/2, 1/2)
    return _betainc(df / 2.0, 0.5, df / (df + t * t))


def t_critical(df: float, alpha: float = config.SIG_ALPHA) -> float | None:
    """双尾临界值 t_{α/2, df}：使 P(|T| > t) = alpha。

    以二分法在 [0, 1e4] 上求解（p 值关于 t 严格单调递减，二分必收敛）。
    """
    if df <= 0:
        return None
    lo, hi = 0.0, 1.0e4
    if t_two_sided_p(hi, df) > alpha:
        return hi
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_two_sided_p(mid, df) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# 独立样本 t 检验（Welch）
# ---------------------------------------------------------------------------
def welch_ttest(a, b, alpha: float = config.SIG_ALPHA) -> dict:
    """Welch 独立样本 t 检验（不假设方差齐性）。

    a、b 分别为两组样本；返回均值差异（a − b）、t 值、自由度、双尾 p 值，
    以及均值差异的 (1 − alpha) 置信区间。
    """
    x = bt_stats._clean(a)
    y = bt_stats._clean(b)
    n1, n2 = int(x.size), int(y.size)
    out = {
        "n1": n1, "n2": n2,
        "mean1": float(np.mean(x)) if n1 else None,
        "mean2": float(np.mean(y)) if n2 else None,
        "var1": float(np.var(x, ddof=1)) if n1 > 1 else None,
        "var2": float(np.var(y, ddof=1)) if n2 > 1 else None,
        "mean_diff": None, "t": None, "df": None, "p_value": None,
        "ci_low": None, "ci_high": None,
        "alpha": alpha, "significant": None,
        "reason": None,
    }
    if n1 < 2 or n2 < 2:
        out["reason"] = "任一分组有效样本不足 2 条，无法进行 t 检验"
        return out

    v1, v2 = out["var1"], out["var2"]
    se2 = v1 / n1 + v2 / n2
    if se2 <= 0:
        out["reason"] = "两组样本方差均为 0，标准误为 0，无法构造检验统计量"
        return out

    diff = out["mean1"] - out["mean2"]
    se = math.sqrt(se2)
    t = diff / se
    # Welch–Satterthwaite 自由度
    df = se2 * se2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    p = t_two_sided_p(t, df)
    tcrit = t_critical(df, alpha)

    out.update({
        "mean_diff": diff, "t": t, "df": df, "p_value": p,
        "se": se,
    })
    if tcrit is not None:
        out["t_critical"] = tcrit
        out["ci_low"] = diff - tcrit * se
        out["ci_high"] = diff + tcrit * se
    if p is not None:
        out["significant"] = bool(p < alpha)
    return out


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------
def _group_returns(conn: sqlite3.Connection, run_id: int, horizon: int,
                   group: str, year: str | None = None) -> np.ndarray:
    """读取某分组某周期的单笔样本收益序列，可选按自然年过滤。"""
    col = bt_industry.HORIZON_COLUMNS.get(int(horizon))
    if col is None:
        return np.array([])
    sql = (f"SELECT {col} AS r FROM bt_trades WHERE run_id=? AND group_name=? "
           f"AND {col} IS NOT NULL")
    params: list = [run_id, group]
    if year:
        sql += " AND substr(obs_date, 1, 4) = ?"
        params.append(year)
    arr = np.array([float(r["r"]) for r in conn.execute(sql, params)], dtype=float)
    return arr[np.isfinite(arr)]


def available_years(conn: sqlite3.Connection, run_id: int) -> list[str]:
    """回测样本覆盖的自然年（升序）。"""
    return sorted(str(r[0]) for r in conn.execute(
        "SELECT DISTINCT substr(obs_date, 1, 4) FROM bt_trades WHERE run_id=?",
        (run_id,)) if r[0])


# ---------------------------------------------------------------------------
# 分组显著性检验
# ---------------------------------------------------------------------------
def format_p(p) -> str:
    """p 值的展示文本。

    极端显著的检验（如 t = 9.5、自由度上万）p 值会小到 1e-21 量级，
    若按固定小数位四舍五入会变成 0.0，看起来像「没算出来」。因此这里
    改用有效数字表示，并对极小值给出明确的「小于」表述。
    """
    if p is None:
        return "—"
    try:
        v = float(p)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    if v < 1e-12:
        return "< 1e-12"
    return f"{v:.4g}"


def compare_groups(conn: sqlite3.Connection, run_id: int, horizons) -> list[dict]:
    """对每个持仓周期做「高匹配分组 vs 低匹配分组」的 Welch t 检验。"""
    hs = bt_industry.supported_horizons(horizons)
    rows: list[dict] = []
    for h in hs:
        a = _group_returns(conn, run_id, h, PAIR[0])
        b = _group_returns(conn, run_id, h, PAIR[1])
        res = welch_ttest(a, b, alpha=config.SIG_ALPHA)
        res["horizon"] = h
        res["enough"] = min(res["n1"], res["n2"]) >= config.SIG_MIN_SAMPLES
        if not res["enough"] and res["reason"] is None:
            res["reason"] = (f"任一分组有效样本少于 {config.SIG_MIN_SAMPLES} 条，"
                             f"检验结果仅供参考")
        # p 值保留原值（不按小数位取整），另给一个展示用文本
        p_raw = res.get("p_value")
        row = {}
        for k, v in res.items():
            if k == "p_value":
                row[k] = bt_stats.safe_float(v, 12)
            elif isinstance(v, float):
                row[k] = bt_stats.safe_float(v, 8)
            else:
                row[k] = v
        row["p_text"] = format_p(p_raw)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 分年度稳健性检验
# ---------------------------------------------------------------------------
def yearly_robustness(conn: sqlite3.Connection, run_id: int, horizons) -> dict:
    """按自然年拆分，逐年检查「高匹配分组优于低匹配分组」的方向是否一致。"""
    hs = bt_industry.supported_horizons(horizons)
    years = available_years(conn, run_id)
    by_year: dict[str, list[dict]] = {}
    for y in years:
        rows = []
        for h in hs:
            hi = _group_returns(conn, run_id, h, PAIR[0], year=y)
            lo = _group_returns(conn, run_id, h, PAIR[1], year=y)
            n1, n2 = int(hi.size), int(lo.size)
            enough = min(n1, n2) >= config.ROBUST_MIN_SAMPLES
            win_hi = bt_stats.win_rate(hi)
            win_lo = bt_stats.win_rate(lo)
            ret_hi = bt_stats.avg_return(hi)
            ret_lo = bt_stats.avg_return(lo)
            rows.append({
                "horizon": h,
                "n_high": n1, "n_low": n2,
                "enough": bool(enough),
                "win_high": bt_stats.safe_float(win_hi, 6),
                "win_low": bt_stats.safe_float(win_lo, 6),
                "ret_high": bt_stats.safe_float(ret_hi, 6),
                "ret_low": bt_stats.safe_float(ret_lo, 6),
                "win_diff": (bt_stats.safe_float(win_hi - win_lo, 6)
                             if (win_hi is not None and win_lo is not None) else None),
                "ret_diff": (bt_stats.safe_float(ret_hi - ret_lo, 6)
                             if (ret_hi is not None and ret_lo is not None) else None),
                # 方向是否一致：高匹配分组的平均收益率高于低匹配分组
                "consistent": (bool(ret_hi > ret_lo)
                               if (ret_hi is not None and ret_lo is not None) else None),
            })
        by_year[y] = rows

    # 汇总：以每个持仓周期为单位，统计有多少个「样本充足」的年份方向一致
    per_horizon: list[dict] = []
    for h in hs:
        valid = [r for y in years for r in by_year[y]
                 if r["horizon"] == h and r["enough"] and r["consistent"] is not None]
        ok = sum(1 for r in valid if r["consistent"])
        per_horizon.append({
            "horizon": h,
            "valid_years": len(valid),
            "consistent_years": ok,
            "consistent_ratio": (round(ok / len(valid), 4) if valid else None),
            "all_consistent": bool(valid) and ok == len(valid),
        })

    return {
        "available": bool(years),
        "years": years,
        "min_samples": config.ROBUST_MIN_SAMPLES,
        "by_year": by_year,
        "per_horizon": per_horizon,
    }


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def build_summary(conn: sqlite3.Connection, run_id: int, horizons) -> dict:
    """接口与简报共用的显著性检验视图。"""
    hs = bt_industry.supported_horizons(horizons)
    if not hs:
        return {"available": False, "reason": "当前回测批次没有可检验的持仓周期"}
    return {
        "available": True,
        "run_id": run_id,
        "horizons": hs,
        "alpha": config.SIG_ALPHA,
        "pair": {"left": PAIR[0], "right": PAIR[1]},
        "tests": compare_groups(conn, run_id, hs),
        "robustness": yearly_robustness(conn, run_id, hs),
    }

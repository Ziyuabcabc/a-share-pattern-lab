# -*- coding: utf-8 -*-
"""筛选打分引擎：基础过滤 + 形态匹配打分 + 增强加分。

分数含义：综合匹配分表示当前个股与历史上升段启动样本的特征相似程度，
不代表未来表现，不构成任何操作指引。

规则总览（参数见 app/config.py）：

【基础过滤】不满足直接剔除，不计分
1. 剔除 ST、*ST、退市整理股票
2. 上市天数 >= 365 天
3. 当日非停牌（成交额 > 0）
4. 剔除北交所股票

【形态匹配打分 0-60】
1. MACD金叉且红柱>0（DIF > DEA 且 MACD柱 > 0）      +20
2. KDJ金叉且J值<100（K > D 且 J < 100）              +15
3. 当日放量：成交量 > 1.3 × 近5日成交量均值          +15
4. 筹码集中度 <= 18%                                 +10（数据缺失计 0 分）

【增强加分 0-40，仅排序加分，不做硬性过滤】
1. 创业板(30)/科创板(68)                              +12
2. 筹码集中度 > 20%                                   +12（数据缺失不计）
3. 当日成交量 >= 近5日成交量均值                      +10
4. 近40个交易日 最高价/最低价 <= 1.8                  +6
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app import config
from app.indicators import hist_peak_stats, range_compact_ratio, volume_stats


@dataclass
class StockSnapshot:
    """单只股票的扫描输入快照：基础信息 + 当日指标计算结果。

    chip_concentration 为最近一个交易日的筹码集中度（百分比，如 12.3），
    数据源缺失时为 None，对应打分项自动计 0 分，不影响整体流程。
    """

    code: str
    name: str
    listing_days: int | None
    amount: float                 # 当日成交额（元），停牌时通常为 0
    turnover_rate: float | None   # 当日换手率（%）

    # 指标序列末值
    dif: float
    dea: float
    hist: float                   # MACD 柱（红绿柱）
    k: float
    d: float
    j: float

    volume_prev_mean: float       # 近5日成交量均值（不含当日）
    volume_today: float

    chip_concentration: float | None = None

    # 近40日高低价（含当日），由调用方按 K 线序列计算后传入
    range_high: float | None = None
    range_low: float | None = None


@dataclass
class ScoreBreakdown:
    """打分明细：每一项的命中情况与得分，供表格与导出展示。"""

    pattern_macd: bool = False
    pattern_kdj: bool = False
    pattern_volume: bool = False
    pattern_chip: bool = False

    bonus_board: bool = False
    bonus_chip_loose: bool = False
    bonus_volume: bool = False
    bonus_range: bool = False

    pattern_score: int = 0
    bonus_score: int = 0

    @property
    def total_score(self) -> int:
        return self.pattern_score + self.bonus_score

    def to_dict(self) -> dict:
        return {
            "pattern": {
                "macd_gold_red": self.pattern_macd,
                "kdj_gold_j_under_100": self.pattern_kdj,
                "volume_surge": self.pattern_volume,
                "chip_concentrated": self.pattern_chip,
            },
            "bonus": {
                "growth_board": self.bonus_board,
                "chip_loose": self.bonus_chip_loose,
                "volume_above_mean": self.bonus_volume,
                "range_compact": self.bonus_range,
            },
            "pattern_score": self.pattern_score,
            "bonus_score": self.bonus_score,
            "total_score": self.total_score,
        }


# ---------------------------------------------------------------------------
# 基础过滤
# ---------------------------------------------------------------------------
def base_filter_reject_reason(snapshot: StockSnapshot) -> str | None:
    """基础过滤：通过返回 None，不通过返回剔除原因（用于日志与调试）。"""
    name = (snapshot.name or "").upper()
    for kw in config.EXCLUDE_NAME_KEYWORDS:
        if kw in name:
            return f"name_contains_{kw}"

    if snapshot.listing_days is None:
        return "listing_days_unknown"
    if snapshot.listing_days < config.FILTER_LISTING_MIN_DAYS:
        return "listed_less_than_365_days"

    if not (snapshot.amount and snapshot.amount > 0):
        return "suspended_or_no_trade"

    if not snapshot.code.startswith(config.KEEP_CODE_PREFIXES):
        return "non_supported_board"

    return None


# ---------------------------------------------------------------------------
# 形态匹配打分（0-60）
# ---------------------------------------------------------------------------
def _pattern_score(snapshot: StockSnapshot, bd: ScoreBreakdown) -> None:
    # 1. MACD金叉且红柱>0：DIF > DEA 且 MACD柱 > 0
    if snapshot.dif > snapshot.dea and snapshot.hist > 0:
        bd.pattern_macd = True
        bd.pattern_score += config.SCORE_MACD_GOLD

    # 2. KDJ金叉且J值<100：K > D 且 J < 100
    if snapshot.k > snapshot.d and snapshot.j < 100:
        bd.pattern_kdj = True
        bd.pattern_score += config.SCORE_KDJ_GOLD

    # 3. 当日放量：成交量 > 1.3 × 近5日成交量均值
    if not math.isnan(snapshot.volume_prev_mean) and snapshot.volume_prev_mean > 0:
        if snapshot.volume_today > config.VOLUME_SURGE_RATIO * snapshot.volume_prev_mean:
            bd.pattern_volume = True
            bd.pattern_score += config.SCORE_VOLUME_SURGE

    # 4. 筹码集中度 <= 18%（数据缺失时不计分，保证流程不中断）
    chip = snapshot.chip_concentration
    if chip is not None and not math.isnan(chip) and chip <= config.CHIP_CONCENTRATION_MAX:
        bd.pattern_chip = True
        bd.pattern_score += config.SCORE_CHIP_CONCENTRATED


# ---------------------------------------------------------------------------
# 增强加分（0-40，仅排序加分）
# ---------------------------------------------------------------------------
def _bonus_score(snapshot: StockSnapshot, bd: ScoreBreakdown) -> None:
    # 1. 创业板(30) / 科创板(68)
    if snapshot.code.startswith(("30", "68")):
        bd.bonus_board = True
        bd.bonus_score += config.BONUS_GROWTH_BOARD

    # 2. 筹码集中度 > 20%（数据缺失不计）
    chip = snapshot.chip_concentration
    if chip is not None and not math.isnan(chip) and chip > config.CHIP_CONCENTRATION_LOOSE:
        bd.bonus_chip_loose = True
        bd.bonus_score += config.BONUS_CHIP_LOOSE

    # 3. 当日成交量 >= 近5日成交量均值
    if not math.isnan(snapshot.volume_prev_mean) and snapshot.volume_prev_mean > 0:
        if snapshot.volume_today >= snapshot.volume_prev_mean:
            bd.bonus_volume = True
            bd.bonus_score += config.BONUS_VOLUME_ABOVE_MA

    # 4. 前期横盘：近40个交易日 最高价/最低价 <= 1.8
    if snapshot.range_high and snapshot.range_low and snapshot.range_low > 0:
        if snapshot.range_high / snapshot.range_low <= config.RANGE_COMPACT_RATIO:
            bd.bonus_range = True
            bd.bonus_score += config.BONUS_RANGE_COMPACT


def evaluate(snapshot: StockSnapshot) -> ScoreBreakdown:
    """对通过基础过滤的单只股票执行完整打分。"""
    bd = ScoreBreakdown()
    _pattern_score(snapshot, bd)
    _bonus_score(snapshot, bd)
    return bd


def derive_display_fields(
    hist_series,
    high_series,
    low_series,
    volume_series,
    turnover_rate: float | None,
    kdj_j: float,
) -> dict:
    """计算附加展示字段（仅展示，不参与打分，不做任何判断）。

    - macd_hist_shrink：MACD红柱相对前高是否收缩（布尔值）
      定义：当日柱 > 0 且回看窗口（不含当日）内柱最大值 > 0 且当日柱 < 前高
    - kdj_j：当日 KDJ-J 值
    - range_high_40d / range_low_40d / range_ratio_40d：近40日区间高低价与振幅比
    - turnover_rate：当日换手率
    """
    peak, today_hist, shrinking = hist_peak_stats(
        hist_series, config.HIST_PEAK_LOOKBACK_DAYS
    )
    hh, ll, ratio = range_compact_ratio(
        high_series, low_series, config.RANGE_LOOKBACK_DAYS
    )
    _, today_vol = volume_stats(volume_series)
    return {
        "macd_hist_shrink": shrinking,
        "macd_hist_peak_prev": peak,
        "macd_hist_today": today_hist,
        "kdj_j": kdj_j,
        "range_high_40d": hh,
        "range_low_40d": ll,
        "range_ratio_40d": ratio,
        "turnover_rate": turnover_rate,
        "volume_today": today_vol,
    }

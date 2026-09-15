# -*- coding: utf-8 -*-
"""筛选打分引擎 v1.1：基础过滤 + 三模块打分（总分 100）。

分数含义：综合匹配分表示当前个股与历史上升段启动样本的特征相似程度，
不代表未来表现，不构成任何操作指引。

规则总览（参数见 app/config.py）：

【基础过滤】不满足直接剔除，不计分（与 v1.0 保持一致）
1. 剔除 ST、*ST、退市整理股票
2. 上市天数 >= 365 天
3. 当日非停牌（成交额 > 0）
4. 剔除北交所股票

【模块一：核心形态匹配分 0-50】
1. MACD(3,6,3)金叉且红柱>0（DIF > DEA 且 MACD柱 > 0）      +15
2. KDJ(9,3,3)金叉且J值<100（K > D 且 J < 100）              +12
3. 量能达标（满分13）：成交量 > 1.3×近5日均量 +8；
   > 2×近5日均量 再 +5
4. 换手率健康度：当日换手率处于 [3%, 15%] 区间              +5
5. 近40个交易日 最高价/最低价 <= 1.8                        +5

【模块二：筹码与基本面加分 0-20】
1. 筹码集中度（满分8）：<= 18% 得 3 分；> 20% 在 3 分基础上
   再加 5 分（即 8 分）；介于两者之间 0 分；数据缺失 0 分
2. PE行业分位（6分）：PE 低于所属行业 30% 分位 +6；
   30%-70% 分位 +3；70% 以上 0。
   数据口径：行业内当日截面分位（免费源无行业 3 年历史 PE 序列，
   降级近似；行业内有效样本 < 5 或 PE 缺失/为负计 0 分）
3. 5日涨幅健康度（6分）：近5日涨幅处于 [5%, 20%] 区间        +6

【模块三：行业与板块加分 0-30】
1. 板块属性：创业板(30)/科创板(68)                          +8
2. 热点行业加成：所属行业命中热点名单（可编辑
   config/hot_industries.json，前缀匹配）                   +22
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field

from app import config
from app.indicators import hist_peak_stats, range_compact_ratio, volume_stats

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 热点行业名单加载（每次扫描/重算时重新读取，编辑即生效）
# ---------------------------------------------------------------------------
_hot_industries_cache: list[str] | None = None


def load_hot_industries(force: bool = False) -> list[str]:
    """读取热点行业名单（config/hot_industries.json）。文件缺失/损坏时返回空名单。"""
    global _hot_industries_cache
    if _hot_industries_cache is not None and not force:
        return _hot_industries_cache
    try:
        with open(config.HOT_INDUSTRIES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        names = [str(x).strip() for x in data.get("industries", []) if str(x).strip()]
    except FileNotFoundError:
        logger.warning("热点行业配置不存在: %s（热点项全部计 0 分）",
                       config.HOT_INDUSTRIES_PATH)
        names = []
    except (ValueError, OSError) as exc:
        logger.warning("热点行业配置读取失败（热点项全部计 0 分）: %s", exc)
        names = []
    _hot_industries_cache = names
    return names


def is_hot_industry(industry: str | None) -> bool:
    """行业名与热点名单做前缀匹配（行业名或名单项任一方向以前缀命中即算）。"""
    if not industry:
        return False
    for name in load_hot_industries():
        if industry == name or industry.startswith(name) or name.startswith(industry):
            return True
    return False


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

    # ---- v1.1 新增输入 ----
    industry: str | None = None       # 所属行业（对照表/行业映射，兜底「其他」）
    pe: float | None = None           # 市盈率 TTM（腾讯行情，缺失 None）
    pe_percentile: float | None = None  # 行业内 PE 截面分位（0-100，由 scanner 预计算）
    return_5d_pct: float | None = None  # 近5日涨幅（%，由调用方按收盘价序列计算）


@dataclass
class ScoreBreakdown:
    """打分明细（v1.1 三模块）：每一项的命中情况与得分，供表格与导出展示。"""

    # 模块一：核心形态
    core_macd: bool = False
    core_kdj: bool = False
    core_volume_surge: bool = False     # >1.3x
    core_volume_double: bool = False    # >2x（叠加 +5）
    core_turnover_healthy: bool = False
    core_range_compact: bool = False

    # 模块二：筹码与基本面
    fund_chip_concentrated: bool = False   # <=18%
    fund_chip_loose: bool = False          # >20%（叠加至满分8）
    fund_pe_tier: str = "missing"          # low / mid / high / missing
    fund_return5_healthy: bool = False

    # 模块三：行业与板块
    ind_growth_board: bool = False
    ind_hot: bool = False

    core_score: int = 0
    fund_score: int = 0
    ind_score: int = 0

    @property
    def total_score(self) -> int:
        return self.core_score + self.fund_score + self.ind_score

    @property
    def pattern_score(self) -> int:
        """兼容旧表结构：scan_results.pattern_score 存核心形态分。"""
        return self.core_score

    @property
    def bonus_score(self) -> int:
        """兼容旧表结构：scan_results.bonus_score 存后两模块合计分。"""
        return self.fund_score + self.ind_score

    def to_dict(self) -> dict:
        return {
            "version": "v1.1",
            "core": {
                "macd_gold_red": self.core_macd,
                "kdj_gold_j_under_100": self.core_kdj,
                "volume_surge_1_3x": self.core_volume_surge,
                "volume_surge_2x": self.core_volume_double,
                "turnover_healthy_3_15": self.core_turnover_healthy,
                "range_compact_40d": self.core_range_compact,
            },
            "fund": {
                "chip_concentrated_le_18": self.fund_chip_concentrated,
                "chip_loose_gt_20": self.fund_chip_loose,
                "pe_tier": self.fund_pe_tier,
                "return5_healthy_5_20": self.fund_return5_healthy,
            },
            "industry": {
                "growth_board": self.ind_growth_board,
                "hot_industry": self.ind_hot,
            },
            "core_score": self.core_score,
            "fund_score": self.fund_score,
            "industry_score": self.ind_score,
            "pattern_score": self.core_score,   # 兼容旧字段
            "bonus_score": self.fund_score + self.ind_score,
            "total_score": self.total_score,
        }


# ---------------------------------------------------------------------------
# 基础过滤（与 v1.0 保持一致）
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
# 模块一：核心形态匹配分（0-50）
# ---------------------------------------------------------------------------
def _core_score(snapshot: StockSnapshot, bd: ScoreBreakdown) -> None:
    # 1. MACD(3,6,3)金叉且红柱>0：DIF > DEA 且 MACD柱 > 0（15分）
    if snapshot.dif > snapshot.dea and snapshot.hist > 0:
        bd.core_macd = True
        bd.core_score += config.SCORE_MACD_GOLD

    # 2. KDJ(9,3,3)金叉且J值<100：K > D 且 J < 100（12分）
    if snapshot.k > snapshot.d and snapshot.j < 100:
        bd.core_kdj = True
        bd.core_score += config.SCORE_KDJ_GOLD

    # 3. 量能达标（满分13）：>1.3x 得 8 分，>2x 再加 5 分
    if not math.isnan(snapshot.volume_prev_mean) and snapshot.volume_prev_mean > 0:
        ratio = snapshot.volume_today / snapshot.volume_prev_mean
        if ratio > config.VOLUME_SURGE_RATIO:
            bd.core_volume_surge = True
            bd.core_score += config.SCORE_VOLUME_SURGE
        if ratio > config.VOLUME_DOUBLE_RATIO:
            bd.core_volume_double = True
            bd.core_score += config.SCORE_VOLUME_DOUBLE

    # 4. 换手率健康度：当日换手率处于 [3%, 15%]（5分；缺失计 0 分）
    t = snapshot.turnover_rate
    if t is not None and not math.isnan(t) and (
        config.TURNOVER_HEALTHY_MIN <= t <= config.TURNOVER_HEALTHY_MAX
    ):
        bd.core_turnover_healthy = True
        bd.core_score += config.SCORE_TURNOVER_HEALTHY

    # 5. 近40个交易日 最高价/最低价 <= 1.8（5分）
    if snapshot.range_high and snapshot.range_low and snapshot.range_low > 0:
        if snapshot.range_high / snapshot.range_low <= config.RANGE_COMPACT_RATIO:
            bd.core_range_compact = True
            bd.core_score += config.SCORE_RANGE_COMPACT


# ---------------------------------------------------------------------------
# 模块二：筹码与基本面加分（0-20）
# ---------------------------------------------------------------------------
def _fund_score(snapshot: StockSnapshot, bd: ScoreBreakdown) -> None:
    # 1. 筹码集中度（满分8）：<=18% 得 3 分；>20% 在 3 分基础上再加 5 分（即 8 分）
    chip = snapshot.chip_concentration
    if chip is not None and not math.isnan(chip):
        if chip <= config.CHIP_CONCENTRATION_MAX:
            bd.fund_chip_concentrated = True
            bd.fund_score += config.SCORE_CHIP_CONCENTRATED
        elif chip > config.CHIP_CONCENTRATION_LOOSE:
            bd.fund_chip_loose = True
            bd.fund_score += (
                config.SCORE_CHIP_CONCENTRATED + config.SCORE_CHIP_LOOSE_EXTRA
            )

    # 2. PE行业分位（6分）：行业内截面分位 <=30% 得 6 分，30%-70% 得 3 分
    pct = snapshot.pe_percentile
    if pct is not None and not math.isnan(pct):
        if pct <= config.PE_PERCENTILE_LOW:
            bd.fund_pe_tier = "low"
            bd.fund_score += config.SCORE_PE_CHEAP
        elif pct <= config.PE_PERCENTILE_HIGH:
            bd.fund_pe_tier = "mid"
            bd.fund_score += config.SCORE_PE_MID
        else:
            bd.fund_pe_tier = "high"

    # 3. 5日涨幅健康度（6分）：近5日涨幅处于 [5%, 20%]
    r = snapshot.return_5d_pct
    if r is not None and not math.isnan(r) and (
        config.RETURN5_MIN_PCT <= r <= config.RETURN5_MAX_PCT
    ):
        bd.fund_return5_healthy = True
        bd.fund_score += config.SCORE_RETURN5_HEALTHY


# ---------------------------------------------------------------------------
# 模块三：行业与板块加分（0-30）
# ---------------------------------------------------------------------------
def _industry_score(snapshot: StockSnapshot, bd: ScoreBreakdown) -> None:
    # 1. 板块属性：创业板(30) / 科创板(68)（8分）
    if snapshot.code.startswith(("30", "68")):
        bd.ind_growth_board = True
        bd.ind_score += config.SCORE_GROWTH_BOARD

    # 2. 热点行业加成（22分）：行业命中可编辑热点名单
    if is_hot_industry(snapshot.industry):
        bd.ind_hot = True
        bd.ind_score += config.SCORE_HOT_INDUSTRY


def evaluate(snapshot: StockSnapshot) -> ScoreBreakdown:
    """对通过基础过滤的单只股票执行 v1.1 完整打分（三模块，总分 100）。"""
    bd = ScoreBreakdown()
    _core_score(snapshot, bd)
    _fund_score(snapshot, bd)
    _industry_score(snapshot, bd)
    return bd


def compute_pe_percentile(pe: float | None, peers_pe: list[float]) -> float | None:
    """计算个股 PE 在同行业截面中的分位（0-100，越小越便宜）。

    peers_pe 为同行业全部有效 PE（>0）列表。个股 PE 缺失/为负、或行业
    有效样本数不足 config.PE_MIN_INDUSTRY_PEERS 时返回 None（该项计 0 分）。
    """
    if pe is None or math.isnan(pe) or pe <= 0:
        return None
    valid = [p for p in peers_pe if p is not None and not math.isnan(p) and p > 0]
    if len(valid) < config.PE_MIN_INDUSTRY_PEERS:
        return None
    below = sum(1 for p in valid if p < pe)
    return round(below / len(valid) * 100, 1)


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

# -*- coding: utf-8 -*-
"""v1.7.0 研究深度升级测试。

覆盖四项新增能力，全部不访问网络：

1. **统计显著性检验**（``app.backtest.significance``）：t 分布双尾 p 值、
   临界值、Welch 检验，与解析解/已知临界值逐位对齐；极端 p 值不得被
   四舍五入成 0。
2. **分行业回测**（``app.backtest.industry``）：聚合口径必须与手工计算
   一致；占位行业（其他/未分类）不得计入任何行业统计；各行业样本合并
   回去必须等于整体口径。
3. **多周期共振**（``app.backtest.resonance``）：周线合成规则、指标只
   依赖历史数据（截断重算前缀一致）、周线「可用日」必须由市场交易日历
   判定而非个股最后成交日。
4. **参数敏感性**（``app.backtest.sensitivity``）：变体定义合法，
   ``ScoreParams`` 覆盖生效且默认值与 config 一致。

另含研究简报九节结构与前端目录键的一致性检查。
"""

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import config, db
from app.backtest import data as bt_data
from app.backtest import engine as bt_engine
from app.backtest import industry as bt_industry
from app.backtest import resonance as bt_resonance
from app.backtest import sensitivity as bt_sensitivity
from app.backtest import significance as bt_sig

ROOT = Path(__file__).resolve().parent.parent
HORIZONS = (5, 10, 20)


# ===========================================================================
# 一、统计显著性检验
# ===========================================================================
# scipy.stats.t.ppf(0.975, df) 的参考值（本研究刻意不依赖 scipy，此处仅作
# 测试基准使用固定常数，避免运行时引入额外依赖）
T_CRIT_REF = {
    1: 12.706204736174694,
    2: 4.302652729911275,
    5: 2.570581835636147,
    10: 2.2281388519649385,
    30: 2.0422724563012373,
    100: 1.9839715184496334,
}


@pytest.mark.parametrize("df,expected", sorted(T_CRIT_REF.items()))
def test_t_critical_matches_reference(df, expected):
    """双尾临界值 t_{0.025, df} 必须与参考值一致（容纳二分法残差）。"""
    assert bt_sig.t_critical(df) == pytest.approx(expected, abs=1e-9)


def test_t_critical_large_df_approaches_normal_quantile():
    """自由度很大时 t 分布退化为标准正态：t_{0.025} → 1.95996398…"""
    assert bt_sig.t_critical(100000) == pytest.approx(1.9599639845, abs=2e-4)


def test_t_critical_none_for_invalid_df():
    assert bt_sig.t_critical(0) is None
    assert bt_sig.t_critical(-3) is None


@pytest.mark.parametrize("df", [1, 2, 5, 10, 30, 100, 5000])
def test_pvalue_is_inverse_of_critical_value(df):
    """p(t_crit(df)) 必须回到显著性水平 α —— 两个函数互为逆运算。

    这是一个强自洽约束：连分式求解与二分求根若有任何一侧偏移，
    往返误差都会立刻暴露。
    """
    tcrit = bt_sig.t_critical(df)
    p = bt_sig.t_two_sided_p(tcrit, df)
    assert p == pytest.approx(config.SIG_ALPHA, abs=1e-10)


def test_pvalue_bounds_and_monotonicity():
    """t = 0 → p = 1；|t| 越大 p 越小，且关于符号对称。"""
    assert bt_sig.t_two_sided_p(0.0, 10) == pytest.approx(1.0)
    assert bt_sig.t_two_sided_p(-2.5, 10) == pytest.approx(
        bt_sig.t_two_sided_p(2.5, 10))
    ps = [bt_sig.t_two_sided_p(t, 12) for t in (0.5, 1.0, 2.0, 4.0, 8.0)]
    assert all(a > b for a, b in zip(ps, ps[1:]))


def test_pvalue_none_for_invalid_input():
    assert bt_sig.t_two_sided_p(1.0, 0) is None
    assert bt_sig.t_two_sided_p(1.0, -5) is None
    assert bt_sig.t_two_sided_p(float("inf"), 10) is None


def test_welch_ttest_matches_hand_computation():
    """构造两组等方差的等差样本，t / df / se / 均值差可手算核对。

    a = [.10 .12 .14 .16 .18]  → mean .14, var(ddof=1) = .001
    b = [.00 .02 .04 .06 .08]  → mean .04, var(ddof=1) = .001
    diff = .10，se = sqrt(.001/5 + .001/5) = .02，t = 5
    Welch 自由度 = 8
    """
    a = [0.10, 0.12, 0.14, 0.16, 0.18]
    b = [0.00, 0.02, 0.04, 0.06, 0.08]
    r = bt_sig.welch_ttest(a, b)

    assert r["n1"] == r["n2"] == 5
    assert r["mean1"] == pytest.approx(0.14)
    assert r["mean2"] == pytest.approx(0.04)
    assert r["var1"] == pytest.approx(0.001)
    assert r["mean_diff"] == pytest.approx(0.10)
    assert r["se"] == pytest.approx(0.02)
    assert r["t"] == pytest.approx(5.0)
    assert r["df"] == pytest.approx(8.0)
    # p 与置信区间必须与同一 df/t 的解析结果自洽
    assert r["p_value"] == pytest.approx(bt_sig.t_two_sided_p(5.0, 8.0))
    assert r["significant"] is True
    # df = 8 的参考临界值（独立常数，用于核对置信区间半宽）
    half = 2.3060041350333704
    assert r["t_critical"] == pytest.approx(half, abs=1e-9)
    assert r["ci_low"] == pytest.approx(0.10 - half * 0.02)
    assert r["ci_high"] == pytest.approx(0.10 + half * 0.02)


def test_welch_ttest_ci_contains_zero_when_not_significant():
    """两组高度重叠时 p > α，且置信区间跨越 0。"""
    rng = np.random.default_rng(20260916)
    a = rng.normal(0.01, 0.05, 400)
    b = rng.normal(0.01, 0.05, 400)
    r = bt_sig.welch_ttest(a, b)
    assert r["significant"] is False
    assert r["ci_low"] < 0 < r["ci_high"]
    assert r["p_value"] > config.SIG_ALPHA


def test_welch_ttest_degenerate_samples():
    """样本不足或方差为 0 时必须给出原因，而不是伪造统计量。"""
    short = bt_sig.welch_ttest([0.01], [0.02, 0.03, 0.04])
    assert short["p_value"] is None and short["t"] is None
    assert short["reason"]

    flat = bt_sig.welch_ttest([0.01, 0.01, 0.01], [0.02, 0.02, 0.02])
    assert flat["p_value"] is None
    assert "方差" in flat["reason"]


def test_format_p_never_collapses_to_zero():
    """极端显著（p ~ 1e-21）必须显示为「< 1e-12」，不能显示成 0。"""
    assert bt_sig.format_p(1e-21) == "< 1e-12"
    assert bt_sig.format_p(0.0) == "< 1e-12"
    assert bt_sig.format_p(0.0324) == "0.0324"
    assert bt_sig.format_p(None) == "—"
    assert bt_sig.format_p(float("nan")) == "—"


# ===========================================================================
# 二、分行业回测
# ===========================================================================
IND_OFFSET = {"电子": 0.004, "计算机": 0.0, "医药生物": -0.004}
GROUP_BASE = {"high": 0.032, "mid": 0.008, "low": -0.012}


def _seed_backtest_run(conn, run_id: int = 1, n_per_cell: int = 40) -> None:
    """写入一个含 3 个真实行业 + 2 个占位行业的回测批次。

    每个（行业 × 分组）格恰好 ``n_per_cell`` 条样本，收益为「组基准 +
    行业偏移 + 5 档确定性扰动」，因此均值与胜率都可手算核对。
    """
    bt_data.ensure_tables(conn)
    conn.execute(
        "INSERT INTO bt_runs(id, created_at, start_date, end_date, step, horizons, "
        "stock_count, obs_count, score_max, summary, status) "
        "VALUES (?, '2026-09-16T09:00:00', '2024-01-02', '2025-12-31', 20, '5,10,20', "
        "120, 1200, ?, '{}', 'success')",
        (run_id, config.BT_SCORE_MAX),
    )

    industries = list(IND_OFFSET) + list(config.INDUSTRY_PLACEHOLDERS)
    rows = []
    seq = 0
    for ind in industries:
        for grp, base in GROUP_BASE.items():
            for k in range(n_per_cell):
                seq += 1
                code = f"{600000 + seq:06d}"
                year = 2024 if k % 2 == 0 else 2025
                obs = f"{year}-{(k % 12) + 1:02d}-{(k % 27) + 1:02d}"
                center = base + IND_OFFSET.get(ind, 0.0)
                # 5 档扰动循环出现，均值恰为 0（n_per_cell 为 5 的倍数时）
                w = (k % 5 - 2) * 0.002
                rows.append((
                    run_id, obs, code, "样本", "深主板", ind,
                    70 if grp == "high" else (45 if grp == "mid" else 20),
                    grp, obs, 10.0,
                    center + w,          # ret_5
                    center + w * 1.5,    # ret_10
                    center + w * 2.0,    # ret_20
                ))
    conn.executemany(
        "INSERT OR REPLACE INTO bt_trades(run_id, obs_date, code, name, board, "
        "industry, score, group_name, entry_date, entry_price, ret_5, ret_10, ret_20) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


@pytest.fixture()
def trade_conn(tmp_path):
    c = db.connect(tmp_path / "v17_trades.db")
    _seed_backtest_run(c)
    yield c
    c.close()


def test_industry_universe_excludes_placeholders(trade_conn):
    """「其他 / 未分类」不是申万一级行业，必须排除在行业全集之外。"""
    universe = bt_industry.industry_universe(trade_conn)
    assert set(universe) == set(IND_OFFSET)
    for ph in config.INDUSTRY_PLACEHOLDERS:
        assert ph not in universe


def test_industry_aggregation_matches_manual(trade_conn):
    """聚合结果必须与手算的样本数 / 胜率 / 平均收益完全一致。"""
    rows = bt_industry.compute_rows(trade_conn, 1, HORIZONS)
    by_key = {(r["industry"], r["group"], r["horizon"]): r for r in rows}

    # 与造数逻辑同源的确定性扰动：40 条恰为 8 个完整周期，均值必为 0
    ws = [(k % 5 - 2) * 0.002 for k in range(40)]
    assert sum(ws) == pytest.approx(0.0)

    for ind, offset in IND_OFFSET.items():
        for grp, base in GROUP_BASE.items():
            center = base + offset
            r5 = by_key[(ind, grp, 5)]
            assert r5["samples"] == 40
            assert r5["avg_return"] == pytest.approx(center, abs=1e-9)

            # 上涨胜率只认收益 > 0：等于 0 的样本同样不计入
            expected_win = sum(1 for w in ws if center + w > 0) / len(ws)
            assert r5["win_rate"] == pytest.approx(expected_win)

    # 占位行业不得出现在任何聚合结果中
    assert not (set(r["industry"] for r in rows) & set(config.INDUSTRY_PLACEHOLDERS))


def test_industry_samples_sum_back_to_overall(trade_conn):
    """一致性自证：各行业样本合并回去必须等于整体口径（3 × 40）。"""
    rows = bt_industry.compute_rows(trade_conn, 1, HORIZONS)
    for h in HORIZONS:
        for grp in GROUP_BASE:
            total = sum(r["samples"] for r in rows
                        if r["horizon"] == h and r["group"] == grp)
            assert total == 3 * 40, f"h={h} group={grp} 样本数不一致"


def test_industry_max_drawdown_is_non_positive(trade_conn):
    rows = bt_industry.compute_rows(trade_conn, 1, HORIZONS)
    for r in rows:
        assert r["max_drawdown"] is not None
        assert r["max_drawdown"] <= 1e-9


def test_industry_save_and_load_is_idempotent(trade_conn):
    """重复落库不得产生重复行（按批次幂等覆盖）。"""
    rows = bt_industry.compute_rows(trade_conn, 1, HORIZONS)
    first = bt_industry.save_rows(trade_conn, 1, rows)
    second = bt_industry.save_rows(trade_conn, 1, rows)
    assert first == second == len(rows)

    loaded = bt_industry.load_rows(trade_conn, 1)
    assert len(loaded) == len(rows)
    keys = [(r["industry"], r["group"], r["horizon"]) for r in loaded]
    assert len(set(keys)) == len(keys)


def test_industry_summary_coverage_never_exceeds_universe(trade_conn):
    """覆盖率必须自洽：覆盖行业数 ≤ 行业全集大小。

    占位行业曾会被当作真实行业计入 covered_count，出现「覆盖 32 / 共 31」。
    """
    rows = bt_industry.compute_rows(trade_conn, 1, HORIZONS)
    bt_industry.save_rows(trade_conn, 1, rows)
    d = bt_industry.build_summary(trade_conn, 1, HORIZONS)

    assert d["available"] is True
    assert d["industry_count"] == len(IND_OFFSET) == 3
    assert d["covered_count"] == 3
    assert d["covered_count"] <= d["industry_count"]
    assert not (set(d["by_industry"]) & set(config.INDUSTRY_PLACEHOLDERS))


def test_industry_top_and_bottom_are_disjoint(trade_conn):
    """靠前与靠后行业不得重叠，否则同一行业会同时出现在两处。"""
    rows = bt_industry.compute_rows(trade_conn, 1, HORIZONS)
    bt_industry.save_rows(trade_conn, 1, rows)
    d = bt_industry.build_summary(trade_conn, 1, HORIZONS)

    top = [x["industry"] for x in d["top"]]
    bottom = [x["industry"] for x in d["bottom"]]
    assert top and bottom
    assert not (set(top) & set(bottom))
    # 排名按高匹配分组平均收益降序
    rets = [x["avg_return"] for x in d["ranking"]]
    assert rets == sorted(rets, reverse=True)
    assert d["ranking"][0]["industry"] == "电子"


def test_industry_concentration_shares_bounded(trade_conn):
    rows = bt_industry.compute_rows(trade_conn, 1, HORIZONS)
    bt_industry.save_rows(trade_conn, 1, rows)
    d = bt_industry.build_summary(trade_conn, 1, HORIZONS)
    c = d["concentration"]
    assert 0.0 <= c["top3_share"] <= 1.0
    assert c["industries_with_samples"] == len(d["ranking"])


# ===========================================================================
# 三、多周期共振（周线）
# ===========================================================================
def _daily_frame(n: int = 400, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=n)
    close = 10.0 * np.cumprod(1.0 + rng.normal(0, 0.02, n))
    high = close * (1.0 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1.0 - np.abs(rng.normal(0, 0.01, n)))
    return pd.DataFrame({
        "trade_date": [d.date().isoformat() for d in dates],
        "open": close * 0.999,
        "high": high,
        "low": low,
        "close": close,
        "volume": rng.integers(1_000_000, 3_000_000, n).astype(float),
    })


def test_weekly_bars_aggregation_rules():
    """周线合成：open 首日、close 末日、high/low 极值、volume 合计。"""
    df = pd.DataFrame({
        "trade_date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05",
                       "2024-01-08", "2024-01-09"],
        "open": [10.0, 10.5, 10.2, 10.8, 11.0, 11.2],
        "high": [10.6, 10.7, 10.9, 11.1, 11.3, 11.4],
        "low": [9.8, 10.0, 10.1, 10.5, 10.9, 11.0],
        "close": [10.5, 10.2, 10.8, 11.0, 11.2, 11.1],
        "volume": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0],
    })
    wk = bt_resonance.weekly_bars(df)
    assert len(wk) == 2

    w1 = wk.iloc[0]
    assert (int(w1["iso_year"]), int(w1["iso_week"])) == (2024, 1)
    assert w1["open"] == pytest.approx(10.0)
    assert w1["close"] == pytest.approx(11.0)
    assert w1["high"] == pytest.approx(11.1)
    assert w1["low"] == pytest.approx(9.8)
    assert w1["volume"] == pytest.approx(1000.0)
    assert w1["bars"] == 4

    w2 = wk.iloc[1]
    assert int(w2["bars"]) == 2
    assert w2["open"] == pytest.approx(11.0)
    assert w2["close"] == pytest.approx(11.1)


def test_weekly_bars_empty_input():
    assert bt_resonance.weekly_bars(pd.DataFrame()).empty


def test_weekly_indicators_use_only_past_data():
    """无未来函数：把日线截断到第 40 周末，前 40 根周线的共振标记不得改变。"""
    df = _daily_frame()
    wk_full = bt_resonance.weekly_bars(df)
    flags_full = bt_resonance.weekly_flags(wk_full)

    iso = pd.to_datetime(df["trade_date"]).dt.isocalendar()
    keys = [f"{y}-{w}" for y, w in zip(iso["year"], iso["week"])]
    unique = list(dict.fromkeys(keys))
    target = unique[39]
    cut = max(i for i, k in enumerate(keys) if k == target)

    wk_cut = bt_resonance.weekly_bars(df.iloc[: cut + 1])
    flags_cut = bt_resonance.weekly_flags(wk_cut)

    assert len(wk_cut) == 40
    assert np.array_equal(flags_cut, flags_full[:40])


def test_usable_from_requires_market_calendar():
    """周线可用日必须取自市场交易日历；日历缺失的周标记为不可用。

    若改用个股自身最后成交日，停牌股会把「尚未定型的当周周线」误判为
    已收盘，从而引入未来函数。
    """
    cal = {(2024, 1): "2024-01-05", (2024, 2): "2024-01-12"}
    wk = pd.DataFrame({"iso_year": [2024, 2024], "iso_week": [1, 2]})
    assert bt_resonance._usable_from(wk, cal) == ["2024-01-05", "2024-01-12"]

    wk_gap = pd.DataFrame({"iso_year": [2024, 2024], "iso_week": [1, 9]})
    assert bt_resonance._usable_from(wk_gap, cal) == ["2024-01-05", None]


def test_week_end_calendar_uses_index_last_trading_day(tmp_path):
    """日历取「市场当周最后一个交易日」，而非个股的最后成交日。"""
    conn = db.connect(tmp_path / "v17_cal.db")
    try:
        bt_data.ensure_tables(conn)
        # 市场因假期提前收市：2024-01-05 与 2024-01-12 为当周最后交易日
        conn.executemany(
            "INSERT OR REPLACE INTO bt_index(symbol, trade_date, open, high, low, close) "
            "VALUES (?,?,?,?,?,?)",
            [(config.BT_INDEX_SYMBOL, d, 3000.0, 3010.0, 2990.0, 3005.0)
             for d in ("2024-01-02", "2024-01-03", "2024-01-05",
                       "2024-01-08", "2024-01-12")],
        )
        conn.commit()
        cal = bt_resonance.week_end_calendar(conn)
        assert cal[(2024, 1)] == "2024-01-05"
        assert cal[(2024, 2)] == "2024-01-12"
    finally:
        conn.close()


def test_resonance_summary_reports_not_ready_without_flags(trade_conn):
    """未打标记时必须明确报告不可用，而不是给出一份空对比。"""
    d = bt_resonance.build_summary(trade_conn, 1, HORIZONS)
    assert d["available"] is False
    assert d["reason"]


# ===========================================================================
# 四、参数敏感性
# ===========================================================================
def test_score_params_defaults_match_config():
    sp = bt_engine.ScoreParams()
    assert (sp.macd_fast, sp.macd_slow, sp.macd_dea) == (
        config.MACD_FAST, config.MACD_SLOW, config.MACD_DEA_SPAN)
    assert sp.volume_surge_ratio == config.VOLUME_SURGE_RATIO
    assert sp.range_compact_ratio == config.RANGE_COMPACT_RATIO
    assert sp.overrides() == {}


def test_score_params_from_overrides_applies_only_given_fields():
    """只覆盖给定项，其余保持基准 —— 变体之间才能构成「单因子对照」。"""
    sp = bt_engine.ScoreParams.from_overrides(
        {"macd_fast": 2, "macd_slow": 4, "macd_dea": 2})
    assert (sp.macd_fast, sp.macd_slow, sp.macd_dea) == (2, 4, 2)
    assert sp.volume_surge_ratio == config.VOLUME_SURGE_RATIO
    assert sp.range_compact_ratio == config.RANGE_COMPACT_RATIO

    ov = sp.overrides()
    assert set(ov) == {"macd_fast", "macd_slow", "macd_dea"}
    assert "MACD(2,4,2)" in sp.label()


def test_sensitivity_groups_are_single_factor_variants():
    """每个变体只在一个「参数族」内扰动，其余参数保持基准。

    MACD 参数天然是三联体（快线 / 慢线 / DEA），因此不能按「单个键」判定，
    而应按参数族判定：变体不得同时改动 MACD 与量能这类互不相干的参数。
    """
    fields = set(bt_engine.ScoreParams.__dataclass_fields__)
    family = {
        "macd": {"macd_fast", "macd_slow", "macd_dea"},
        "volume": {"volume_surge_ratio", "volume_double_ratio"},
        "range": {"range_lookback_days", "range_compact_ratio"},
    }
    assert len(config.SENSITIVITY_GROUPS) == 3

    covered: set[str] = set()
    for grp in config.SENSITIVITY_GROUPS:
        assert grp["key"] in family
        assert grp["label"] and grp["base_label"]
        assert len(grp["variants"]) == 2

        fam = family[grp["key"]]
        for vkey, vlabel, overrides in grp["variants"]:
            assert vkey and vlabel
            assert isinstance(overrides, dict) and overrides
            assert set(overrides) <= fam, f"{vkey} 跨参数族改动了 {set(overrides) - fam}"
            assert set(overrides) <= fields
            # 变体取值必须真的不同于基准，否则对照没有意义
            sp = bt_engine.ScoreParams.from_overrides(overrides)
            assert sp.overrides() == overrides
            covered |= set(overrides)

    # 三组参数都覆盖到了，且没有遗漏 MACD 三要素
    assert covered == {"macd_fast", "macd_slow", "macd_dea",
                       "volume_surge_ratio", "range_compact_ratio"}


def test_sensitivity_direction_kept_detects_inversion():
    """方向判定：高匹配分组收益被扰动到低于低匹配分组时必须判为未保持。"""
    hs = [5, 10]
    good = {str(h): {"high": {"avg_return": 0.02}, "low": {"avg_return": 0.01}}
            for h in hs}
    bad = {str(h): {"high": {"avg_return": 0.005}, "low": {"avg_return": 0.01}}
           for h in hs}
    assert all(bt_sensitivity._direction_kept(good, hs).values())
    assert not any(bt_sensitivity._direction_kept(bad, hs).values())


def test_sensitivity_load_summary_missing_is_unavailable(tmp_path):
    conn = db.connect(tmp_path / "v17_sens.db")
    try:
        bt_data.ensure_tables(conn)
        d = bt_sensitivity.load_summary(conn, 999)
        assert d["available"] is False
    finally:
        conn.close()


# ===========================================================================
# 五、研究简报结构与前端目录一致性
# ===========================================================================
SECTION_TITLES = [
    "一、研究摘要", "二、研究方法与规则", "三、历史回测结论",
    "四、分行业回测", "五、多周期共振策略", "六、当日候选池分析",
    "七、研究结论与展望", "八、研究局限性与风险提示", "九、合规声明",
]


@pytest.fixture()
def empty_conn(tmp_path):
    c = db.connect(tmp_path / "v17_report.db")
    yield c
    c.close()


def test_report_has_nine_sections(empty_conn):
    """v1.7.0 简报为九节；四项新分析各自独立成节或并入方法章。"""
    from app import report as rp

    html = rp.render_html(rp.collect(empty_conn))
    assert html.count("<h2>") == len(SECTION_TITLES) == 9
    for title in SECTION_TITLES:
        assert title in html, f"缺少章节：{title}"


def test_report_limitations_no_longer_claim_missing_significance_test(empty_conn):
    """第四节已补做显著性检验，局限章节不得再声称「未做检验」。"""
    from app import report as rp

    data = rp.collect(empty_conn)
    blob = "".join(x["body"] for x in data["limitations"])
    assert "未做统计显著性检验" not in blob
    assert "显著性" in blob


def test_frontend_outline_keys_match_section_count():
    """前端目录键（中/英）必须与简报章节数一致，否则界面会露出键名。"""
    src = (ROOT / "app" / "static" / "i18n.js").read_text(encoding="utf-8")
    for i in range(1, 10):
        n = len(re.findall(rf"repOutline{i}\s*:", src))
        assert n == 2, f"repOutline{i} 在中/英语言包中出现 {n} 次，应为 2 次"

    app_js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"const REP_OUTLINE_KEYS = \[(.*?)\];", app_js, re.S)
    assert m, "未找到 REP_OUTLINE_KEYS 定义"
    assert len(re.findall(r"repOutline\d+", m.group(1))) == 9


def test_frontend_analysis_endpoints_referenced():
    """前端必须真正调用四个新接口，避免板块永远空白。"""
    app_js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    for path in ("/api/analysis/industry", "/api/analysis/significance",
                 "/api/analysis/resonance", "/api/analysis/sensitivity"):
        assert path in app_js, f"前端未引用 {path}"


def test_static_asset_version_bumped():
    """静态资源版本号必须与 app.js 声明的版本一致，否则浏览器会命中旧缓存。

    版本号以 app.js 头部注释为唯一事实来源，避免每次升级都要改测试。
    """
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"版本：v(\d+\.\d+\.\d+)", js)
    assert m, "app.js 头部未找到版本号声明"
    versions = set(re.findall(r"\?v=([\d.]+)", html))
    assert versions == {m.group(1)}, (
        f"静态资源版本号与 app.js 声明不一致：html={sorted(versions)} js={m.group(1)}"
    )


# ===========================================================================
# 六、分析接口（端到端）
# ===========================================================================
@pytest.fixture()
def api_client(monkeypatch, tmp_path):
    """把库指向临时文件并预置回测批次，返回 TestClient。"""
    from fastapi.testclient import TestClient

    from app.main import app

    db_path = tmp_path / "v17_api.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    conn = db.connect(db_path)
    _seed_backtest_run(conn)
    bt_industry.save_rows(
        conn, 1, bt_industry.compute_rows(conn, 1, HORIZONS))
    conn.close()
    return TestClient(app)


def test_analysis_endpoints_all_reachable(api_client):
    """四个新接口与状态接口都必须可用（未构建的分析降级为 available=false）。"""
    for path in ("/api/analysis/industry", "/api/analysis/significance",
                 "/api/analysis/resonance", "/api/analysis/sensitivity",
                 "/api/analysis/status"):
        resp = api_client.get(path)
        assert resp.status_code == 200, f"{path} → {resp.status_code}"


def test_analysis_industry_endpoint_reports_consistent_coverage(api_client):
    d = api_client.get("/api/analysis/industry").json()
    assert d["available"] is True
    assert d["run_id"] == 1
    assert d["industry_count"] == len(IND_OFFSET)
    assert d["covered_count"] <= d["industry_count"]
    assert d["horizons"] == list(HORIZONS)
    assert d["rank_horizon"] == config.IND_RANK_HORIZON


def test_analysis_significance_endpoint_returns_three_horizons(api_client):
    d = api_client.get("/api/analysis/significance").json()
    assert d["available"] is True
    assert [t["horizon"] for t in d["tests"]] == list(HORIZONS)

    # t 检验针对「全市场高/低匹配分组」，与行业归属无关，
    # 因此行业未知（占位行业）的样本同样计入。
    per_cell = len(IND_OFFSET) + len(config.INDUSTRY_PLACEHOLDERS)
    for t in d["tests"]:
        assert t["n1"] == t["n2"] == 40 * per_cell
        assert t["mean_diff"] > 0            # 高匹配分组收益更高
        assert t["p_value"] is not None
        assert t["p_text"] and t["p_text"] != "0"
    assert d["robustness"]["years"] == ["2024", "2025"]


def test_analysis_resonance_reports_not_ready_before_marking(api_client):
    d = api_client.get("/api/analysis/resonance").json()
    assert d["available"] is False
    assert d["reason"]


def test_analysis_status_reports_build_flags(api_client):
    d = api_client.get("/api/analysis/status").json()
    assert d["latest_run_id"] == 1
    assert d["industry_ready"] is True
    assert d["resonance_ready"] is False
    assert set(d["counts"]) == {"bt_industry", "bt_resonance", "bt_sensitivity"}


def test_analysis_industry_csv_export_is_excel_safe(api_client):
    """导出件必须带 UTF-8 BOM（Excel 不乱码）且自带合规说明。"""
    resp = api_client.get("/api/analysis/industry.csv")
    assert resp.status_code == 200
    assert resp.content.startswith("\ufeff".encode("utf-8"))

    body = resp.content.decode("utf-8-sig")
    assert "申万一级行业" in body
    assert config.DISCLAIMER in body
    # 占位行业不得出现在导出件中
    for ph in config.INDUSTRY_PLACEHOLDERS:
        assert ph not in body.split(config.DISCLAIMER)[0]


def test_analysis_industry_csv_404_when_not_built(monkeypatch, tmp_path):
    """未构建分行业结果时应明确 404，而不是导出一份空表。"""
    from fastapi.testclient import TestClient

    from app.main import app

    db_path = tmp_path / "v17_empty.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    conn = db.connect(db_path)
    _seed_backtest_run(conn)
    conn.close()

    resp = TestClient(app).get("/api/analysis/industry.csv")
    assert resp.status_code == 404


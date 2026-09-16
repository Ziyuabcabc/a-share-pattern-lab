# -*- coding: utf-8 -*-
"""历史回测模块测试。

覆盖三个层面：

1. **绩效统计口径**（`app.backtest.stats`）：胜率、平均收益、盈亏比、
   最大回撤、复利净值、超额收益，全部为纯函数，直接断言数值。
2. **未来函数防御**：滚动窗口指标在「只给到第 i 根K线」与「给到全序列」
   两种输入下，前 i 个取值必须完全一致；这是回测可信度的底线。
3. **交易撮合口径**：入场价必须是观察日次日的开盘价、出场价必须是
   第 T+n 日的收盘价；ST/*ST/退市整理与停牌个股必须被剔除。

所有用例均不访问网络：行情数据直接写入临时库。
"""

import math
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from app import config, db
from app.backtest import engine as bt_engine
from app.backtest import stats as bt_stats

PASS = 101.0   # 满足「上涨」阈值：收益 > 0
FAIL = 99.0    # 满足「下跌」阈值：收益 < 0


# ---------------------------------------------------------------------------
# 一、绩效统计口径
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "score,expected",
    [
        (100, "high"), (60, "high"),          # 高分组下限：≥60
        (59, "mid"), (45, "mid"), (30, "mid"),  # 中分组：30–59
        (29, "low"), (0, "low"),               # 低分组：<30
    ],
)
def test_group_boundaries(score, expected):
    """分组阈值必须与看板 v1.1 完全一致（60 / 30）。"""
    assert bt_engine._group_of(score) == expected
    assert config.HIGH_SCORE_THRESHOLD == 60
    assert config.MID_SCORE_THRESHOLD == 30


def test_win_rate_counts_only_positive():
    """上涨胜率的分子只认收益 > 0：0 收益与亏损样本一样不计入。"""
    assert bt_stats.win_rate([0.01, -0.02, 0.03, 0.0]) == 0.5
    assert bt_stats.win_rate([0.0, -0.01]) == 0.0
    assert bt_stats.win_rate([]) is None


def test_avg_return_ignores_nan_and_inf():
    arr = [0.10, float("nan"), -0.02, float("inf")]
    assert bt_stats.avg_return(arr) == pytest.approx(0.04)


def test_win_loss_ratio_is_avg_win_over_avg_loss():
    # 平均盈利 0.05，平均亏损 -0.025 → 盈亏比 2.0
    assert bt_stats.win_loss_ratio([0.04, 0.06, -0.02, -0.03]) == pytest.approx(2.0)


def test_win_loss_ratio_none_without_losses():
    """全为盈利样本时无亏损可除，返回 None（前端展示「—」）。"""
    assert bt_stats.win_loss_ratio([0.01, 0.02]) is None
    assert bt_stats.win_loss_ratio([-0.01, -0.02]) is None


def test_max_drawdown_from_peak():
    nav = [1.0, 1.2, 0.9, 1.1]     # 自 1.2 跌到 0.9 → -25%
    assert bt_stats.max_drawdown(nav) == pytest.approx(-0.25)


def test_max_drawdown_of_monotonic_rise_is_zero():
    assert bt_stats.max_drawdown([1.0, 1.1, 1.2]) == pytest.approx(0.0)


def test_build_nav_compounds_periodically():
    nav = bt_stats.build_nav([0.10, -0.10])
    assert nav[0] == pytest.approx(1.10)
    assert nav[-1] == pytest.approx(1.10 * 0.90)


def test_total_return_equals_last_nav_minus_one():
    nav = bt_stats.build_nav([0.05, 0.05])
    assert bt_stats.total_return(nav) == pytest.approx(nav[-1] - 1.0)


def test_summarize_group_excess_is_total_minus_benchmark():
    returns = [0.05, -0.02, 0.03, 0.01]
    nav = bt_stats.build_nav([0.02, 0.01])          # 组合累计 +3.02%
    bench = bt_stats.build_nav([0.01, -0.005])      # 基准累计 +0.495%
    out = bt_stats.summarize_group(returns, [0.02, 0.01], nav, bench)
    assert out["samples"] == 4
    assert out["excess_return"] == pytest.approx(out["total_return"] - out["benchmark_return"])
    assert out["excess_return"] == pytest.approx(nav[-1] - bench[-1])


def test_summarize_group_handles_empty_benchmark():
    out = bt_stats.summarize_group([0.01], [0.01], bt_stats.build_nav([0.01]), [])
    assert out["excess_return"] is None
    assert out["benchmark_return"] is None


def test_safe_float_rejects_non_finite():
    assert bt_stats.safe_float(None) is None
    assert bt_stats.safe_float(float("nan")) is None
    assert bt_stats.safe_float(1.23456789, 4) == pytest.approx(1.2346)


# ---------------------------------------------------------------------------
# 二、未来函数防御
# ---------------------------------------------------------------------------
def _series(days: int, base: float = 10.0) -> pd.DataFrame:
    """构造带波动的合成日线，保证 MACD/KDJ 有金叉死叉交替。"""
    dates = pd.bdate_range(end=pd.Timestamp("2026-09-15"), periods=days)
    rows = []
    for i, d in enumerate(dates):
        close = base * (1 + 0.004 * i) * (1 + 0.012 * math.sin(i / 3.0))
        rows.append({
            "trade_date": d.strftime("%Y-%m-%d"),
            "open": close * 0.99,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": 1_000_000 * (1 + (i % 7) * 0.2),
        })
    return pd.DataFrame(rows)


def test_indicators_do_not_use_future_bars():
    """滚动窗口指标只看过去：截断未来数据后，历史得分必须逐格一致。

    这是回测「无未来函数」的核心断言——若指标用了全序列末尾信息
    （例如对整段做归一化、用后向 ewm 等），截断后前段数值就会改变。
    """
    full = _series(200)
    cut = 120
    calendar = full["trade_date"].tolist()
    cal_pos = {d: i for i, d in enumerate(calendar)}
    n_cal = len(calendar)

    s_full = bt_engine._prepare_stock(full, "600001", None, cal_pos, n_cal)
    s_cut = bt_engine._prepare_stock(full.iloc[:cut].copy(), "600001", None, cal_pos, n_cal)

    assert s_full is not None and s_cut is not None
    # 前 cut 根的得分逐格比对
    assert np.array_equal(s_full["score"][:cut], s_cut["score"][:cut])
    # 与价格无关的结论再确认一次：只看前 cut 根的收盘价也完全一致
    assert np.allclose(s_full["close"][:cut], s_cut["close"])


def test_score_changes_when_past_changes():
    """反向验证：修改**过去**的数据会改变得分，说明断言本身有效。"""
    full = _series(200)
    cut = 120
    calendar = full["trade_date"].tolist()
    cal_pos = {d: i for i, d in enumerate(calendar)}
    n_cal = len(calendar)

    base = bt_engine._prepare_stock(full, "600001", None, cal_pos, n_cal)

    tweaked = full.copy()
    tweaked.loc[:cut - 1, "close"] = tweaked.loc[:cut - 1, "close"] * 0.5
    other = bt_engine._prepare_stock(tweaked, "600001", None, cal_pos, n_cal)

    assert not np.array_equal(base["score"][:cut], other["score"][:cut])


# ---------------------------------------------------------------------------
# 三、端到端：撮合口径与基础过滤
# ---------------------------------------------------------------------------
def _seed(conn, codes_names, days: int = 200, gap_at: dict | None = None):
    """向临时库写入合成行情、基准指数与股票基础信息。

    codes_names: [(code, name)]；gap_at: {code: 缺失的日期下标}（模拟停牌）。
    """
    from app.backtest import data as bt_data

    bt_data.ensure_tables(conn)
    gap_at = gap_at or {}
    dates = None
    for code, name in codes_names:
        df = _series(days, base=10.0 + int(code[-2:]) % 7)
        dates = df["trade_date"].tolist()
        drop = gap_at.get(code)
        if drop is not None:
            df = df[df["trade_date"] != dates[drop]]
        conn.executemany(
            "INSERT OR REPLACE INTO bt_klines "
            "(code, trade_date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
            [(code, r.trade_date, r.open, r.high, r.low, r.close, r.volume)
             for r in df.itertuples()],
        )
        conn.execute(
            "INSERT OR REPLACE INTO stocks(code, name, board, industry, listing_days, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (code, name, "沪深主板", "银行", 3000, datetime.now().isoformat(timespec="seconds")),
        )

    # 基准指数：与股票同一日历，温和上行
    idx = _series(days, base=3500.0)
    conn.executemany(
        "INSERT OR REPLACE INTO bt_index "
        "(symbol, trade_date, open, high, low, close) VALUES (?,?,?,?,?,?)",
        [(config.BT_INDEX_SYMBOL, r.trade_date, r.open, r.high, r.low, r.close)
         for r in idx.itertuples()],
    )
    # 上市天数缓存：让「上市满 365 天」对全部样本成立
    db.meta_set(conn, "listing_days_map",
                "{" + ",".join(f'"{c}": 3000' for c, _ in codes_names) + "}")
    conn.commit()
    return dates


@pytest.fixture()
def bt_conn(tmp_path):
    conn = db.connect(tmp_path / "bt.db")
    yield conn
    conn.close()


def test_entry_is_next_open_and_exit_is_h_close(bt_conn):
    """撮合价格：观察日 T 收盘后打分 → T+1 开盘入场 → T+h 收盘出场。"""
    codes = [("600001", "形态样本一"), ("300001", "形态样本二")]
    calendar = _seed(bt_conn, codes)

    summary = bt_engine.run_backtest(
        bt_conn, bt_engine.BacktestParams(step=20, horizons=(5, 10, 20)))

    trades = {
        (r["obs_date"], r["code"]): dict(r)
        for r in bt_conn.execute("SELECT * FROM bt_trades WHERE run_id=?", (summary["run_id"],))
    }
    assert trades, "回测未产生样本，用例前提不成立"

    pos = {d: i for i, d in enumerate(calendar)}
    for (obs_date, code), row in trades.items():
        i = pos[obs_date]
        # 入场日必须是观察日的下一个交易日
        assert row["entry_date"] == calendar[i + 1]
        # 入场价必须等于 T+1 的开盘价
        o = bt_conn.execute(
            "SELECT open, close FROM bt_klines WHERE code=? AND trade_date=?",
            (code, calendar[i + 1])).fetchone()
        assert row["entry_price"] == pytest.approx(float(o["open"]))
        # 5 日收益必须等于 close(T+5)/open(T+1) - 1
        c5 = bt_conn.execute(
            "SELECT close FROM bt_klines WHERE code=? AND trade_date=?",
            (code, calendar[i + 5])).fetchone()
        assert row["ret_5"] == pytest.approx(float(c5["close"]) / float(o["open"]) - 1.0)


def test_st_stocks_are_excluded(bt_conn):
    """ST/*ST/退市整理个股不得进入回测样本（与看板基础过滤一致）。"""
    codes = [
        ("600001", "形态样本一"),
        ("600002", "ST风险警示"),
        ("600003", "*ST退市整理"),
        ("600004", "正常样本二"),
    ]
    _seed(bt_conn, codes)

    summary = bt_engine.run_backtest(
        bt_conn, bt_engine.BacktestParams(step=20, horizons=(5, 10, 20)))

    assert summary["excluded_st"] == 2
    assert summary["stock_count"] == 2

    used = {r[0] for r in bt_conn.execute(
        "SELECT DISTINCT code FROM bt_trades WHERE run_id=?", (summary["run_id"],))}
    assert used == {"600001", "600004"}


def test_suspended_stock_has_no_sample_on_gap_day(bt_conn):
    """观察日停牌的个股在该截面不产生样本（停牌剔除）。"""
    codes = [("600001", "形态样本一"), ("600002", "停牌样本")]
    # 让 600002 在日历第 100 个交易日缺失（停牌）
    calendar = _seed(bt_conn, codes, gap_at={"600002": 100})

    summary = bt_engine.run_backtest(
        bt_conn, bt_engine.BacktestParams(step=20, horizons=(5, 10, 20)))
    gap_date = calendar[100]

    rows = list(bt_conn.execute(
        "SELECT code FROM bt_trades WHERE run_id=? AND obs_date=?",
        (summary["run_id"], gap_date)))
    assert all(r["code"] != "600002" for r in rows)
    # 同一天另一只正常股票仍有样本，说明该截面本身是有效的
    assert any(r["code"] == "600001" for r in rows)


def test_benchmark_and_groups_share_rebalance_points(bt_conn):
    """三组与基准必须在同一批调仓点上逐期对齐，超额收益才可比。"""
    codes = [("600001", "形态样本一"), ("600004", "形态样本二")]
    _seed(bt_conn, codes)
    summary = bt_engine.run_backtest(
        bt_conn, bt_engine.BacktestParams(step=20, horizons=(5, 10, 20)))

    for h in (5, 10, 20):
        bench_dates = [p["date"] for p in summary["benchmark_equity"][str(h)]]
        for g in ("high", "mid", "low"):
            group_dates = [p["date"] for p in summary["equity"][g][str(h)]]
            assert group_dates == bench_dates

    # 摘要里的数值统一保留 6 位小数，比对容差取 2e-6
    for s in summary["stats"]:
        assert s["excess_return"] is None or abs(
            s["excess_return"] - (s["total_return"] - s["benchmark_return"])) < 2e-6


def test_empty_data_raises_actionable_error(bt_conn):
    """无行情数据时必须抛出可操作的错误，而不是静默返回空结果。"""
    from app.backtest import data as bt_data

    bt_data.ensure_tables(bt_conn)
    with pytest.raises(RuntimeError, match="回测行情数据为空"):
        bt_engine.run_backtest(bt_conn, bt_engine.BacktestParams(step=20, horizons=(5,)))

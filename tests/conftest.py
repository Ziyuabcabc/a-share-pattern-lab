# -*- coding: utf-8 -*-
"""pytest 共享夹具：以合成数据替换数据源（无网络依赖）。"""

import math

import pandas as pd
import pytest

from app import config, data_source, scoring

KLINE_CACHE: dict[str, pd.DataFrame] = {}

UNIVERSE = [
    ("600100", "形态样本A", 2.0),   # 主板，全条件命中候选
    ("300100", "形态样本B", 1.5),   # 创业板
    ("688100", "形态样本C", 1.2),   # 科创板
    ("000100", "形态样本D", 1.0),   # 深主板
    ("600200", "ST问题股", 1.0),    # 应被名称剔除
    ("830100", "北交所股", 1.0),    # 应被板块剔除
    ("600300", "停牌股", 0.0),      # 应被停牌剔除
    ("600400", "次新股", 1.0),      # 应被上市天数剔除
]

LISTING_DAYS = {
    "600100": 3000, "300100": 2500, "688100": 1500,
    "000100": 5000, "600200": 4000, "830100": 2000,
    "600300": 3500,
    # 600400 故意缺失 → 走 K 线窗口估计降级路径
}

CHIP_VALUES = {"600100": 12.0, "300100": 25.0, "688100": None, "000100": None}


def make_kline(code: str, days: int = 160, base: float = 10.0) -> pd.DataFrame:
    """生成日K：前段上行，中段横盘，末 5 日温和放量上行。"""
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
    rows = []
    for i, d in enumerate(dates):
        if i < days - 45:
            close = base * (1 + 0.002 * i)  # 缓慢上行
        elif i < days - 5:
            close = base * (1 + 0.002 * (days - 45)) * (1 + 0.001 * math.sin(i))
        else:
            # 末 5 日温和上行（累计约 1.75%）：保证 MACD红柱>0 且 KDJ J<100 同时成立
            close = base * (1 + 0.002 * (days - 45)) * (1 + 0.0035 * (i - (days - 6)))
        volume = 1_000_000 * (1 if i < days - 1 else 2.0)  # 末日 2 倍量
        rows.append(
            {
                "trade_date": d.date(),
                "open": close * 0.995,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": volume,
                "amount": volume * close * 100,
                "turnover_rate": 1.5,
            }
        )
    return pd.DataFrame(rows)


def _fake_fetch_hist(code, start_date, end_date):
    return KLINE_CACHE[code].copy()


def _fake_fetch_spot():
    rows = []
    for code, name, amount_scale in UNIVERSE:
        k = KLINE_CACHE[code]
        last, prev = k.iloc[-1], k.iloc[-2]
        rows.append(
            {
                "code": code, "name": name,
                "close": float(last["close"]), "prev_close": float(prev["close"]),
                "open": float(last["open"]), "high": float(last["high"]),
                "low": float(last["low"]),
                "volume": float(last["volume"]),
                "amount": float(last["amount"]) * amount_scale,
                "turnover_rate": float(last["turnover_rate"]),
            }
        )
    return pd.DataFrame(rows)


def _fake_fetch_listing_dates():
    return dict(LISTING_DAYS)


def _fake_fetch_chip_concentration(code):
    value = CHIP_VALUES.get(code)
    if value is None:
        return None
    return ("2026-09-02", value)


def _fake_fetch_industry_map():
    return {
        "600100": "电子元件", "300100": "半导体", "688100": "半导体",
        "000100": "白色家电", "600200": "医药制造",
        "830100": "通用设备", "600300": "食品饮料", "600400": "电子元件",
    }


def _fake_fetch_pe_map(codes, progress=None):
    """v1.1：腾讯 PE 行情 mock（行业内样本数 < 5 → 分位计 0 分，由引擎处理）。"""
    values = {
        "600100": 15.0, "300100": 40.0, "688100": 90.0,
        "000100": 30.0, "600200": 25.0, "600300": 20.0, "600400": 35.0,
    }
    return {c: values[c] for c in codes if c in values}


def _fake_fetch_overseas_indices():
    return []


def _fake_fetch_index_snapshot():
    return [
        {"code": "000001", "name": "上证指数", "close": 3500.0, "change_pct": 0.5, "amount": 6e11},
        {"code": "399006", "name": "创业板指", "close": 2100.0, "change_pct": -0.3, "amount": 2e11},
    ]


@pytest.fixture()
def patched_source(monkeypatch, tmp_path):
    """替换数据源函数与数据库路径，返回临时目录。"""
    for code, _, _ in UNIVERSE:
        KLINE_CACHE[code] = make_kline(code, base=10.0 + int(code[-2:]))
    monkeypatch.setattr(data_source, "fetch_hist", _fake_fetch_hist)
    monkeypatch.setattr(data_source, "fetch_spot", _fake_fetch_spot)
    monkeypatch.setattr(data_source, "fetch_listing_dates", _fake_fetch_listing_dates)
    monkeypatch.setattr(data_source, "fetch_industry_map", _fake_fetch_industry_map)
    monkeypatch.setattr(data_source, "fetch_chip_concentration", _fake_fetch_chip_concentration)
    monkeypatch.setattr(data_source, "fetch_index_snapshot", _fake_fetch_index_snapshot)
    monkeypatch.setattr(data_source, "fetch_pe_map", _fake_fetch_pe_map)
    monkeypatch.setattr(data_source, "fetch_overseas_indices", _fake_fetch_overseas_indices)
    # 热点行业名单与真实配置文件隔离，保证测试确定性（半导体 → 热点命中）
    monkeypatch.setattr(scoring, "_hot_industries_cache", ["半导体"])
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    yield tmp_path

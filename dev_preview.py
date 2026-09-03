#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开发预览脚本：以合成数据填充临时数据库并启动本地看板（仅供开发预览）。

用法：
    python dev_preview.py            # http://127.0.0.1:8765
    python dev_preview.py --port 9000

数据为随机生成的演示数据，写入独立的临时数据库，不影响真实扫描数据。
"""

import argparse
import os
import random
import sys
import tempfile

# 必须在导入 app.config 之前设置数据库隔离（真实路径在 main() 中完成导入）
_preview_db = os.path.join(tempfile.gettempdir(), "pattern_preview.db")
os.environ["PATTERN_LAB_DB"] = _preview_db

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build_fake_source(seed: int = 42):
    """构造合成数据源（确定性随机，保证演示效果稳定）。

    K 线生成约 300 个交易日（自然日跨度 >= 400 天），与真实拉取窗口一致，
    保证「上市天数降级估计」路径的表现与真实数据相同。
    """
    from app import data_source, scanner  # 延迟导入

    rng = random.Random(seed)

    def make_kline(code: str, days: int = 300, base: float = 10.0):
        import pandas as pd

        dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
        rows = []
        drift = rng.uniform(0.0008, 0.003)
        for i, d in enumerate(dates):
            if i < days - 45:
                close = base * (1 + drift * i)
            elif i < days - 5:
                close = base * (1 + drift * (days - 45)) * (1 + 0.001 * (rng.random() - 0.5))
            else:
                step = rng.uniform(0.0025, 0.004)
                close = base * (1 + drift * (days - 45)) * (1 + step * (i - (days - 6)))
            volume = 1_000_000 * (1 if i < days - 1 else rng.uniform(1.5, 2.5))
            rows.append(
                {
                    "trade_date": d.date(),
                    "open": close * 0.995,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": volume,
                    "amount": volume * close * 100,
                    "turnover_rate": rng.uniform(0.8, 6.0),
                }
            )
        return pd.DataFrame(rows)

    universe = []
    for i in range(24):
        board = ["600", "000", "300", "688"][i % 4]
        code = f"{board}{100 + i:03d}"
        universe.append((code, f"演示样本{i + 1:02d}", rng.uniform(0.8, 2.5)))
    # 混入应被过滤的样本
    universe += [
        ("600900", "ST剔除演示", 1.0),
        ("830900", "北交所剔除演示", 1.0),
        ("600901", "停牌剔除演示", 0.0),
    ]

    kline_cache = {code: make_kline(code, base=rng.uniform(8, 40)) for code, _, _ in universe}

    def fake_fetch_hist(code, start_date, end_date):
        return kline_cache[code].copy()

    def fake_fetch_spot():
        rows = []
        for code, name, scale in universe:
            k = kline_cache[code]
            last, prev = k.iloc[-1], k.iloc[-2]
            rows.append(
                {
                    "code": code, "name": name,
                    "close": float(last["close"]), "prev_close": float(prev["close"]),
                    "open": float(last["open"]), "high": float(last["high"]),
                    "low": float(last["low"]),
                    "volume": float(last["volume"]),
                    "amount": float(last["amount"]) * scale,
                    "turnover_rate": float(last["turnover_rate"]),
                }
            )
        import pandas as pd

        return pd.DataFrame(rows)

    industries = ["半导体", "电子元件", "软件开发", "光伏设备", "白酒", "医疗器械", "汽车零部件"]
    industry_map = {code: rng.choice(industries) for code, _, _ in universe}
    listing = {code: rng.choice([800, 1500, 3000, 5000]) for code, _, _ in universe}
    chips = {
        code: (rng.choice([None, None]) or None)
        for code, _, _ in universe
    }

    def fake_fetch_chip_concentration(code):
        value = chips.get(code)
        if value is None and rng.random() < 0.6:
            return ("2026-09-02", round(rng.uniform(6.0, 28.0), 2))
        return None

    def fake_fetch_listing_dates():
        return dict(listing)

    def fake_fetch_industry_map():
        return dict(industry_map)

    return fake_fetch_hist, fake_fetch_spot, fake_fetch_listing_dates, fake_fetch_industry_map, fake_fetch_chip_concentration


def main():
    parser = argparse.ArgumentParser(description="开发预览：合成数据 + 本地看板")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    # app 模块在此处导入（数据库路径已提前设置完毕）
    from app import data_source, scanner
    from app.main import app

    (f_hist, f_spot, f_listing, f_industry, f_chips) = build_fake_source()
    data_source.fetch_hist = f_hist
    data_source.fetch_spot = f_spot
    data_source.fetch_listing_dates = f_listing
    data_source.fetch_industry_map = f_industry
    data_source.fetch_chip_concentration = f_chips
    print("预览数据填充中（合成数据，仅用于开发预览）…")
    scanner.run_scan(with_chips=True)
    print(f"预览服务启动: http://127.0.0.1:{args.port}  (Ctrl+C 退出)")

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

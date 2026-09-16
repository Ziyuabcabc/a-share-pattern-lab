# -*- coding: utf-8 -*-
"""历史回测模块（v1.6.0）。

本模块仅基于本地历史行情做统计回测，输出为历史分组表现的统计结果，
不代表未来表现，不构成任何形式的操作指引。

子模块：
- data   回测数据层（bt_klines / bt_index 两张独立表）
- engine 向量化回测引擎（严格规避未来函数）
- stats  绩效统计口径

合规说明：模块内所有对外文案统一使用「分组、样本、绩效、统计」等研究表述。
"""

from app.backtest import data, engine, stats  # noqa: F401

__all__ = ["data", "engine", "stats"]

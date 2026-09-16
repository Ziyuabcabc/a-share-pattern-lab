# -*- coding: utf-8 -*-
"""参数敏感性分析（v1.7.0）：结论是否依赖特定参数取值。

研究动机
--------
``app/config.py`` 里的形态判定参数（MACD 周期、量能放大倍数、横盘振幅阈值）
由先验设定，未做寻优。一个自然的质疑是：**换一组参数，结论还成立吗？**
若结论只在某一组特定参数下成立，那么它更可能是参数过拟合的产物。

做法
----
对 3 组核心参数各取 2 个扰动档（只改这一个参数，其余保持基准），
每档跑一次**全量回测**，与基准回测的分档绩效逐项对比：

- **方向是否保持**：高匹配分组的平均收益率是否仍高于低匹配分组；
- **幅度是否稳定**：高匹配分组的平均收益率相对基准变动了多少个百分点。

变体回测一律以 ``persist=False`` 运行（见 ``engine.run_backtest``），
不写入 ``bt_runs`` 等表——它们是对照样本，不是正式回测批次，
写入会污染看板与简报读取的「最新成功批次」。

结论口径
--------
``config.SENSITIVITY_STABLE_PP`` 给出「幅度稳定」的判定阈值（百分点）。
方向全部保持、且幅度变动均未超过该阈值时，判定为「对参数扰动不敏感」。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime

from app import config
from app.backtest import data as bt_data
from app.backtest import engine as bt_engine
from app.backtest import industry as bt_industry
from app.backtest import stats as bt_stats

logger = logging.getLogger(__name__)

_METRICS = ("win_rate", "avg_return", "max_drawdown")


def _group_stats(summary: dict, horizons) -> dict:
    """从回测摘要中抽出 {horizon: {group: {win_rate, avg_return, max_drawdown, samples}}}。"""
    by_h = summary.get("stats_by_horizon") or {}
    out: dict[str, dict] = {}
    for h in horizons:
        rows = {r.get("group"): r for r in (by_h.get(str(h)) or [])}
        out[str(h)] = {
            g: {
                "samples": rows.get(g, {}).get("samples"),
                "win_rate": rows.get(g, {}).get("win_rate"),
                "avg_return": rows.get(g, {}).get("avg_return"),
                "max_drawdown": rows.get(g, {}).get("max_drawdown"),
            }
            for g in bt_engine.GROUP_ORDER
        }
    return out


def _delta(base: dict, new: dict, horizons) -> dict:
    """逐项差值（新 − 基准），缺失记为 None。"""
    out: dict[str, dict] = {}
    for h in horizons:
        out[str(h)] = {}
        for g in bt_engine.GROUP_ORDER:
            out[str(h)][g] = {}
            for m in _METRICS:
                a = (new.get(str(h), {}).get(g) or {}).get(m)
                b = (base.get(str(h), {}).get(g) or {}).get(m)
                out[str(h)][g][m] = (bt_stats.safe_float(a - b, 6)
                                     if (a is not None and b is not None) else None)
    return out


def _direction_kept(stats: dict, horizons) -> dict:
    """各周期上「高匹配分组平均收益率 > 低匹配分组」是否成立。"""
    out: dict[str, bool | None] = {}
    for h in horizons:
        hi = (stats.get(str(h), {}).get("high") or {}).get("avg_return")
        lo = (stats.get(str(h), {}).get("low") or {}).get("avg_return")
        out[str(h)] = bool(hi > lo) if (hi is not None and lo is not None) else None
    return out


def run_sensitivity(
    conn: sqlite3.Connection,
    base_run_id: int,
    horizons,
    step: int = config.BT_REBALANCE_STEP,
    start_date: str | None = None,
    end_date: str | None = None,
    progress=None,
) -> dict:
    """执行参数敏感性分析并返回结果（不落库，由调用方决定是否保存）。"""
    def say(msg: str) -> None:
        logger.info(msg)
        if progress:
            progress(msg)

    hs = bt_industry.supported_horizons(horizons)
    row = conn.execute("SELECT * FROM bt_runs WHERE id=?", (base_run_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"基准回测批次不存在: run_id={base_run_id}")
    base_summary = json.loads(row["summary"]) if row["summary"] else {}
    if not base_summary:
        raise RuntimeError("基准回测批次缺少摘要，无法作为对照")

    base_stats = _group_stats(base_summary, hs)
    # v1.7.0 之前落库的批次摘要中没有 score_params 字段，
    # 此时回退为当前 config 的基准参数描述（回测引擎默认值即 config 取值）。
    base_label = ((base_summary.get("score_params") or {}).get("label")
                  or bt_engine.ScoreParams().label())
    say(f"参数敏感性：基准 {base_label}，共 "
        f"{sum(len(g['variants']) for g in config.SENSITIVITY_GROUPS)} 个变体待跑")

    groups_out: list[dict] = []
    total = sum(len(g["variants"]) for g in config.SENSITIVITY_GROUPS)
    done = 0
    for grp in config.SENSITIVITY_GROUPS:
        variants_out: list[dict] = []
        for vkey, vlabel, overrides in grp["variants"]:
            done += 1
            sp = bt_engine.ScoreParams.from_overrides(overrides)
            say(f"  参数变体 {done}/{total}：{grp['label']} → {vlabel}")
            sub = bt_engine.run_backtest(
                conn,
                params=bt_engine.BacktestParams(
                    start_date=start_date, end_date=end_date, step=step,
                    horizons=tuple(hs), score=sp),
                persist=False,
            )
            stats = _group_stats(sub, hs)
            dl = _delta(base_stats, stats, hs)
            variants_out.append({
                "key": vkey,
                "label": vlabel,
                "overrides": overrides,
                "score_params_label": sp.label(),
                "stats": stats,
                "delta": dl,
                "direction_kept": _direction_kept(stats, hs),
                "obs_count": sub.get("obs_count"),
                "stock_count": sub.get("stock_count"),
            })

        # 该参数组的整体判定：所有变体在所有周期上方向是否保持；
        # 幅度以「高匹配分组平均收益率」的最大绝对变动（百分点）衡量。
        dir_flags = [v for var in variants_out for v in var["direction_kept"].values()
                     if v is not None]
        base_dir = _direction_kept(base_stats, hs)
        max_pp = 0.0
        for var in variants_out:
            for h in hs:
                d = var["delta"][str(h)]["high"]["avg_return"]
                if d is not None:
                    max_pp = max(max_pp, abs(d) * 100.0)
        groups_out.append({
            "key": grp["key"],
            "label": grp["label"],
            "base_label": grp["base_label"],
            "variants": variants_out,
            "direction_all_kept": bool(dir_flags) and all(dir_flags),
            "base_direction": base_dir,
            "max_high_return_delta_pp": round(max_pp, 3),
            "stable_magnitude": max_pp <= config.SENSITIVITY_STABLE_PP,
        })

    # 总体结论
    all_dir = all(g["direction_all_kept"] for g in groups_out) if groups_out else False
    worst = max(groups_out, key=lambda g: g["max_high_return_delta_pp"],
                default=None) if groups_out else None
    stable_all = bool(groups_out) and all(g["stable_magnitude"] for g in groups_out)
    return {
        "available": bool(groups_out),
        "base_run_id": base_run_id,
        "base_label": base_label,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "horizons": hs,
        "stable_threshold_pp": config.SENSITIVITY_STABLE_PP,
        "base_stats": base_stats,
        "groups": groups_out,
        "conclusion": {
            "direction_all_kept": all_dir,
            "magnitude_stable_all": stable_all,
            "worst_group": worst["label"] if worst else None,
            "worst_delta_pp": worst["max_high_return_delta_pp"] if worst else None,
            "variant_count": total,
        },
    }


# ---------------------------------------------------------------------------
# 落库 / 读取
# ---------------------------------------------------------------------------
def save_summary(conn: sqlite3.Connection, run_id: int, payload: dict) -> None:
    """把敏感性结果写入 bt_sensitivity（按批次幂等覆盖）。"""
    bt_data.ensure_tables(conn)
    conn.execute(
        "INSERT OR REPLACE INTO bt_sensitivity (run_id, generated_at, payload) "
        "VALUES (?,?,?)",
        (run_id, payload.get("generated_at"),
         json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()


def load_summary(conn: sqlite3.Connection, run_id: int) -> dict:
    """读取已落库的敏感性结果。"""
    bt_data.ensure_tables(conn)
    row = conn.execute(
        "SELECT payload FROM bt_sensitivity WHERE run_id=?", (run_id,)).fetchone()
    if row is None or not row["payload"]:
        return {"available": False, "run_id": run_id,
                "reason": "尚未执行参数敏感性分析"}
    try:
        payload = json.loads(row["payload"])
        payload["available"] = True
        return payload
    except (TypeError, ValueError):
        return {"available": False, "run_id": run_id,
                "reason": "敏感性结果解析失败"}

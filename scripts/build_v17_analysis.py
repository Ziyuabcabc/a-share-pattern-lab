# -*- coding: utf-8 -*-
"""构建 v1.7.0 的四项研究分析结果（全部基于已有回测批次，二次统计）。

四项分析：
1. **分行业回测**  —— 按申万一级行业拆分高 / 中 / 低三组的绩效（写入 bt_industry）
2. **周线共振**    —— 为每条样本打周线共振标记，并聚合双共振分组绩效
                      （写入 bt_resonance）
3. **统计显著性**  —— t 检验与分年度稳健性为即时计算，无需落库
4. **参数敏感性**  —— 跑 6 个参数变体的全量回测（不落库），结果写入 bt_sensitivity

用法：
    python scripts/build_v17_analysis.py                    # 全部构建（默认最新回测批次）
    python scripts/build_v17_analysis.py --run-id 2         # 指定回测批次
    python scripts/build_v17_analysis.py --only industry    # 只跑其中一项
    python scripts/build_v17_analysis.py --skip-sensitivity # 跳过最耗时的敏感性

注意：参数敏感性需要跑 6 次全量回测（每次约 30–60 秒），是耗时主体。
运行前请确保本地看板服务已停止，避免 SQLite 写锁竞争。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认编码可能不是 UTF-8，统一包装避免中文输出报错
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app import config, db  # noqa: E402
from app.backtest import engine as bt_engine  # noqa: E402
from app.backtest import industry as bt_industry  # noqa: E402
from app.backtest import resonance as bt_resonance  # noqa: E402
from app.backtest import sensitivity as bt_sensitivity  # noqa: E402
from app.backtest import significance as bt_significance  # noqa: E402

STEPS = ("industry", "resonance", "significance", "sensitivity")


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:+.2f}%"


def _resolve_run(conn, run_id):
    row = (conn.execute("SELECT * FROM bt_runs WHERE id=?", (run_id,)).fetchone()
           if run_id else bt_engine.latest_bt_run(conn))
    if row is None:
        raise SystemExit("没有可用的回测批次，请先运行 scripts/build_backtest.py")
    return row


def do_industry(conn, run_id, horizons) -> None:
    print("\n" + "=" * 78)
    print("【一、分行业回测】")
    print("=" * 78)
    rows = bt_industry.compute_rows(conn, run_id, horizons)
    n = bt_industry.save_rows(conn, run_id, rows)
    print(f"已落库 {n} 条（行业 × 分组 × 周期）")
    summ = bt_industry.build_summary(conn, run_id, horizons, rows)
    print(f"行业覆盖 {summ['covered_count']}/{summ['industry_count']} 个"
          f"（申万一级行业总数 {summ['expected_count']}）")
    conc = summ["concentration"]
    print(f"高匹配分组样本行业集中度：{conc['industries_with_samples']} 个行业有样本，"
          f"前三行业（{'、'.join(conc['top_industries'])}）合计占 {conc['top3_share']*100:.1f}%")
    print(f"\n参与排名的行业 {len(summ['ranking'])} 个（高匹配分组样本 ≥ "
          f"{summ['min_samples']}，按 {summ['rank_horizon']} 日平均收益率排序）")
    print(f"\n{'效果相对靠前':<14}{'样本':>7}{'胜率':>9}{'平均收益':>11}{'最大回撤':>11}")
    for x in summ["top"]:
        print(f"{x['industry']:<14}{x['samples']:>7}{x['win_rate']*100:>8.1f}%"
              f"{_pct(x['avg_return']):>11}{_pct(x['max_drawdown']):>11}")
    print(f"\n{'效果相对靠后':<14}{'样本':>7}{'胜率':>9}{'平均收益':>11}{'最大回撤':>11}")
    for x in summ["bottom"]:
        print(f"{x['industry']:<14}{x['samples']:>7}{x['win_rate']*100:>8.1f}%"
              f"{_pct(x['avg_return']):>11}{_pct(x['max_drawdown']):>11}")
    if summ["insufficient"]:
        print(f"\n未参与排名的行业（{len(summ['insufficient'])} 个，高匹配分组样本不足）："
              f"{'、'.join(summ['insufficient'])}")


def do_resonance(conn, run_id, horizons) -> None:
    print("\n" + "=" * 78)
    print("【二、多周期共振】")
    print("=" * 78)
    info = bt_resonance.mark_resonance(
        conn, run_id, progress=lambda m: print("  " + m, flush=True))
    print(f"标记 {info['rows']} 条样本，周线共振命中 {info['weekly_hit']} 条"
          f"（{info['weekly_hit_rate']*100:.1f}%）")
    summ = bt_resonance.build_summary(conn, run_id, horizons)
    if not summ.get("available"):
        print("不可用：", summ.get("reason"))
        return
    print(f"\n{'周期':>4}　{'双共振 n':>9}{'胜率':>9}{'平均收益':>11}{'回撤':>10}"
          f"　{'单日线 n':>9}{'胜率':>9}{'平均收益':>11}{'回撤':>10}")
    for r in summ["stats"]:
        a, b = r["resonance"], r["daily_high"]
        print(f"{r['horizon']:>4}　{a['samples']:>9}{a['win_rate']*100:>8.1f}%"
              f"{_pct(a['avg_return']):>11}{_pct(a['max_drawdown']):>10}"
              f"　{b['samples']:>9}{b['win_rate']*100:>8.1f}%"
              f"{_pct(b['avg_return']):>11}{_pct(b['max_drawdown']):>10}")
    print("\n差异（双共振 − 单日线）：")
    for r in summ["stats"]:
        d = r["diff"]
        print(f"  {r['horizon']:>2} 日　收益 {_pct(d['avg_return'])}　"
              f"胜率 {_pct(d['win_rate'])}　回撤 {_pct(d['max_drawdown'])}")


def do_significance(conn, run_id, horizons) -> None:
    print("\n" + "=" * 78)
    print("【三、统计显著性检验】")
    print("=" * 78)
    summ = bt_significance.build_summary(conn, run_id, horizons)
    print(f"检验：高匹配分组 vs 低匹配分组，双尾 Welch t 检验，"
          f"显著性水平 α = {summ['alpha']}")
    for r in summ["tests"]:
        print(f"\n  {r['horizon']} 日周期　n_high={r['n1']}　n_low={r['n2']}")
        print(f"    均值 {r['mean1']*100:+.4f}% vs {r['mean2']*100:+.4f}%　"
              f"差异 {r['mean_diff']*100:+.4f}%")
        print(f"    t = {r['t']:.4f}　df = {r['df']:.1f}　p {r['p_text']}　"
              f"显著 = {r['significant']}")
        print(f"    95% 置信区间 [{r['ci_low']*100:+.4f}%, {r['ci_high']*100:+.4f}%]")

    rob = summ["robustness"]
    print(f"\n分年度稳健性（样本下限 {rob['min_samples']}）：")
    for y in rob["years"]:
        for r in rob["by_year"][y]:
            mark = "方向一致" if r["consistent"] else "方向不一致"
            if not r["enough"]:
                mark = "样本不足"
            print(f"  {y} {r['horizon']:>2} 日　高 {_pct(r['ret_high'])}　"
                  f"低 {_pct(r['ret_low'])}　差 {_pct(r['ret_diff'])}　{mark}")
    print("\n分周期汇总：")
    for p in rob["per_horizon"]:
        print(f"  {p['horizon']:>2} 日　有效年份 {p['valid_years']}　"
              f"方向一致 {p['consistent_years']}　"
              f"一致率 {p['consistent_ratio']}")


def do_sensitivity(conn, run_id, horizons, step) -> None:
    print("\n" + "=" * 78)
    print("【四、参数敏感性分析】")
    print("=" * 78)
    t0 = time.time()
    payload = bt_sensitivity.run_sensitivity(
        conn, run_id, horizons, step=step,
        progress=lambda m: print("  " + m, flush=True))
    bt_sensitivity.save_summary(conn, run_id, payload)
    print(f"\n基准参数：{payload['base_label']}")
    for g in payload["groups"]:
        print(f"\n  ▸ {g['label']}（基准 {g['base_label']}）"
              f"　方向全保持={g['direction_all_kept']}"
              f"　最大收益变动={g['max_high_return_delta_pp']}pp"
              f"　幅度稳定={g['stable_magnitude']}")
        for v in g["variants"]:
            hi = v["stats"]["20"]["high"]
            print(f"      {v['label']:<12} 20日高匹配 {_pct(hi['avg_return'])}"
                  f"　胜率 {hi['win_rate']*100:.1f}%　"
                  f"变动 {_pct(v['delta']['20']['high']['avg_return'])}"
                  f"　方向 {'保持' if v['direction_kept']['20'] else '反转'}")
    c = payload["conclusion"]
    print(f"\n结论：方向全部保持 = {c['direction_all_kept']}；"
          f"幅度全部稳定 = {c['magnitude_stable_all']}；"
          f"变动最大的是「{c['worst_group']}」（{c['worst_delta_pp']}pp）")
    print(f"耗时 {time.time() - t0:.1f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 v1.7.0 研究分析结果")
    parser.add_argument("--run-id", type=int, default=None, help="回测批次 ID，默认最新")
    parser.add_argument("--only", choices=STEPS, default=None, help="只运行其中一项")
    parser.add_argument("--skip-sensitivity", action="store_true",
                        help="跳过参数敏感性（最耗时）")
    parser.add_argument("--skip-resonance", action="store_true",
                        help="跳过周线共振标记（全量打标约 1–2 分钟）")
    args = parser.parse_args()

    conn = db.connect(config.DB_PATH)
    try:
        row = _resolve_run(conn, args.run_id)
        run_id = int(row["id"])
        summary = json.loads(row["summary"]) if row["summary"] else {}
        horizons = summary.get("horizons") or list(config.BT_HORIZONS)
        step = summary.get("step") or row["step"] or config.BT_REBALANCE_STEP
        print(f"回测批次 #{run_id}　区间 {row['start_date']} ~ {row['end_date']}"
              f"　周期 {horizons}　调仓间隔 {step}")

        wanted = [args.only] if args.only else list(STEPS)
        if args.skip_sensitivity and "sensitivity" in wanted:
            wanted.remove("sensitivity")
        if args.skip_resonance and "resonance" in wanted:
            wanted.remove("resonance")

        t0 = time.time()
        if "industry" in wanted:
            do_industry(conn, run_id, horizons)
        if "resonance" in wanted:
            do_resonance(conn, run_id, horizons)
        if "significance" in wanted:
            do_significance(conn, run_id, horizons)
        if "sensitivity" in wanted:
            do_sensitivity(conn, run_id, horizons, step)

        print("\n" + "=" * 78)
        print(f"全部完成，总耗时 {time.time() - t0:.1f}s")
        print("=" * 78)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

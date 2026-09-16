# -*- coding: utf-8 -*-
"""研究简报生成（v1.6.0）。

把当日扫描结果与历史回测结果组装成一份标准化研究简报，
全部数据直接读自本地数据库，保证**结论可复现**、与看板实时同步。

结构（六节，固定顺序）：
1. 研究摘要
2. 研究方法与规则
3. 历史回测结论
4. 当日候选池分析
5. 研究局限性与风险提示
6. 合规声明

输出为**自包含 HTML**（图表以行内 SVG 绘制，不依赖任何外部资源与网络），
再由 scripts/make_pdf.py 用本机浏览器渲染为 PDF。

口径与合规：全部为历史公开数据的统计描述，不构成任何操作指引。
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime

from app import config, db
from app.backtest import data as bt_data
from app.backtest import engine as bt_engine

# ---------------------------------------------------------------------------
# 配色（与看板前端保持一致的低饱和研究风）
# ---------------------------------------------------------------------------
_C_HIGH = "#c4574e"     # 高匹配分组：柔和红
_C_MID = "#d99a3d"      # 中匹配分组：柔和琥珀
_C_LOW = "#8c93a3"      # 低匹配分组：中性灰蓝
_C_BENCH = "#6b7280"    # 基准：深灰
_C_ACCENT = "#3b6fd4"   # 强调蓝
_C_GRID = "#e6e9ef"
_C_TEXT = "#1c1f24"
_C_MUTED = "#6b7280"


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _pct(v, digits: int = 2, signed: bool = False) -> str:
    """小数转百分比文本；None 显示为「—」。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{f * 100:+.{digits}f}%" if signed else f"{f * 100:.{digits}f}%"


def _num(v, digits: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------------------
# 数据收集
# ---------------------------------------------------------------------------
def collect(conn: sqlite3.Connection, scan_run_id: int | None = None,
            bt_run_id: int | None = None) -> dict:
    """汇总简报所需的全部数据（只读，不改动业务数据）。

    回测相关表按需建表：全新克隆的仓库尚未执行过回测数据构建时，
    bt_* 表不存在，这里不能直接查询（否则简报接口会 500）。
    """
    bt_data.ensure_tables(conn)
    scan = _collect_scan(conn, scan_run_id)
    bt = _collect_backtest(conn, bt_run_id)
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "generated_date": datetime.now().strftime("%Y年%m月%d日"),
        "title": config.REPORT_TITLE,
        "subtitle": config.REPORT_SUBTITLE,
        "author": config.REPORT_AUTHOR,
        "scan": scan,
        "backtest": bt,
        "rules": _rule_rows(),
        "limitations": _limitations(scan, bt),
        "disclaimer": config.DISCLAIMER,
    }


def _collect_scan(conn: sqlite3.Connection, run_id: int | None) -> dict:
    """当日扫描批次与候选池统计。"""
    if run_id is None:
        row = db.latest_result_run(conn)
    else:
        row = conn.execute("SELECT * FROM scan_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return {"available": False, "reason": "尚无已完成的扫描批次"}

    rid = int(row["id"])
    total = int(conn.execute(
        "SELECT COUNT(*) AS c FROM scan_results WHERE run_id=?", (rid,)).fetchone()["c"])
    high = int(conn.execute(
        "SELECT COUNT(*) AS c FROM scan_results WHERE run_id=? AND total_score>=?",
        (rid, config.HIGH_SCORE_THRESHOLD)).fetchone()["c"])
    mid = int(conn.execute(
        "SELECT COUNT(*) AS c FROM scan_results WHERE run_id=? AND total_score>=? AND total_score<?",
        (rid, config.MID_SCORE_THRESHOLD, config.HIGH_SCORE_THRESHOLD)).fetchone()["c"])
    low = total - high - mid

    stat = conn.execute(
        "SELECT AVG(total_score) AS avg_score, MAX(total_score) AS max_score, "
        "AVG(pattern_score) AS avg_pattern, AVG(bonus_score) AS avg_bonus "
        "FROM scan_results WHERE run_id=?", (rid,)).fetchone()

    boards = [dict(r) for r in conn.execute(
        "SELECT board, COUNT(*) AS count FROM scan_results WHERE run_id=? "
        "GROUP BY board ORDER BY count DESC", (rid,))]

    industries = [dict(r) for r in conn.execute(
        "SELECT industry, COUNT(*) AS count, ROUND(AVG(total_score),1) AS avg_score "
        "FROM scan_results WHERE run_id=? GROUP BY industry ORDER BY count DESC LIMIT 12",
        (rid,))]

    top = [dict(r) for r in conn.execute(
        "SELECT code, name, industry, board, total_score, pattern_score, bonus_score "
        "FROM scan_results WHERE run_id=? ORDER BY total_score DESC, code LIMIT 20",
        (rid,))]

    scanned = row["total_scanned"] if "total_scanned" in row.keys() else None
    return {
        "available": True,
        "run_id": rid,
        "scan_time": row["finished_at"] or row["started_at"],
        "scanned": scanned,
        "candidates": total,
        "high": high,
        "mid": mid,
        "low": low,
        "avg_score": float(stat["avg_score"]) if stat and stat["avg_score"] is not None else None,
        "max_score": float(stat["max_score"]) if stat and stat["max_score"] is not None else None,
        "avg_pattern": float(stat["avg_pattern"]) if stat and stat["avg_pattern"] is not None else None,
        "avg_bonus": float(stat["avg_bonus"]) if stat and stat["avg_bonus"] is not None else None,
        "boards": boards,
        "industries": industries,
        "top": top,
    }


def _collect_backtest(conn: sqlite3.Connection, run_id: int | None) -> dict:
    """最新回测摘要（读落库 JSON，缺失时由明细表重建）。"""
    row = (conn.execute("SELECT * FROM bt_runs WHERE id=?", (run_id,)).fetchone()
           if run_id else bt_engine.latest_bt_run(conn))
    if row is None:
        return {"available": False, "reason": "尚未执行历史回测"}

    keys = row.keys()
    raw = row["summary"] if "summary" in keys else None
    if raw:
        try:
            payload = json.loads(raw)
            payload["available"] = True
            return payload
        except (TypeError, ValueError):
            pass

    stats = bt_engine.load_bt_stats(conn, int(row["id"]))
    horizons = [int(x) for x in str(row["horizons"] or "").split(",") if x.strip()]
    return {
        "available": True,
        "run_id": int(row["id"]),
        "generated_at": row["created_at"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "step": row["step"],
        "horizons": horizons,
        "stock_count": row["stock_count"],
        "obs_count": row["obs_count"],
        "score_max": row["score_max"],
        "groups": [{"key": g, "label": bt_engine.GROUP_LABELS[g]}
                   for g in bt_engine.GROUP_ORDER],
        "stats": stats,
        "stats_by_horizon": {
            str(h): [s for s in stats if int(s["horizon"]) == h] for h in horizons
        },
        "equity": {},
        "benchmark_equity": {},
        "method_notes": bt_engine._method_notes(),
    }


def _rule_rows() -> list[tuple[str, str, str]]:
    """打分规则表（模块 / 条件 / 分值），与 config 实时联动。"""
    return [
        ("核心形态匹配分", f"MACD({config.MACD_FAST},{config.MACD_SLOW},{config.MACD_DEA_SPAN}) "
                      f"金叉且红柱 &gt; 0", f"{config.SCORE_MACD_GOLD}"),
        ("核心形态匹配分", f"KDJ({config.KDJ_N},{config.KDJ_M1},{config.KDJ_M2}) "
                      f"金叉且 J &lt; 100", f"{config.SCORE_KDJ_GOLD}"),
        ("核心形态匹配分", f"当日成交量 &gt; {config.VOLUME_SURGE_RATIO} × 近 5 日均量",
         f"{config.SCORE_VOLUME_SURGE}"),
        ("核心形态匹配分", f"当日成交量 &gt; {config.VOLUME_DOUBLE_RATIO} × 近 5 日均量（叠加）",
         f"{config.SCORE_VOLUME_DOUBLE}"),
        ("核心形态匹配分", f"当日换手率处于 [{config.TURNOVER_HEALTHY_MIN:.0f}%, "
                      f"{config.TURNOVER_HEALTHY_MAX:.0f}%]",
         f"{config.SCORE_TURNOVER_HEALTHY}"),
        ("核心形态匹配分", f"近 {config.RANGE_LOOKBACK_DAYS} 个交易日最高价/最低价 "
                      f"≤ {config.RANGE_COMPACT_RATIO}", f"{config.SCORE_RANGE_COMPACT}"),
        ("筹码与基本面加分", f"筹码集中度 ≤ {config.CHIP_CONCENTRATION_MAX:.0f}%",
         f"{config.SCORE_CHIP_CONCENTRATED}"),
        ("筹码与基本面加分", f"筹码集中度 &gt; {config.CHIP_CONCENTRATION_LOOSE:.0f}%（叠加）",
         f"{config.SCORE_CHIP_LOOSE_EXTRA}"),
        ("筹码与基本面加分", "PE(TTM) 低于行业 30% 分位", f"{config.SCORE_PE_CHEAP}"),
        ("筹码与基本面加分", "PE(TTM) 处于行业 30%–70% 分位", f"{config.SCORE_PE_MID}"),
        ("筹码与基本面加分", f"近 5 日涨幅处于 [{config.RETURN5_MIN_PCT:.0f}%, "
                      f"{config.RETURN5_MAX_PCT:.0f}%]", f"{config.SCORE_RETURN5_HEALTHY}"),
        ("行业与板块加分", "创业板（30）/ 科创板（68）", f"{config.SCORE_GROWTH_BOARD}"),
        ("行业与板块加分", "所属行业在热点行业名单内", f"{config.SCORE_HOT_INDUSTRY}"),
    ]


def _limitations(scan: dict, bt: dict) -> list[dict]:
    """按实际数据情况生成局限性条目（不写空话）。"""
    out = [
        {"title": "历史统计不等于未来表现",
         "body": "简报中全部指标均为对历史公开数据的统计描述，描述的是「过去发生了什么」，"
                 "而非「未来会发生什么」。样本区间内的统计规律在区间外不保证延续。"},
        {"title": "回测口径的三项缺失",
         "body": f"筹码集中度（{config.BT_UNAVAILABLE_ITEMS[0][1]} 分）、"
                 f"PE 行业分位（{config.BT_UNAVAILABLE_ITEMS[1][1]} 分）、"
                 f"换手率（{config.BT_UNAVAILABLE_ITEMS[2][1]} 分）三项在历史时点"
                 f"缺乏免费且可复现的数据源，回测中统一计 0 分，因此回测可复现满分为 "
                 f"{config.BT_SCORE_MAX} 分而非 100 分。这会使回测分数与看板当日分数"
                 f"不完全可比：看板分数包含这三项，回测分数不含。"},
        {"title": "行业归属与 ST 状态未回溯",
         "body": "回测使用当前申万一级行业映射与最新股票名称判定 ST 状态，未回溯历史行业变更"
                 "与历史 ST 变更记录，存在轻微前视。因两者变动频率低，对整体结论影响有限。"},
        {"title": "样本自相关的控制与其代价",
         "body": f"调仓观察点间隔取 {config.BT_REBALANCE_STEP} 个交易日，使 "
                 f"{'/'.join(str(h) for h in config.BT_HORIZONS)} 日三个持仓周期的样本区间"
                 f"互不重叠，避免了重叠样本导致的显著性虚高；代价是可用调仓点数量减少，"
                 f"分组样本量下降。"},
        {"title": "成本与摩擦未计入",
         "body": "回测未扣除交易佣金、印花税、冲击成本与滑点，也未考虑涨跌停导致的无法成交。"
                 "实际可执行结果会低于理论统计值。"},
        {"title": "分组收益的统计口径",
         "body": "分组累计净值为各调仓周期内等权平均收益的复利累积。当持仓周期短于调仓间隔时"
                 "（如 5 日周期对应 20 日间隔），组合仅在该周期的窗口内持有，"
                 "其余时间为空仓，净值曲线的斜率不与满仓持有直接可比。"},
    ]
    if scan.get("available") and scan.get("candidates"):
        out.append({
            "title": "候选池仅覆盖当期截面",
            "body": f"当日候选池共 {scan['candidates']} 只，是基础过滤后的当期截面，"
                    f"不含已被过滤的个股，因此行业的绝对数量分布受过滤规则影响，"
                    f"不能直接外推为全市场行业结构。",
        })
    if bt.get("available"):
        pts = bt.get("rebalance_points")
        if pts:
            out.append({
                "title": "回测调仓点数量有限",
                "body": f"回测区间内有效调仓点仅 {pts} 个，"
                        f"在分组层面属于小样本，单期极端行情会对整体统计产生较大影响，"
                        f"分组间差异的统计显著性未做检验。",
            })
    return out


# ---------------------------------------------------------------------------
# 行内 SVG 图表（离线自包含）
# ---------------------------------------------------------------------------
def _svg_grouped_bar(categories: list[str], series: list[dict], width=660, height=250,
                     value_fmt="pct") -> str:
    """分组柱状图：categories 为组名，series 每项 {name, values, color}。"""
    pad_l, pad_r, pad_t, pad_b = 52, 12, 18, 46
    iw = width - pad_l - pad_r
    ih = height - pad_t - pad_b
    vals = [v for s in series for v in s["values"] if v is not None]
    vmax = max(vals) if vals else 1.0
    vmin = min(0.0, min(vals) if vals else 0.0)
    span = (vmax - vmin) or 1.0

    def y(v):
        return pad_t + ih - (v - vmin) / span * ih

    n = len(categories)
    gn = len(series)
    slot = iw / max(1, n)
    bar_w = min(38.0, slot / (gn + 0.9))
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
           f'style="max-width:{width}px" role="img">']

    # 网格与 Y 轴刻度
    ticks = 5
    # 刻度小数位随量程自适应，避免小量程下出现重复刻度标签
    nd = 0 if (value_fmt != "pct" or span * 100 >= 5) else 1
    for t in range(ticks + 1):
        v = vmin + span * t / ticks
        yy = y(v)
        out.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{width - pad_r}" y2="{yy:.1f}" '
                   f'stroke="{_C_GRID}" stroke-width="1"/>')
        label = f"{v * 100:.{nd}f}%" if value_fmt == "pct" else f"{v:.{nd}f}"
        out.append(f'<text x="{pad_l - 8}" y="{yy + 3.5:.1f}" text-anchor="end" '
                   f'font-size="10" fill="{_C_MUTED}">{label}</text>')
    # 零轴
    if vmin < 0 < vmax:
        out.append(f'<line x1="{pad_l}" y1="{y(0):.1f}" x2="{width - pad_r}" '
                   f'y2="{y(0):.1f}" stroke="#c9cfda" stroke-width="1"/>')

    for k, cat in enumerate(categories):
        cx = pad_l + slot * (k + 0.5)
        group_w = bar_w * gn + 4 * (gn - 1)
        x0 = cx - group_w / 2
        for si, s in enumerate(series):
            v = s["values"][k] if k < len(s["values"]) else None
            if v is None:
                continue
            bx = x0 + si * (bar_w + 4)
            top = min(y(v), y(0.0 if vmin < 0 else 0.0) if vmin < 0 else y(v))
            h = abs(y(v) - y(0.0 if vmin < 0 else 0.0)) if vmin < 0 else (pad_t + ih - y(v))
            out.append(f'<rect x="{bx:.1f}" y="{top:.1f}" width="{bar_w:.1f}" '
                       f'height="{h:.1f}" rx="3" fill="{s["color"]}" opacity="0.92"/>')
            txt = f"{v * 100:+.1f}%" if value_fmt == "pct" else f"{v:.2f}"
            ty = top - 5 if v >= 0 else top + h + 12
            out.append(f'<text x="{bx + bar_w / 2:.1f}" y="{ty:.1f}" text-anchor="middle" '
                       f'font-size="10" fill="{_C_TEXT}">{txt}</text>')
        out.append(f'<text x="{cx:.1f}" y="{pad_t + ih + 18:.1f}" text-anchor="middle" '
                   f'font-size="11" fill="{_C_TEXT}">{_esc(cat)}</text>')

    # 图例
    lx = pad_l
    for s in series:
        out.append(f'<rect x="{lx}" y="{height - 16}" width="9" height="9" rx="2" '
                   f'fill="{s["color"]}"/>')
        out.append(f'<text x="{lx + 13}" y="{height - 8}" font-size="10" '
                   f'fill="{_C_MUTED}">{_esc(s["name"])}</text>')
        lx += 20 + len(str(s["name"])) * 10
    out.append("</svg>")
    return "".join(out)


def _svg_multiline(x_labels: list[str], series: list[dict], width=660, height=270,
                   y_fmt="nav") -> str:
    """多线折线图：series 每项 {name, values, color}。"""
    pad_l, pad_r, pad_t, pad_b = 52, 14, 16, 52
    iw = width - pad_l - pad_r
    ih = height - pad_t - pad_b
    vals = [v for s in series for v in s["values"] if v is not None]
    if not vals:
        return f'<svg viewBox="0 0 {width} {height}" width="100%"></svg>'
    vmin, vmax = min(vals), max(vals)
    if vmax - vmin < 1e-9:
        vmax = vmin + 0.1
    span = vmax - vmin
    pad = span * 0.08
    vmin -= pad
    vmax += pad
    span = vmax - vmin
    n = len(x_labels)

    def x(i):
        return pad_l + (iw * i / max(1, n - 1))

    def y(v):
        return pad_t + ih - (v - vmin) / span * ih

    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
           f'style="max-width:{width}px" role="img">']
    for t in range(5 + 1):
        v = vmin + span * t / 5
        yy = y(v)
        out.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{width - pad_r}" y2="{yy:.1f}" '
                   f'stroke="{_C_GRID}" stroke-width="1"/>')
        lab = f"{v:.2f}" if y_fmt == "nav" else f"{v:.0f}"
        out.append(f'<text x="{pad_l - 8}" y="{yy + 3.5:.1f}" text-anchor="end" '
                   f'font-size="10" fill="{_C_MUTED}">{lab}</text>')

    for s in series:
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}"
                       for i, v in enumerate(s["values"]) if v is not None)
        if not pts:
            continue
        out.append(f'<polyline points="{pts}" fill="none" stroke="{s["color"]}" '
                   f'stroke-width="1.8" stroke-linejoin="round"/>')

    # X 轴：只标首、中、尾
    for idx in sorted({0, n // 2, n - 1}):
        if 0 <= idx < n:
            out.append(f'<text x="{x(idx):.1f}" y="{pad_t + ih + 16:.1f}" '
                       f'text-anchor="middle" font-size="10" fill="{_C_MUTED}">'
                       f'{_esc(x_labels[idx])}</text>')
    lx = pad_l
    for s in series:
        out.append(f'<line x1="{lx}" y1="{height - 16}" x2="{lx + 14}" y2="{height - 16}" '
                   f'stroke="{s["color"]}" stroke-width="2"/>')
        out.append(f'<text x="{lx + 18}" y="{height - 12}" font-size="10" '
                   f'fill="{_C_MUTED}">{_esc(s["name"])}</text>')
        lx += 34 + len(str(s["name"])) * 10
    out.append("</svg>")
    return "".join(out)


def _svg_hbar(items: list[dict], width=660, row_h=22, label_w=118) -> str:
    """横向条形图：items 每项 {label, value, note}。"""
    n = len(items)
    height = n * row_h + 16
    pad_r = 96
    iw = width - label_w - pad_r
    vmax = max((it["value"] for it in items), default=1) or 1
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
           f'style="max-width:{width}px" role="img">']
    for k, it in enumerate(items):
        cy = 8 + k * row_h
        bw = max(2.0, iw * it["value"] / vmax)
        out.append(f'<text x="{label_w - 8}" y="{cy + 12:.1f}" text-anchor="end" '
                   f'font-size="11" fill="{_C_TEXT}">{_esc(it["label"])}</text>')
        out.append(f'<rect x="{label_w}" y="{cy + 2:.1f}" width="{iw}" height="13" rx="6" '
                   f'fill="#f1f3f7"/>')
        color = it.get("color") or _C_ACCENT
        out.append(f'<rect x="{label_w}" y="{cy + 2:.1f}" width="{bw:.1f}" height="13" rx="6" '
                   f'fill="{color}" opacity="0.9"/>')
        note = it.get("note") or str(it["value"])
        out.append(f'<text x="{label_w + iw + 8}" y="{cy + 12:.1f}" font-size="10" '
                   f'fill="{_C_MUTED}">{_esc(note)}</text>')
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------
# HTML 渲染
# ---------------------------------------------------------------------------
_CSS = """
:root{--ink:#1c1f24;--muted:#6b7280;--line:#e3e7ee;--soft:#f6f7f9;--accent:#3b6fd4;
--high:#c4574e;--mid:#d99a3d;--low:#8c93a3;--bench:#6b7280}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);
font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
font-size:12pt;line-height:1.72;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.wrap{max-width:760px;margin:0 auto;padding:34px 30px 60px}
.cover{border-bottom:2px solid var(--ink);padding-bottom:18px;margin-bottom:26px}
.eyebrow{font-size:9.5pt;letter-spacing:.16em;color:var(--accent);font-weight:600;
margin-bottom:8px;text-transform:uppercase}
h1{font-size:20pt;font-weight:700;margin:0 0 6px;letter-spacing:-.01em;line-height:1.3}
.sub{font-size:11pt;color:var(--muted);margin:0 0 12px}
.meta{font-size:9.5pt;color:var(--muted)}
h2{font-size:13.5pt;font-weight:700;margin:30px 0 12px;padding-left:10px;
border-left:4px solid var(--accent);line-height:1.4}
h3{font-size:11.5pt;font-weight:600;margin:20px 0 8px;color:var(--ink)}
p{margin:0 0 10px;text-align:justify}
ul,ol{margin:0 0 12px;padding-left:20px}
li{margin-bottom:6px;text-align:justify}
table{width:100%;border-collapse:collapse;margin:12px 0 16px;font-size:10pt}
th{background:var(--soft);font-weight:600;text-align:left;padding:7px 9px;
border-bottom:1.5px solid var(--line);color:var(--ink);white-space:nowrap}
td{padding:6px 9px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:last-child td{border-bottom:1.5px solid var(--line)}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.up{color:var(--high)}.down{color:var(--low)}
.kpis{display:flex;gap:10px;margin:14px 0 18px;flex-wrap:wrap}
.kpi{flex:1 1 0;min-width:104px;border:1px solid var(--line);border-radius:10px;
padding:10px 12px;background:var(--soft)}
.kpi .k{font-size:9pt;color:var(--muted);margin-bottom:4px}
.kpi .v{font-size:16pt;font-weight:700;letter-spacing:-.02em;line-height:1.15}
.kpi .u{font-size:9pt;color:var(--muted);font-weight:400;margin-left:2px}
.chart{margin:14px 0 6px;border:1px solid var(--line);border-radius:10px;
padding:12px 14px 8px;background:#fff}
.cap{font-size:9pt;color:var(--muted);margin:0 0 14px;text-align:center}
.note{border-left:3px solid var(--mid);background:#fdf8ef;padding:10px 14px;
border-radius:0 8px 8px 0;margin:12px 0;font-size:10.5pt}
.note ol{margin:0;padding-left:18px}
.lim{border:1px solid var(--line);border-radius:10px;padding:12px 15px;margin-bottom:10px}
.lim .t{font-weight:600;font-size:11pt;margin-bottom:4px}
.lim .b{font-size:10pt;color:#3c424c;margin:0}
.rule-mod{font-weight:600;color:var(--accent)}
footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--line);
font-size:9pt;color:var(--muted);text-align:center}
@media print{
  @page{size:A4;margin:16mm 14mm 14mm}
  .wrap{max-width:none;padding:0}
  h2{page-break-after:avoid}
  table,.chart,.lim{page-break-inside:avoid}
  .pgbreak{page-break-before:always}
}
"""


def _section_summary(d: dict) -> str:
    scan, bt = d["scan"], d["backtest"]
    bullets = []
    if scan.get("available"):
        bullets.append(
            f"当日扫描覆盖沪深主板、创业板、科创板，经基础过滤（剔除 ST/*ST/退市整理、"
            f"剔除北交所、剔除上市不满 {config.FILTER_LISTING_MIN_DAYS} 天、剔除停牌）后"
            f"进入候选池的个股共 <b>{scan['candidates']}</b> 只，"
            f"其中高匹配分（≥{config.HIGH_SCORE_THRESHOLD}）"
            f"<b>{scan['high']}</b> 只、中匹配分"
            f"（{config.MID_SCORE_THRESHOLD}–{config.HIGH_SCORE_THRESHOLD - 1}）"
            f"<b>{scan['mid']}</b> 只、低匹配分"
            f"（&lt;{config.MID_SCORE_THRESHOLD}）<b>{scan['low']}</b> 只，"
            f"候选池平均形态匹配分 {_num(scan['avg_score'], 1)} 分"
            f"（满分 100），最高 {_num(scan['max_score'], 0)} 分。")
    if bt.get("available"):
        by_h = bt.get("stats_by_horizon") or {}
        parts = []
        for h in bt.get("horizons", []):
            rows = {r["group"]: r for r in (by_h.get(str(h)) or [])}
            if not all(g in rows for g in bt_engine.GROUP_ORDER):
                continue
            win = " / ".join(_pct(rows[g]["win_rate"], 1) for g in bt_engine.GROUP_ORDER)
            ret = " / ".join(_pct(rows[g]["avg_return"], 2, True)
                             for g in bt_engine.GROUP_ORDER)
            parts.append(f"{h} 日周期：上涨胜率 {win}，平均收益 {ret}")
        if parts:
            bullets.append(
                f"历史回测区间 {bt.get('start_date')} 至 {bt.get('end_date')}，"
                f"有效调仓点 {bt.get('rebalance_points')} 个，"
                f"共 {bt.get('obs_count'):,} 条个股–周期样本。"
                f"下表按「高 / 中 / 低」三组顺序完整列出三档表现，未做筛选："
                + "；".join(parts) + "。")
        bullets.append(
            f"回测可复现满分为 {bt.get('score_max')} 分：筹码集中度、PE 行业分位、换手率"
            f"三项在历史时点无免费可复现数据，统一计 0 分，故回测分数与看板当日分数"
            f"不完全可比。分组间差异未做统计显著性检验，不应据此推断分档具有区分能力。")
    if not bullets:
        bullets.append("当前尚无已完成的扫描批次或回测记录，简报内容不完整。")

    kpis = ""
    if scan.get("available"):
        kpis = (
            '<div class="kpis">'
            f'<div class="kpi"><div class="k">候选池个股数</div>'
            f'<div class="v">{scan["candidates"]}<span class="u">只</span></div></div>'
            f'<div class="kpi"><div class="k">高匹配分</div>'
            f'<div class="v up">{scan["high"]}<span class="u">只</span></div></div>'
            f'<div class="kpi"><div class="k">中匹配分</div>'
            f'<div class="v" style="color:var(--mid)">{scan["mid"]}<span class="u">只</span></div></div>'
            f'<div class="kpi"><div class="k">候选池均分</div>'
            f'<div class="v">{_num(scan["avg_score"], 1)}<span class="u">分</span></div></div>'
            '</div>')
    return kpis + "<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>"


def _section_method(d: dict) -> str:
    rows = "".join(
        f'<tr><td class="rule-mod">{_esc(m)}</td><td>{c}</td>'
        f'<td class="num">{p}</td></tr>'
        for m, c, p in d["rules"])
    bt = d["backtest"]
    notes = ""
    if bt.get("available"):
        notes = ('<div class="note"><b>回测方法学要点</b><ol>' +
                 "".join(f"<li>{_esc(x)}</li>" for x in bt.get("method_notes", [])) +
                 "</ol></div>")
    return f"""
<p>本研究的形态匹配分是一个<b>特征相似度指标</b>：把个股当前的量价形态与本地历史样本库中
「上升段启动」时点的形态特征逐项比对，命中的特征按预设权重累加，得到 0–100 的综合匹配分。
分数越高，表示该股当前形态与历史样本的特征重合度越高，<b>不代表任何未来结果</b>。</p>
<h3>打分规则（总分 100，三大模块）</h3>
<table><thead><tr><th>模块</th><th>条件</th><th class="num">分值</th></tr></thead>
<tbody>{rows}</tbody></table>
<h3>基础过滤规则</h3>
<ul>
<li>剔除 ST / *ST / 退市整理个股（按股票名称关键词判定）</li>
<li>剔除北交所个股，仅保留沪主板（60）、科创板（68）、深主板（00）、创业板（30）</li>
<li>剔除上市不满 {config.FILTER_LISTING_MIN_DAYS} 天的个股</li>
<li>剔除停牌或无成交个股</li>
</ul>
<h3>历史回测设计</h3>
<ul>
<li><b>回测区间</b>：近 {config.BT_CALENDAR_YEARS} 年，价格口径为前复权日线</li>
<li><b>分组</b>：按形态匹配分分为高（≥{config.HIGH_SCORE_THRESHOLD}）、
中（{config.MID_SCORE_THRESHOLD}–{config.HIGH_SCORE_THRESHOLD - 1}）、
低（&lt;{config.MID_SCORE_THRESHOLD}）三组</li>
<li><b>持仓周期</b>：{'、'.join(str(h) for h in config.BT_HORIZONS)} 个交易日</li>
<li><b>调仓频率</b>：每 {config.BT_REBALANCE_STEP} 个交易日形成一个观察截面，
使三个持仓周期的样本区间互不重叠</li>
<li><b>规避未来函数</b>：观察点判定仅使用观察日当天及之前的滚动窗口数据；
入场价取次一交易日开盘价，出场价取持有期满当日收盘价；持有期内停牌导致交易日
不连续的样本整体剔除，不做价格前推</li>
<li><b>基准</b>：{config.BT_INDEX_NAME}（{config.BT_INDEX_SYMBOL}）同期收益率</li>
</ul>
{notes}"""


def _section_backtest(d: dict) -> str:
    bt = d["backtest"]
    if not bt.get("available"):
        return f'<p class="cap">历史回测尚未执行（{_esc(bt.get("reason", ""))}）。</p>'

    horizons = bt.get("horizons", [])
    by_h = bt.get("stats_by_horizon") or {}
    gname = {"high": "高匹配分组", "mid": "中匹配分组", "low": "低匹配分组"}

    # 绩效统计表（三组 × 三个周期）
    body = []
    for h in horizons:
        rows = sorted(by_h.get(str(h)) or [],
                      key=lambda r: bt_engine.GROUP_ORDER.index(r["group"]))
        for k, r in enumerate(rows):
            hcell = (f'<td rowspan="{len(rows)}" class="num">{h}</td>' if k == 0 else "")
            body.append(
                f'<tr>{hcell}<td>{gname.get(r["group"], r["group"])}</td>'
                f'<td class="num">{r["samples"]:,}</td>'
                f'<td class="num">{_num(r.get("avg_score"), 1)}</td>'
                f'<td class="num">{_pct(r["win_rate"], 1)}</td>'
                f'<td class="num">{_pct(r["avg_return"], 2, True)}</td>'
                f'<td class="num">{_pct(r.get("excess_return"), 2, True)}</td>'
                f'<td class="num">{_pct(r.get("max_drawdown"), 2)}</td>'
                f'<td class="num">{_num(r.get("pl_ratio"), 2)}</td></tr>')
    table = ('<table><thead><tr><th>周期</th><th>分组</th><th class="num">样本数</th>'
             '<th class="num">均分</th><th class="num">上涨胜率</th>'
             '<th class="num">平均收益</th><th class="num">超额收益</th>'
             '<th class="num">最大回撤</th><th class="num">盈亏比</th></tr></thead>'
             f'<tbody>{"".join(body)}</tbody></table>')

    # 图 1：20 日周期分档平均收益
    focus = 20 if 20 in horizons else max(horizons) if horizons else None
    chart1 = chart2 = ""
    if focus is not None:
        rows = {r["group"]: r for r in (by_h.get(str(focus)) or [])}
        cats, vals = [], []
        for g in bt_engine.GROUP_ORDER:
            if g in rows:
                cats.append(gname[g].replace("匹配分组", ""))
                vals.append(rows[g]["avg_return"] or 0.0)
        if cats:
            chart1 = ('<div class="chart">' + _svg_grouped_bar(
                [f"{c}分组" for c in cats],
                [{"name": f"{focus} 日平均收益率", "values": vals, "color": _C_ACCENT}],
            ) + '</div><p class="cap">图 1　分档平均收益率（持有 '
                f'{focus} 个交易日，单笔样本算术平均）</p>')

    # 图 2：净值曲线（组合 vs 基准）
    eq = bt.get("equity") or {}
    bench = bt.get("benchmark_equity") or {}
    if focus is not None and eq:
        series = []
        for g in bt_engine.GROUP_ORDER:
            pts = (eq.get(g) or {}).get(str(focus)) or []
            if pts:
                series.append({"name": gname[g], "values": [p["nav"] for p in pts],
                               "color": {"high": _C_HIGH, "mid": _C_MID, "low": _C_LOW}[g]})
        bpts = bench.get(str(focus)) or []
        if bpts:
            series.append({"name": config.BT_INDEX_NAME,
                           "values": [p["nav"] for p in bpts], "color": _C_BENCH})
        if series:
            labels = [p["date"] for p in ((eq.get("high") or {}).get(str(focus)) or bpts)]
            chart2 = ('<div class="chart">' + _svg_multiline(labels, series) +
                      '</div><p class="cap">图 2　分档累计净值与基准对比'
                      f'（持有 {focus} 个交易日，按调仓周期复利，起点 = 1.00）</p>')

    # 图 3：候选池行业分布
    scan = d["scan"]
    chart3 = ""
    if scan.get("available") and scan.get("industries"):
        tops = scan["industries"][:12]
        items = [{"label": it["industry"], "value": it["count"],
                  "note": f"{it['count']} 只 · 均分 {it['avg_score']}",
                  "color": _C_ACCENT} for it in tops]
        chart3 = ('<div class="chart">' + _svg_hbar(items) +
                  '</div><p class="cap">图 3　候选池行业分布（前 12 个申万一级行业）</p>')

    notes = ('<div class="note"><b>结论表述约定</b><ol>'
             '<li>「上涨胜率」定义为：在给定持仓周期内，样本收益为正的历史占比。</li>'
             '<li>「超额收益」为分组区间累计收益率减去同期基准累计收益率，'
             '未做风险调整（未计算夏普比率、信息比率）。</li>'
             '<li>分组间差异未做统计显著性检验，小样本下不排除随机波动主导。</li>'
             '</ol></div>')

    # 由数据自动生成的观察（完整列出每个周期的领先组，不做选择性呈现）
    obs = []
    for h in horizons:
        rows = {r["group"]: r for r in (by_h.get(str(h)) or [])}
        if not all(g in rows for g in bt_engine.GROUP_ORDER):
            continue
        best_ret = max(bt_engine.GROUP_ORDER,
                       key=lambda g: rows[g]["avg_return"] if rows[g]["avg_return"] is not None
                       else float("-inf"))
        best_win = max(bt_engine.GROUP_ORDER,
                       key=lambda g: rows[g]["win_rate"] if rows[g]["win_rate"] is not None
                       else float("-inf"))
        obs.append(
            f"{h} 日周期中，平均收益率最高的是<b>{gname[best_ret]}</b>"
            f"（{_pct(rows[best_ret]['avg_return'], 2, True)}），"
            f"上涨胜率最高的是<b>{gname[best_win]}</b>"
            f"（{_pct(rows[best_win]['win_rate'], 1)}）。")
    mono = _monotonic_note(by_h, horizons)
    obs_text = ""
    if obs:
        obs_text = ('<h3>观察到的数据特征</h3><ul>'
                    + "".join(f"<li>{x}</li>" for x in obs)
                    + (f"<li>{mono}</li>" if mono else "")
                    + "</ul>")

    return (f'<p>本部分把上述打分规则放到历史数据上做分组检验：'
            f'在回测区间内按每 {bt.get("step")} 个交易日形成一个观察截面，'
            f'对截面内所有满足基础过滤的个股计算形态匹配分并归档，'
            f'再统计各组在 {"/".join(str(h) for h in horizons)} 个交易日三个持仓周期上的表现。'
            f'期间共产生 <b>{bt.get("trade_count") or bt.get("obs_count"):,}</b> 条个股–周期样本，'
            f'覆盖 <b>{bt.get("stock_count"):,}</b> 只个股。</p>'
            + table + obs_text + notes + chart1 + chart2 + chart3)


def _monotonic_note(by_h: dict, horizons: list) -> str:
    """检查「匹配分越高、表现越好」这一单调关系在数据中是否成立。"""
    ok, bad = [], []
    for h in horizons:
        rows = {r["group"]: r for r in (by_h.get(str(h)) or [])}
        if not all(g in rows for g in bt_engine.GROUP_ORDER):
            continue
        hi = rows["high"]["avg_return"]
        mid = rows["mid"]["avg_return"]
        lo = rows["low"]["avg_return"]
        if None in (hi, mid, lo):
            continue
        (ok if (hi >= mid >= lo) else bad).append(f"{h} 日")
    if not ok and not bad:
        return ""
    if bad and not ok:
        return (f"「匹配分越高、平均收益越高」的单调关系在 {' / '.join(bad)} 周期上"
                f"<b>并未成立</b>，说明该形态匹配分在这段样本中对未来收益的区分能力有限，"
                f"此为该研究的负面结果，如实列出。")
    if bad:
        return (f"单调关系在 {' / '.join(ok)} 周期上成立，"
                f"在 {' / '.join(bad)} 周期上不成立。")
    return f"「匹配分越高、平均收益越高」的单调关系在 {' / '.join(ok)} 周期上均成立。"


def _section_pool(d: dict) -> str:
    scan = d["scan"]
    if not scan.get("available"):
        return f'<p class="cap">当日候选池数据不可用（{_esc(scan.get("reason", ""))}）。</p>'

    boards = "".join(
        f'<tr><td>{_esc(b["board"])}</td><td class="num">{b["count"]:,}</td>'
        f'<td class="num">{b["count"] / max(1, scan["candidates"]) * 100:.1f}%</td></tr>'
        for b in scan["boards"])
    inds = "".join(
        f'<tr><td>{_esc(i["industry"])}</td><td class="num">{i["count"]}</td>'
        f'<td class="num">{i["avg_score"]}</td></tr>'
        for i in scan["industries"][:10])
    top = "".join(
        f'<tr><td>{_esc(r["code"])}</td><td>{_esc(r["name"])}</td>'
        f'<td>{_esc(r["industry"])}</td><td>{_esc(r["board"])}</td>'
        f'<td class="num">{r["total_score"]}</td>'
        f'<td class="num">{r["pattern_score"]}</td>'
        f'<td class="num">{r["bonus_score"]}</td></tr>'
        for r in scan["top"])

    score_dist = (""
                  f'<div class="kpis">'
                  f'<div class="kpi"><div class="k">候选池个股数</div>'
                  f'<div class="v">{scan["candidates"]:,}<span class="u">只</span></div></div>'
                  f'<div class="kpi"><div class="k">平均形态匹配分</div>'
                  f'<div class="v">{_num(scan["avg_score"], 1)}<span class="u">分</span></div></div>'
                  f'<div class="kpi"><div class="k">最高形态匹配分</div>'
                  f'<div class="v">{_num(scan["max_score"], 0)}<span class="u">分</span></div></div>'
                  f'<div class="kpi"><div class="k">平均核心形态分</div>'
                  f'<div class="v">{_num(scan["avg_pattern"], 1)}<span class="u">分</span></div></div>'
                  f'</div>')

    return f"""
<p>本节描述扫描批次 #{scan['run_id']}（扫描完成时间 {_esc(scan['scan_time'])}）的候选池结构。
全部为当期截面的统计描述。</p>
{score_dist}
<h3>板块分布</h3>
<table><thead><tr><th>板块</th><th class="num">候选数</th><th class="num">占比</th></tr></thead>
<tbody>{boards}</tbody></table>
<h3>行业分布（前 10 个申万一级行业）</h3>
<table><thead><tr><th>行业</th><th class="num">候选数</th><th class="num">平均分</th></tr></thead>
<tbody>{inds}</tbody></table>
<h3>形态匹配分居前 20 位的个股</h3>
<p class="cap">下表仅按匹配分排序展示，用于观察当前截面中形态特征重合度较高的样本，
<b>不构成任何操作指引</b>。</p>
<table><thead><tr><th>代码</th><th>名称</th><th>行业</th><th>板块</th>
<th class="num">总分</th><th class="num">核心形态分</th><th class="num">增强加分</th>
</tr></thead><tbody>{top}</tbody></table>"""


def _section_limits(d: dict) -> str:
    return "".join(
        f'<div class="lim"><div class="t">{_esc(x["title"])}</div>'
        f'<p class="b">{x["body"]}</p></div>' for x in d["limitations"])


def _section_compliance(d: dict) -> str:
    return f"""
<p>{_esc(d['disclaimer'])}</p>
<ul>
<li>本简报由本地程序依据<b>公开历史行情数据</b>自动生成，全部结论为对历史数据的统计描述，
不含任何对未来价格、涨跌幅或收益的预测。</li>
<li>简报中出现的个股名称与代码，均为形态特征匹配度统计的<b>样本对象</b>，
用于说明当前截面的统计分布，<b>不构成任何形式的操作指引</b>。</li>
<li>本项目为个人量化研究学习作品，不提供证券投资咨询服务，不收取任何费用，
不与任何券商或交易通道对接，不具备也不提供任何交易执行能力。</li>
<li>历史数据不等于未来表现。市场有风险，研究需谨慎。任何人依据本简报内容作出的
决策及其后果，均由其本人承担。</li>
<li>数据来源为公开免费行情接口，可能存在缺失、延迟或错误；免费接口的历史数据
不保证与交易所原始记录完全一致。</li>
</ul>
<p class="meta">生成时间：{_esc(d['generated_at'])}　|　
数据批次：扫描 #{d['scan'].get('run_id', '—')}　|　
回测 #{d['backtest'].get('run_id', '—')}　|　
本简报内容与看板数据实时同步，可通过重新生成完整复现。</p>"""


def render_html(d: dict) -> str:
    """渲染完整的自包含 HTML 简报。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{_esc(d['title'])}</title><style>{_CSS}</style></head>
<body><div class="wrap">
<div class="cover">
  <div class="eyebrow">QUANTITATIVE RESEARCH NOTE</div>
  <h1>{_esc(d['title'])}</h1>
  <p class="sub">{_esc(d['subtitle'])}</p>
  <div class="meta">{_esc(d['author'])}　|　生成日期：{_esc(d['generated_date'])}
  　|　数据口径：前复权日线</div>
</div>

<h2>一、研究摘要</h2>
{_section_summary(d)}

<h2>二、研究方法与规则</h2>
{_section_method(d)}

<h2 class="pgbreak">三、历史回测结论</h2>
{_section_backtest(d)}

<h2>四、当日候选池分析</h2>
{_section_pool(d)}

<h2>五、研究局限性与风险提示</h2>
{_section_limits(d)}

<h2>六、合规声明</h2>
{_section_compliance(d)}

<footer>{_esc(d['author'])}　|　本简报由程序自动生成，全部为历史数据统计结果，
不代表未来表现，不构成任何操作指引。</footer>
</div></body></html>"""

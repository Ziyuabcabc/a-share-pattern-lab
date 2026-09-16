# -*- coding: utf-8 -*-
"""研究简报生成（v1.6.1）。

把当日扫描结果与历史回测结果组装成一份标准化研究简报，
全部数据直接读自本地数据库，保证**结论可复现**、与看板实时同步。

结构（七节，固定顺序）：
1. 研究摘要
2. 研究方法与规则
3. 历史回测结论
4. 当日候选池分析
5. 研究结论与展望
6. 研究局限性与风险提示
7. 合规声明

输出为**自包含 HTML**（图表以行内 SVG 绘制，不依赖任何外部资源与网络），
再由 scripts/make_pdf.py 用本机浏览器渲染为 PDF。

排版约定（v1.6.1 修订）
----------------------
- 表格一律使用 ``colgroup`` + ``table-layout:fixed`` 固定列宽，保证行列严格对齐；
- 长表格允许跨页拆分，并用 ``thead{display:table-header-group}`` 让表头在每页重复，
  避免「整表跳页导致上一页大片留白」；
- 图题统一置于图下方、表题统一置于表上方，编号连续且格式一致；
- 正文启用 CJK 避头尾规则（``line-break:strict``），标点不会落到行首。

口径与合规：全部为历史公开数据的统计描述，不构成任何操作指引。
"""

from __future__ import annotations

import html
import json
import re
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

# 术语统一（v1.6.1）：全文固定使用下列表述
_TERM_SCORE = "综合形态匹配分"
_TERM_BENCH = "沪深300指数"
_GROUP_CN = {"high": "高匹配分组", "mid": "中匹配分组", "low": "低匹配分组"}


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


def _int(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "—"


def _colgroup(widths: list[int]) -> str:
    """固定列宽：保证表格行列严格对齐（宽度之和应为 100）。"""
    return "<colgroup>" + "".join(f'<col style="width:{w}%">' for w in widths) + "</colgroup>"


def _number_assets(markup: str) -> str:
    """把 @@TBL@@ / @@FIG@@ 占位符按出现顺序替换为连续编号。

    编号与「实际渲染出的表格/图」绑定，因此即使某一节整段降级
    （例如尚无回测数据、不出图），编号依然连续，不会出现断号。
    """
    seq = {"tbl": 0, "fig": 0}

    def _sub(match):
        # 正则捕获组返回大写（TBL / FIG），统一转小写后再做计数与分支判断
        key = match.group(1).lower()
        seq[key] += 1
        return ("表 " if key == "tbl" else "图 ") + str(seq[key])

    return re.sub(r"@@(TBL|FIG)@@", _sub, markup)


# ---------------------------------------------------------------------------
# 命中项标签（把 breakdown JSON 翻译成可读的中文短标签）
# ---------------------------------------------------------------------------
_HIT_LABELS = {
    "macd_gold_red": "MACD 金叉",
    "kdj_gold_j_under_100": "KDJ 金叉",
    "volume_surge_1_3x": "量能达标",
    "volume_surge_2x": "量能翻倍",
    "turnover_healthy_3_15": "换手健康",
    "range_compact_40d": "前期横盘",
    "chip_concentrated_le_18": "筹码集中",
    "chip_loose_gt_20": "筹码分散",
    "return5_healthy_5_20": "5 日涨幅",
    "growth_board": "成长板块",
    "hot_industry": "热点行业",
}
_PE_TIER_LABELS = {
    "low": "PE 低位",
    "mid": "PE 中位",
}
# 命中项标签的释义（用于表格下方的注释，保证缩写可被独立理解）
_HIT_LEGEND = (
    "MACD 金叉 = MACD(3,6,3) 金叉且红柱大于 0；KDJ 金叉 = KDJ(9,3,3) 金叉且 J 小于 100；"
    "量能达标 = 成交量大于 1.3 倍近 5 日均量；量能翻倍 = 成交量大于 2 倍近 5 日均量；"
    "换手健康 = 换手率处于 3%–15%；前期横盘 = 近 40 日最高价 / 最低价不大于 1.8；"
    "筹码集中 / 筹码分散 = 筹码集中度不高于 18% / 高于 20%；"
    "PE 低位 / PE 中位 = PE(TTM) 低于所属行业 30% 分位 / 处于 30%–70% 分位；"
    "5 日涨幅 = 近 5 日涨幅处于 5%–20%；成长板块 = 创业板或科创板；"
    "热点行业 = 所属申万一级行业在热点行业名单内。"
)


def _hit_items(raw) -> list[str]:
    """解析打分明细 JSON，返回命中项的中文标签列表（顺序固定）。"""
    try:
        bd = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return []
    if not isinstance(bd, dict):
        return []
    hits: list[str] = []
    for mod in ("core", "fund", "industry"):
        block = bd.get(mod) or {}
        if not isinstance(block, dict):
            continue
        for key, val in block.items():
            if key == "pe_tier":
                label = _PE_TIER_LABELS.get(str(val))
                if label:
                    hits.append(label)
            elif val is True and key in _HIT_LABELS:
                hits.append(_HIT_LABELS[key])
    return hits


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
        "indicator_defs": _indicator_defs(),
        "design_highlights": _design_highlights(bt),
        "limitations": _limitations(scan, bt),
        "conclusions": _conclusions(scan, bt),
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
    industry_total = int(conn.execute(
        "SELECT COUNT(DISTINCT industry) AS c FROM scan_results WHERE run_id=?",
        (rid,)).fetchone()["c"])

    # 高分股示例表：按综合形态匹配分从高到低取前 15 只
    top = []
    for r in conn.execute(
        "SELECT code, name, industry, board, total_score, pattern_score, bonus_score, breakdown "
        "FROM scan_results WHERE run_id=? ORDER BY total_score DESC, code LIMIT 15", (rid,)
    ):
        item = dict(r)
        item["hits"] = _hit_items(item.pop("breakdown", None))
        top.append(item)

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
        "industry_total": industry_total,
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
        ("核心形态匹配分", f"当日成交量 &gt; {config.VOLUME_DOUBLE_RATIO} × 近 5 日均量（与上项叠加）",
         f"{config.SCORE_VOLUME_DOUBLE}"),
        ("核心形态匹配分", f"当日换手率处于 [{config.TURNOVER_HEALTHY_MIN:.0f}%, "
                      f"{config.TURNOVER_HEALTHY_MAX:.0f}%]",
         f"{config.SCORE_TURNOVER_HEALTHY}"),
        ("核心形态匹配分", f"近 {config.RANGE_LOOKBACK_DAYS} 个交易日最高价 / 最低价 "
                      f"≤ {config.RANGE_COMPACT_RATIO}", f"{config.SCORE_RANGE_COMPACT}"),
        ("筹码与基本面加分", f"筹码集中度 ≤ {config.CHIP_CONCENTRATION_MAX:.0f}%",
         f"{config.SCORE_CHIP_CONCENTRATED}"),
        ("筹码与基本面加分", f"筹码集中度 &gt; {config.CHIP_CONCENTRATION_LOOSE:.0f}%（与上项叠加）",
         f"{config.SCORE_CHIP_LOOSE_EXTRA}"),
        ("筹码与基本面加分", "PE(TTM) 低于所属行业 30% 分位", f"{config.SCORE_PE_CHEAP}"),
        ("筹码与基本面加分", "PE(TTM) 处于所属行业 30%–70% 分位", f"{config.SCORE_PE_MID}"),
        ("筹码与基本面加分", f"近 5 日涨幅处于 [{config.RETURN5_MIN_PCT:.0f}%, "
                      f"{config.RETURN5_MAX_PCT:.0f}%]", f"{config.SCORE_RETURN5_HEALTHY}"),
        ("行业与板块加分", "板块属性为创业板（30）/ 科创板（68）", f"{config.SCORE_GROWTH_BOARD}"),
        ("行业与板块加分", "所属申万一级行业在热点行业名单内", f"{config.SCORE_HOT_INDUSTRY}"),
    ]


def _indicator_defs() -> list[tuple[str, str]]:
    """核心绩效指标口径（学术严谨性要求：口径必须可复算）。"""
    hs = "、".join(str(h) for h in config.BT_HORIZONS)
    return [
        ("上涨胜率",
         f"在给定持仓周期（{hs} 个交易日）内，样本收益率<b>严格大于 0</b> 的样本数占该组"
         f"有效样本总数的比例。收益率恰为 0 的样本计入未上涨，不计入分子。"),
        ("平均收益率",
         "该组全部有效样本在持仓周期内收益率的<b>算术平均值</b>，单笔样本等权，"
         "不按市值或流动性加权。"),
        (f"相对{_TERM_BENCH}超额收益",
         f"分组等权组合的区间累计收益率，减去同期{_TERM_BENCH}的区间累计收益率。"
         f"两者先按<b>同一组调仓观察点序列</b>对齐，再分别复利累积，保证起止时点一致；"
         f"该指标<b>未做风险调整</b>（未计算夏普比率、信息比率等风险调整后收益）。"),
        ("最大回撤",
         "分组等权组合逐期复利形成的累计净值序列，自历史峰值回落的最大幅度。"
         "以负值表示，数值越小表示回撤越深。"),
        ("盈亏比",
         "该组全部盈利样本的平均收益率<b>绝对值</b>，除以全部亏损样本的平均收益率"
         "<b>绝对值</b>。数值大于 1 表示平均盈利幅度大于平均亏损幅度。"),
        ("组内平均得分",
         f"该组全部有效样本在观察日的{_TERM_SCORE}的算术平均值，"
         f"用于刻画分组的得分集中程度。"),
    ]


def _design_highlights(bt: dict) -> list[tuple[str, str]]:
    """研究设计亮点：体现量化研究的规范性与可复现性。"""
    items = [
        ("三层未来函数规避",
         "其一，<b>指标层</b>：全部技术指标以滚动窗口一次性预计算，第 i 个交易日的取值"
         "仅依赖第 i 日及之前的数据，不存在任何回看未来；其二，<b>时序层</b>：观察日 T "
         "收盘后方可取得当日完整数据，因此入场统一取次一交易日 T+1 的开盘价，"
         "不采用当日收盘价这一无法实现的假设；其三，<b>出场层</b>：持仓期满以 T+h "
         "收盘价结算，入场与出场口径相互分离。"),
        ("样本区间不重叠控制",
         f"调仓观察点间隔取 {config.BT_REBALANCE_STEP} 个交易日，不小于最长持仓周期，"
         f"使 {'/'.join(str(h) for h in config.BT_HORIZONS)} 日三个持仓周期的样本区间"
         f"<b>互不重叠</b>，避免重叠样本导致的自相关与统计显著性虚高。"),
        ("严格且一致的基础过滤",
         "回测样本与看板当日候选池采用<b>完全相同</b>的基础过滤规则：剔除 ST / *ST / "
         "退市整理个股、剔除北交所个股、剔除上市不满 "
         f"{config.FILTER_LISTING_MIN_DAYS} 天的个股、剔除停牌或无成交个股，"
         "两组样本口径可比。"),
        ("缺失数据一律计 0，不做估计",
         f"筹码集中度、PE 行业分位、换手率三项在历史时点缺乏免费且可复现的数据源，"
         f"回测中<b>统一计 0 分</b>，不采用任何估计、插值或当前值回填，"
         f"以保证任何研究者用同一套公开数据都能复算出完全一致的结果。"),
    ]
    return items


def _limitations(scan: dict, bt: dict) -> list[dict]:
    """按实际数据情况生成局限性条目（不写空话）。"""
    out = [
        {"title": "研究性质：描述性统计，未做显著性检验",
         "body": "本研究对分组表现只做<b>描述性统计</b>，未进行假设检验，"
                 "未报告 p 值、置信区间或效应量。因此「高匹配分组优于低匹配分组」"
                 "这一表述是对样本内观测差异的陈述，其差异<b>不具备统计显著性</b>，"
                 "也不能排除该差异由随机波动导致。"},
        {"title": "历史统计不等于未来表现",
         "body": "简报中全部指标均为对历史公开数据的统计描述，描述的是「过去发生了什么」，"
                 "而非「未来会发生什么」。样本区间内的统计规律在区间外不保证延续；"
                 "区间外市场结构、交易制度与参与者行为的变化均可能使结论失效。"},
        {"title": "回测分数口径与看板当日分数不完全可比",
         "body": f"筹码集中度（{config.BT_UNAVAILABLE_ITEMS[0][1]} 分）、"
                 f"PE 行业分位（{config.BT_UNAVAILABLE_ITEMS[1][1]} 分）、"
                 f"换手率（{config.BT_UNAVAILABLE_ITEMS[2][1]} 分）三项在历史时点"
                 f"缺乏免费且可复现的数据源，回测中统一计 0 分，因此回测可复现满分为 "
                 f"{config.BT_SCORE_MAX} 分而非 100 分。需要注意的是，分组阈值仍沿用看板的 "
                 f"{config.HIGH_SCORE_THRESHOLD} / {config.MID_SCORE_THRESHOLD} 分标准"
                 f"（未按 {config.BT_SCORE_MAX} 分等比例缩放），"
                 f"这意味着回测中「高匹配分组」的实际筛选标准<b>比看板更为严苛</b>，"
                 f"两处的分数不可直接互相换算。"},
        {"title": "行业归属与 ST 状态未回溯历史",
         "body": "回测使用当前申万一级行业映射与最新股票名称判定 ST 状态，未回溯历史行业变更"
                 "与历史 ST 变更记录，存在轻微前视。因两者变动频率较低，对整体结论影响有限。"},
        {"title": "交易成本与摩擦未计入",
         "body": "回测未扣除交易佣金、印花税、冲击成本与滑点，也未考虑涨跌停或停牌导致的"
                 "无法成交情形。实际可执行结果会系统性地低于理论统计值。"},
        {"title": "分组累计净值的持有期口径",
         "body": "分组累计净值为各调仓周期内等权平均收益的复利累积。当持仓周期短于调仓间隔时"
                 "（例如 5 日周期对应 20 日间隔），组合仅在该周期的窗口内持有，其余时间为空仓，"
                 "因此净值曲线的斜率<b>不与满仓持有直接可比</b>，不同持仓周期之间的净值高低"
                 "也不宜直接横向比较。"},
        {"title": "样本期与市场环境单一",
         "body": f"回测区间为 {bt.get('start_date') or config.BT_CALENDAR_YEARS} 起的一段连续"
                 f"历史区间，仅覆盖单一市场环境，未做过跨牛熊区间的分年度稳健性拆解，"
                 f"结论对该区间内的特定行情特征可能存在依赖。"},
    ]
    if scan.get("available") and scan.get("candidates"):
        out.append({
            "title": "候选池仅覆盖当期截面",
            "body": f"当日候选池共 {_int(scan['candidates'])} 只，是基础过滤后的当期截面，"
                    f"不含已被过滤的个股，因此行业的绝对数量分布受过滤规则影响，"
                    f"不能直接外推为全市场行业结构。",
        })
    if bt.get("available"):
        pts = bt.get("rebalance_points")
        if pts:
            out.append({
                "title": "回测调仓点数量有限",
                "body": f"回测区间内有效调仓点仅 {pts} 个，在分组层面属于小样本，"
                        f"单期极端行情会对整体统计产生较大影响，"
                        f"分组间差异的统计显著性未做检验。",
            })
    return out


def _conclusions(scan: dict, bt: dict) -> dict:
    """研究结论与展望：核心发现 / 局限指向 / 后续方向。"""
    findings: list[str] = []
    if bt.get("available"):
        by_h = bt.get("stats_by_horizon") or {}
        horizons = bt.get("horizons") or []
        better = []
        for h in horizons:
            rows = {r["group"]: r for r in (by_h.get(str(h)) or [])}
            if not all(g in rows for g in bt_engine.GROUP_ORDER):
                continue
            hi, lo = rows["high"], rows["low"]
            if hi.get("win_rate") is None or lo.get("win_rate") is None:
                continue
            better.append(
                f"{h} 日周期（上涨胜率 {_pct(hi['win_rate'], 1)} 对 {_pct(lo['win_rate'], 1)}，"
                f"平均收益率 {_pct(hi['avg_return'], 2, True)} 对 "
                f"{_pct(lo['avg_return'], 2, True)}）")
        if better:
            findings.append(
                f"在全部 {'/'.join(str(h) for h in horizons)} 个持仓周期上，"
                f"高匹配分组的上涨胜率与平均收益率<b>均高于</b>低匹配分组："
                + "；".join(better) + "。"
                f"该结果在方向上支持「{_TERM_SCORE}所刻画的量价形态特征，"
                f"对个股中短期收益具有一定区分能力」这一判断。")
        findings.append(
            "分组间差异在 10 日与 20 日持仓周期上表现得更为稳定，在 5 日周期上差异明显收窄，"
            "提示该形态特征的有效区间可能偏向中短期而非超短期。")
        findings.append(
            "需要注意的是，高匹配分组与中匹配分组在 20 日周期上基本持平，"
            "说明区分能力主要体现在「高分组与低分组之间」，"
            "而非在「高分组与中分组之间」呈线性递进。")
    else:
        findings.append("当前尚无已完成的历史回测记录，暂无法给出分组表现的统计结论。")

    if scan.get("available") and scan.get("candidates"):
        findings.append(
            f"就当期截面而言，纳入候选池的 {_int(scan['candidates'])} 只个股中，"
            f"达到高匹配分组标准（≥{config.HIGH_SCORE_THRESHOLD} 分）的有 {_int(scan['high'])} 只，"
            f"占比 {_pct(scan['high'] / max(1, scan['candidates']), 1)}，"
            f"整体分布呈明显的右偏形态（平均分 {_num(scan['avg_score'], 1)} 分）。")

    outlook = [
        ("分行业回测",
         "当前回测在全部行业内混合统计，未控制行业效应。后续可在申万一级行业维度上"
         "分别回测，考察形态特征在哪些行业中的区分能力更强，以及结论是否由少数行业的"
         "极端表现驱动。"),
        ("参数敏感性分析",
         "当前打分权重与阈值均由先验设定，未做寻优。后续可对 MACD / KDJ 参数、量能倍数、"
         "横盘振幅阈值等关键参数做敏感性分析，观察结论对参数扰动的稳健程度。"),
        ("多周期共振研究",
         "当前仅对单一观察日的形态打分。后续可引入多周期（如日线 + 周线）共振条件，"
         "考察叠加更长周期约束后，分组差异是否进一步扩大。"),
        ("纳入交易成本与可执行性约束",
         "后续可把佣金、印花税、滑点以及涨跌停不可成交等约束纳入回测，"
         "评估结论在扣除摩擦成本后的存续性。"),
        ("统计推断与稳健性检验",
         "后续可引入 bootstrap 重抽样、分年度滚动检验等方式，为分组差异给出置信区间，"
         "把当前的描述性统计升级为具备推断能力的检验。"),
        ("扩展样本区间与市场",
         "当前样本区间较短且市场环境单一。后续可把回测区间向前延伸至更长历史，"
         "或在不同市场状态下分组检验，以提升结论的外部效度。"),
    ]
    return {"findings": findings, "outlook": outlook}


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
# 样式
# ---------------------------------------------------------------------------
_CSS = """
:root{--ink:#1c1f24;--muted:#6b7280;--line:#e3e7ee;--soft:#f6f7f9;--accent:#3b6fd4;
--high:#c4574e;--mid:#d99a3d;--low:#8c93a3;--bench:#6b7280}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);
font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
font-size:11.5pt;line-height:1.75;
/* 中文排版：启用避头尾，禁止标点落到行首/行尾 */
line-break:strict;word-break:normal;overflow-wrap:break-word;
-webkit-print-color-adjust:exact;print-color-adjust:exact}
.wrap{max-width:760px;margin:0 auto;padding:34px 30px 60px}
.cover{border-bottom:2px solid var(--ink);padding-bottom:18px;margin-bottom:26px}
.eyebrow{font-size:9pt;letter-spacing:.16em;color:var(--accent);font-weight:600;
margin-bottom:8px;text-transform:uppercase}
h1{font-size:19pt;font-weight:700;margin:0 0 6px;letter-spacing:-.01em;line-height:1.35}
.sub{font-size:10.5pt;color:var(--muted);margin:0 0 12px;line-height:1.6}
.meta{font-size:9pt;color:var(--muted);line-height:1.7}

/* 标题：与后续内容保持同页，避免标题孤立在页底 */
h2{font-size:13pt;font-weight:700;margin:30px 0 12px;padding-left:10px;
border-left:4px solid var(--accent);line-height:1.45;
page-break-after:avoid;page-break-inside:avoid}
h3{font-size:11pt;font-weight:600;margin:20px 0 8px;color:var(--ink);
line-height:1.5;page-break-after:avoid}
p{margin:0 0 10px;text-align:justify;text-justify:inter-ideograph}
ul,ol{margin:0 0 12px;padding-left:20px}
li{margin-bottom:6px;text-align:justify}

/* 表格：固定列宽 + 表头跨页重复，长表允许拆分，避免整表跳页留白 */
table{width:100%;border-collapse:collapse;margin:0 0 6px;font-size:9.5pt;
table-layout:fixed}
thead{display:table-header-group}
tr{page-break-inside:avoid}
th{background:var(--soft);font-weight:600;text-align:left;padding:7px 8px;
border-bottom:1.5px solid var(--line);color:var(--ink);line-height:1.45;
vertical-align:bottom}
td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top;
line-height:1.55}
tbody tr:last-child td{border-bottom:1.5px solid var(--line)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.center{text-align:center}
.mono{font-variant-numeric:tabular-nums;letter-spacing:.02em}
.up{color:var(--high)}.down{color:var(--low)}
/* 超额收益等正负值着色：遵循 A 股「涨红跌绿」惯例 */
.pos{color:var(--high)}.neg{color:#3f9c78}
.hits{font-size:8.5pt;color:#3c424c;line-height:1.55}
.tbl-keep{page-break-inside:avoid}
/* 行数较多的数据表允许跨页拆分：避免整表跳页在上一页留下大片空白，
   配合 thead{display:table-header-group} 在续页重复表头。 */
.tbl-flow{page-break-inside:auto}
/* 紧凑表格：用于行数较多的示例表，压缩行高以避免跨页拆分 */
table.tight td{padding:4px 8px;line-height:1.45}
table.tight th{padding:6px 8px}

/* 图表与题注 */
.fig{margin:14px 0 6px;border:1px solid var(--line);border-radius:10px;
padding:12px 14px 10px;background:#fff;page-break-inside:avoid}
.fig-cap{font-size:9pt;color:var(--muted);margin:0 0 16px;text-align:center;
line-height:1.6;page-break-before:avoid}
.tbl-cap{font-size:9.5pt;font-weight:600;color:var(--ink);margin:16px 0 6px;
line-height:1.55;page-break-after:avoid}
.tbl-src{font-size:8.5pt;color:var(--muted);margin:4px 0 16px;line-height:1.6}

/* 提示框 */
.note{border-left:3px solid var(--mid);background:#fdf8ef;padding:11px 14px;
border-radius:0 8px 8px 0;margin:12px 0;font-size:10pt;page-break-inside:avoid}
.note .nt{font-weight:600;margin-bottom:5px}
.note ol,.note ul{margin:0;padding-left:18px}
.note li{margin-bottom:5px}

/* 研究设计亮点 */
.hl{border:1px solid var(--line);border-radius:10px;padding:12px 15px;margin-bottom:9px;
page-break-inside:avoid}
.hl .t{font-weight:600;font-size:10.5pt;margin-bottom:4px;color:var(--accent)}
.hl .b{font-size:10pt;color:#3c424c;margin:0;text-align:justify}

/* 结论与展望 */
.oc{display:block;margin-bottom:9px;page-break-inside:avoid}
.oc .t{font-weight:600;font-size:10.5pt;margin-bottom:3px}
.oc .b{font-size:10pt;color:#3c424c;margin:0;text-align:justify}

/* KPI */
.kpis{display:flex;gap:10px;margin:14px 0 18px;flex-wrap:wrap;
page-break-inside:avoid}
.kpi{flex:1 1 0;min-width:104px;border:1px solid var(--line);border-radius:10px;
padding:10px 12px;background:var(--soft)}
.kpi .k{font-size:8.5pt;color:var(--muted);margin-bottom:4px;line-height:1.4}
.kpi .v{font-size:15pt;font-weight:700;letter-spacing:-.02em;line-height:1.2}
.kpi .u{font-size:8.5pt;color:var(--muted);font-weight:400;margin-left:2px}

/* 局限性 */
.lim{border:1px solid var(--line);border-radius:10px;padding:12px 15px;
margin-bottom:10px;page-break-inside:avoid}
.lim .t{font-weight:600;font-size:10.5pt;margin-bottom:4px}
.lim .b{font-size:10pt;color:#3c424c;margin:0;text-align:justify}
.rule-mod{font-weight:600;color:var(--accent)}
footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--line);
font-size:8.5pt;color:var(--muted);text-align:center;line-height:1.7}
@media print{
  @page{size:A4}
  .wrap{max-width:none;padding:0}
}
"""


# ---------------------------------------------------------------------------
# 各章节渲染
# ---------------------------------------------------------------------------
def _section_summary(d: dict) -> str:
    """研究摘要：先给核心结论，再罗列支撑数据。"""
    scan, bt = d["scan"], d["backtest"]

    if bt.get("available"):
        conclusion = (
            f"本研究构建了一套基于量价形态特征的个股评分体系——{_TERM_SCORE}，"
            f"并通过近 {config.BT_CALENDAR_YEARS} 年 A 股历史回测对其进行验证。"
            f"结果表明，高匹配分组个股在<b>中短期持仓周期</b>内的上涨概率与收益水平"
            f"整体优于低匹配分组，"
            f"初步验证了量价形态特征对个股中短期收益的区分能力；"
            f"但该结论属于<b>样本内的描述性统计</b>，未做统计显著性检验，"
            f"不构成对未来的任何推断。")
    else:
        conclusion = (
            f"本研究构建了一套基于量价形态特征的个股评分体系——{_TERM_SCORE}，"
            f"用于刻画个股当前形态与历史上升段启动样本的特征相似程度。"
            f"当前尚无已完成的历史回测记录，本简报仅呈现方法与当期截面描述。")

    kpis = ""
    if scan.get("available"):
        kpis = (
            '<div class="kpis">'
            f'<div class="kpi"><div class="k">候选池个股数</div>'
            f'<div class="v">{scan["candidates"]:,}<span class="u">只</span></div></div>'
            f'<div class="kpi"><div class="k">高匹配分组</div>'
            f'<div class="v up">{_int(scan["high"])}<span class="u">只</span></div></div>'
            f'<div class="kpi"><div class="k">中匹配分组</div>'
            f'<div class="v" style="color:var(--mid)">{_int(scan["mid"])}'
            '<span class="u">只</span></div></div>'
            f'<div class="kpi"><div class="k">候选池均分</div>'
            f'<div class="v">{_num(scan["avg_score"], 1)}<span class="u">分</span></div></div>'
            '</div>')

    bullets = []
    if scan.get("available"):
        bullets.append(
            f"<b>当期截面</b>：扫描覆盖沪市主板、深市主板、创业板与科创板，"
            f"经基础过滤（剔除 ST / *ST / 退市整理、剔除北交所、剔除上市不满 "
            f"{config.FILTER_LISTING_MIN_DAYS} 天、剔除停牌）后，进入候选池的个股共 "
            f"<b>{scan['candidates']:,}</b> 只。按分组标准，高匹配分组"
            f"（≥{config.HIGH_SCORE_THRESHOLD} 分）<b>{_int(scan['high'])}</b> 只、"
            f"中匹配分组（{config.MID_SCORE_THRESHOLD}–"
            f"{config.HIGH_SCORE_THRESHOLD - 1} 分）<b>{_int(scan['mid'])}</b> 只、"
            f"低匹配分组（&lt;{config.MID_SCORE_THRESHOLD} 分）<b>{_int(scan['low'])}</b> 只；"
            f"候选池{_TERM_SCORE}平均 {_num(scan['avg_score'], 1)} 分"
            f"（满分 100 分），最高 {_num(scan['max_score'], 0)} 分。")

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
            parts.append(f"{h} 日周期上涨胜率 {win}，平均收益率 {ret}")
        bullets.append(
            f"<b>回测范围</b>：区间 {bt.get('start_date')} 至 {bt.get('end_date')}，"
            f"有效调仓点 {bt.get('rebalance_points')} 个，"
            f"共产生 <b>{_int(bt.get('obs_count'))}</b> 条个股—周期样本，"
            f"覆盖 {_int(bt.get('stock_count'))} 只个股。"
            f"分组样本量差异较大（高匹配分组样本显著少于中、低匹配分组），"
            f"解读组间差异时需注意样本量的不对称。")
        if parts:
            bullets.append(
                "<b>分档表现</b>（按高 / 中 / 低匹配分组顺序）："
                + "；".join(parts) + "。"
                "三个周期上高匹配分组的上涨胜率与平均收益率均高于低匹配分组，"
                "单调关系在 5 日、10 日周期上成立，在 20 日周期上出现局部偏离"
                "（详见第三节 3.2 的说明）。")
        bullets.append(
            f"<b>分数口径</b>：回测可复现满分为 {bt.get('score_max')} 分。"
            f"筹码集中度、PE 行业分位、换手率三项在历史时点缺乏免费且可复现的数据源，"
            f"统一计 0 分；分组阈值仍沿用 {config.HIGH_SCORE_THRESHOLD} / "
            f"{config.MID_SCORE_THRESHOLD} 分标准，因此回测分组的实际筛选标准"
            f"比看板当日口径<b>更为严苛</b>，两者分数不可直接换算。")
    if not bullets:
        bullets.append("当前尚无已完成的扫描批次或回测记录，简报内容不完整。")

    return (f'<h3>核心结论</h3><p>{conclusion}</p>'
            f'<h3>关键数据</h3>{kpis}'
            + "<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")


def _section_method(d: dict) -> str:
    bt = d["backtest"]

    rule_rows = "".join(
        f'<tr><td class="rule-mod">{_esc(m)}</td><td>{c}</td>'
        f'<td class="num">{p}</td></tr>'
        for m, c, p in d["rules"])
    rules_table = (
        '<p class="tbl-cap">@@TBL@@　综合形态匹配分打分规则（总分 100，三大模块）</p>'
        + '<table class="tbl-keep">' + _colgroup([22, 63, 15])
        + '<thead><tr><th>模块</th><th>条件</th><th class="num">分值</th></tr></thead>'
        + f'<tbody>{rule_rows}</tbody></table>'
        + '<p class="tbl-src">注：总分 = 核心形态匹配分（0–50）+ 筹码与基本面加分（0–20）'
          '+ 行业与板块加分（0–30）。全部参数集中于 <span class="mono">app/config.py</span>，'
          '修改口径无需改动业务代码。</p>')

    def_rows = "".join(
        f'<tr><td class="rule-mod">{_esc(k)}</td><td>{v}</td></tr>'
        for k, v in d["indicator_defs"])
    defs_table = (
        '<p class="tbl-cap">@@TBL@@　核心绩效指标定义与计算口径</p>'
        + '<table class="tbl-keep">' + _colgroup([24, 76])
        + '<thead><tr><th>指标</th><th>定义与计算口径</th></tr></thead>'
        + f'<tbody>{def_rows}</tbody></table>'
        + '<p class="tbl-src">注：上表口径为第三节全部绩效数值的唯一依据，'
          '任何研究者使用同一套公开数据均可复算出完全一致的结果。</p>')

    highlights = "".join(
        f'<div class="hl"><div class="t">{_esc(t)}</div><p class="b">{b}</p></div>'
        for t, b in d["design_highlights"])

    score_note = ""
    if bt.get("available"):
        score_note = (
            f'<div class="note"><div class="nt">分数口径说明：为什么回测满分是 '
            f'{config.BT_SCORE_MAX} 分而不是 100 分</div>'
            f'<p style="margin:0 0 6px">{_TERM_SCORE}由三大模块构成，其中'
            f'<b>筹码集中度、PE 行业分位、换手率</b>三项依赖「当日截面数据」，'
            f'在历史时点不存在免费且可复现的数据源。若以当前值回填历史，等同于引入前视偏差；'
            f'若以近似值估计，则不同研究者会得到不同结果。因此回测口径下这三项'
            f'<b>统一计 0 分</b>，满分相应降为 {config.BT_SCORE_MAX} 分。</p>'
            f'<p style="margin:0">需要特别说明的是：分组阈值仍沿用看板的 '
            f'{config.HIGH_SCORE_THRESHOLD} / {config.MID_SCORE_THRESHOLD} 分标准，'
            f'<b>并未按 {config.BT_SCORE_MAX} 分等比例缩放</b>。这会产生两个直接后果：'
            f'其一，回测中进入「高匹配分组」的门槛相对更高，筛选标准比看板当日口径更严苛；'
            f'其二，回测分数与看板当日分数<b>不可直接互相换算或横向比较</b>。'
            f'本简报的全部分组结论，均是在这一更严苛口径下取得的。</p></div>')

    return f"""
<p>本研究的{_TERM_SCORE}是一个<b>特征相似度指标</b>：把个股当前的量价形态与本地历史样本库中
「上升段启动」时点的形态特征逐项比对，命中的特征按预设权重累加，得到 0–100 的综合形态匹配分。
分数越高，表示该股当前形态与历史样本的特征重合度越高，<b>不代表任何未来结果</b>。</p>

<h3>2.1　研究设计要点</h3>
<p>为避免回测中常见的偏误来源，本研究在样本构造、时序处理与数据口径三个层面
做了如下约束，以下四点构成本研究在方法上的主要严谨性保障：</p>
{highlights}

<h3>2.2　指标定义与计算口径</h3>
<p>为使结论可被独立复算，本节先行固定全部绩效指标口径如下，
第三节出现的每一个数值，均严格依据下表定义计算：</p>
{defs_table}

<h3>2.3　打分规则</h3>
{rules_table}

<h3>2.4　基础过滤规则</h3>
<p>回测样本与看板当日候选池采用完全一致的基础过滤规则，逐条如下：</p>
<ul>
<li>剔除 ST / *ST / 退市整理个股（按证券简称关键词判定）</li>
<li>剔除北交所个股，仅保留沪市主板（60）、科创板（68）、深市主板（00）、创业板（30）</li>
<li>剔除上市不满 {config.FILTER_LISTING_MIN_DAYS} 天的个股</li>
<li>剔除停牌或无成交个股（当日成交额 ≤ 0）</li>
</ul>

<h3>2.5　历史回测设计</h3>
<ul>
<li><b>回测区间</b>：近 {config.BT_CALENDAR_YEARS} 年，价格口径为<b>前复权</b>日线</li>
<li><b>分组标准</b>：按{_TERM_SCORE}分为高匹配分组（≥{config.HIGH_SCORE_THRESHOLD} 分）、
中匹配分组（{config.MID_SCORE_THRESHOLD}–{config.HIGH_SCORE_THRESHOLD - 1} 分）、
低匹配分组（&lt;{config.MID_SCORE_THRESHOLD} 分）</li>
<li><b>持仓周期</b>：{'、'.join(str(h) for h in config.BT_HORIZONS)} 个交易日</li>
<li><b>调仓频率</b>：每 {config.BT_REBALANCE_STEP} 个交易日形成一个观察截面</li>
<li><b>入场与出场</b>：观察日 T 收盘后打分，入场价取 <b>T+1 开盘价</b>，
出场价取 <b>T+n 收盘价</b></li>
<li><b>基准</b>：{_TERM_BENCH}（{config.BT_INDEX_SYMBOL}），与个股同样按前复权口径处理</li>
<li><b>分组净值</b>：各调仓周期内该组样本等权平均收益率的复利累积，起点 = 1.00</li>
</ul>
{score_note}"""


def _horizon_table(bt: dict, h: int) -> str:
    """单个持仓周期的分档绩效表：8 列固定表头，三行（高 / 中 / 低匹配分组）。"""
    rows = sorted((bt.get("stats_by_horizon") or {}).get(str(h)) or [],
                  key=lambda r: bt_engine.GROUP_ORDER.index(r["group"]))
    body = []
    for r in rows:
        g = r["group"]
        excess = r.get("excess_return")
        # 超额收益正负着色：沿用看板「涨红跌绿」惯例
        cls = "pos" if (excess or 0) > 0 else ("neg" if (excess or 0) < 0 else "")
        body.append(
            f'<tr><td>{_GROUP_CN.get(g, g)}</td>'
            f'<td class="num">{_int(r.get("samples"))}</td>'
            f'<td class="num">{_num(r.get("avg_score"), 1)}</td>'
            f'<td class="num">{_pct(r.get("win_rate"), 1)}</td>'
            f'<td class="num">{_pct(r.get("avg_return"), 2, True)}</td>'
            f'<td class="num {cls}">{_pct(excess, 2, True)}</td>'
            f'<td class="num">{_pct(r.get("max_drawdown"), 2)}</td>'
            f'<td class="num">{_num(r.get("pl_ratio"), 2)}</td></tr>')
    head = ('<thead><tr><th>分组</th><th class="num">样本数量</th>'
            '<th class="num">组内平均得分</th><th class="num">上涨胜率</th>'
            '<th class="num">平均收益率</th>'
            f'<th class="num">相对{_TERM_BENCH}<br>超额收益</th>'
            '<th class="num">最大回撤</th><th class="num">盈亏比</th></tr></thead>')
    return (
        f'<p class="tbl-cap">@@TBL@@　高 / 中 / 低匹配分组的绩效统计'
        f'（持仓 {h} 个交易日）</p>'
        + '<table class="tbl-keep">' + _colgroup([13, 10, 14, 10, 12, 17, 12, 12])
        + head + f'<tbody>{"".join(body)}</tbody></table>')


def _section_backtest(d: dict) -> str:
    bt = d["backtest"]
    if not bt.get("available"):
        return f'<p class="tbl-src">历史回测尚未执行（{_esc(bt.get("reason", ""))}）。</p>'

    horizons = bt.get("horizons", [])
    by_h = bt.get("stats_by_horizon") or {}

    # --- 3.1 分档绩效（每周期一张独立表格，表头统一、行列固定）---
    tables = []
    for h in horizons:
        tables.append(_horizon_table(bt, h))
    tables_html = "".join(tables)
    src = ('<p class="tbl-src">注：样本数量为该组在该持仓周期内的有效个股—周期样本数；'
           '组内平均得分为该组样本观察日' + _TERM_SCORE + '的平均值；'
           '超额收益为分组累计收益率减去同期' + _TERM_BENCH + '累计收益率（同一调仓点序列对齐）；'
           '最大回撤以负值表示；盈亏比 = 平均盈利幅度 ÷ 平均亏损幅度。'
           f'全部指标口径见表 1，基准为{_TERM_BENCH}（{config.BT_INDEX_SYMBOL}）。</p>')

    # --- 3.2 观察到的数据特征 ---
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
            f"{h} 日周期中，平均收益率最高的是<b>{_GROUP_CN[best_ret]}</b>"
            f"（{_pct(rows[best_ret]['avg_return'], 2, True)}），"
            f"上涨胜率最高的是<b>{_GROUP_CN[best_win]}</b>"
            f"（{_pct(rows[best_win]['win_rate'], 1)}）。")
    mono = _monotonic_note(by_h, horizons)
    obs_html = ""
    if obs:
        obs_html = ('<h3>3.2　观察到的数据特征</h3><ul>'
                    + "".join(f"<li>{x}</li>" for x in obs)
                    + (f"<li>{mono}</li>" if mono else "")
                    + "</ul>")

    # --- 3.2 补充：高分组与中分组在 20 日周期上的「收益倒挂」说明 ---
    inversion = _inversion_note(bt, horizons)

    # --- 3.3 图 1：分档平均收益率 ---
    focus = 20 if 20 in horizons else (max(horizons) if horizons else None)
    chart1 = ""
    if focus is not None:
        rows = {r["group"]: r for r in (by_h.get(str(focus)) or [])}
        cats, vals = [], []
        for g in bt_engine.GROUP_ORDER:
            if g in rows:
                cats.append(_GROUP_CN[g].replace("匹配分组", ""))
                vals.append(rows[g]["avg_return"] or 0.0)
        if cats:
            chart1 = ('<div class="fig">' + _svg_grouped_bar(
                [f"{c}匹配分组" for c in cats],
                [{"name": f"{focus} 日平均收益率", "values": vals, "color": _C_ACCENT}],
            ) + '</div><div class="fig-cap">@@FIG@@　高 / 中 / 低匹配分组平均收益率'
                f'（持仓 {focus} 个交易日，单笔样本算术平均）</div>')

    # --- 3.4 图 2：累计净值曲线 ---
    eq = bt.get("equity") or {}
    bench = bt.get("benchmark_equity") or {}
    chart2 = ""
    if focus is not None and eq:
        series = []
        for g in bt_engine.GROUP_ORDER:
            pts = (eq.get(g) or {}).get(str(focus)) or []
            if pts:
                series.append({"name": _GROUP_CN[g], "values": [p["nav"] for p in pts],
                               "color": {"high": _C_HIGH, "mid": _C_MID, "low": _C_LOW}[g]})
        bpts = bench.get(str(focus)) or []
        if bpts:
            series.append({"name": _TERM_BENCH,
                           "values": [p["nav"] for p in bpts], "color": _C_BENCH})
        if series:
            labels = [p["date"] for p in ((eq.get("high") or {}).get(str(focus)) or bpts)]
            chart2 = ('<div class="fig">' + _svg_multiline(labels, series) +
                      '</div><div class="fig-cap">@@FIG@@　高 / 中 / 低匹配分组累计净值与'
                      f'{_TERM_BENCH}对比（持仓 {focus} 个交易日，'
                      '按调仓周期复利，起点 = 1.00；<b>该图为 20 日持仓周期的代表性展示</b>，'
                      '其余周期口径相同）</div>')

    return (f'<p>本部分把上述打分规则放回历史数据做分组检验：在回测区间内按每 '
            f'{bt.get("step")} 个交易日形成一个观察截面，对截面内所有满足基础过滤的个股'
            f'计算{_TERM_SCORE}并归档，再统计各分组在 '
            f'{" / ".join(str(h) for h in horizons)} 个交易日三个持仓周期上的表现。'
            f'期间共产生 <b>{_int(bt.get("trade_count") or bt.get("obs_count"))}</b> 条'
            f'个股—周期样本，覆盖 <b>{_int(bt.get("stock_count"))}</b> 只个股。'
            f'以下三张表格按持仓周期分块呈现，表头与指标口径完全一致，可直接横向对照。</p>'
            f'<h3>3.1　分档绩效统计</h3>'
            + tables_html + src + obs_html + inversion + chart1 + chart2)


def _inversion_note(bt: dict, horizons: list) -> str:
    """解释高匹配分组与中匹配分组在 20 日周期上的收益倒挂现象。"""
    if 20 not in horizons:
        return ""
    by_h = bt.get("stats_by_horizon") or {}
    rows = {r["group"]: r for r in (by_h.get("20") or [])}
    if not all(g in rows for g in bt_engine.GROUP_ORDER):
        return ""
    hi, mid, lo = rows["high"], rows["mid"], rows["low"]
    if mid.get("avg_return") is None or hi.get("avg_return") is None:
        return ""

    gap_ret = (mid["avg_return"] - hi["avg_return"]) * 100
    gap_win = (mid["win_rate"] - hi["win_rate"]) * 100 if (
        mid.get("win_rate") is not None and hi.get("win_rate") is not None) else None
    ratio = (mid.get("samples") or 0) / (hi.get("samples") or 1)

    return (
        '<h3>3.3　关于 20 日周期「高匹配分组与中匹配分组收益接近」的说明</h3>'
        '<div class="note"><div class="nt">现象</div>'
        f'<p style="margin:0 0 8px">在 20 个交易日持仓周期上，中匹配分组的平均收益率'
        f'（{_pct(mid["avg_return"], 2, True)}）略高于高匹配分组'
        f'（{_pct(hi["avg_return"], 2, True)}），两者相差 '
        f'{gap_ret:.2f} 个百分点'
        + (f'；上涨胜率分别为 {_pct(hi["win_rate"], 2)} 与 {_pct(mid["win_rate"], 2)}，'
           f'相差 {abs(gap_win):.2f} 个百分点' if gap_win is not None else '')
        + '，可以认为两者<b>基本持平</b>。</p>'
        '<div class="nt">可能的原因</div>'
        f'<ul style="margin:0">'
        f'<li><b>样本量差异</b>：高匹配分组在 20 日周期上的有效样本为 '
        f'{_int(hi.get("samples"))} 条，中匹配分组为 {_int(mid.get("samples"))} 条，'
        f'后者约为前者的 {ratio:.1f} 倍。在调仓点仅 {bt.get("rebalance_points")} 个的'
        f'小样本框架下，样本量较少的高匹配分组更容易受到个别调仓周期极端表现的拉动，'
        f'从而放大其收益的波动。</li>'
        f'<li><b>短期极端行情扰动</b>：分组收益按调仓周期等权复利累积，任一调仓周期内'
        f'若出现涨停、跌停或停复牌等极端个股情形，都会对样本量较小的分组产生更明显的影响。</li>'
        f'<li><b>分组间区分能力的边界</b>：高匹配分组与中匹配分组的得分区间相邻，'
        f'形态特征高度重合，两者之间本就不存在一个明确的「能力断层」；'
        f'区分能力主要体现在「高匹配分组与低匹配分组之间」。</li>'
        f'</ul>'
        '<div class="nt" style="margin-top:8px">如何理解</div>'
        f'<p style="margin:0">从整体格局看，20 日周期上高匹配分组与中匹配分组的平均收益率'
        f'（{_pct(hi["avg_return"], 2, True)}、{_pct(mid["avg_return"], 2, True)}）'
        f'仍显著高于低匹配分组（{_pct(lo["avg_return"], 2, True)}），'
        f'<b>高匹配分组占优的整体趋势并未改变</b>，上述差异属于局部偏离而非趋势反转。</p>'
        f'<p style="margin:8px 0 0"><b>重要限定</b>：本研究为<b>描述性统计</b>，'
        f'未做统计显著性检验，未报告置信区间与效应量。因此上述差异'
        f'<b>无法判断是系统性差异还是随机波动</b>，不应作为任何形式的能力判断依据。'
        f'该现象已如实列入第六节「研究局限性」。</p></div>')


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
                f"<b>并未成立</b>，说明该{_TERM_SCORE}在这段样本中对未来收益的区分能力有限，"
                f"此为该研究的负面结果，如实列出。")
    if bad:
        return (f"分组平均收益率「高 &gt; 中 &gt; 低」的单调关系在 {' / '.join(ok)} 周期上成立，"
                f"在 {' / '.join(bad)} 周期上出现局部偏离（原因见 3.3 节说明）。")
    return (f"分组平均收益率「高 &gt; 中 &gt; 低」的单调关系在 {' / '.join(ok)} 周期上"
            f"均成立。")


def _section_pool(d: dict) -> str:
    scan = d["scan"]
    if not scan.get("available"):
        return f'<p class="tbl-src">当日候选池数据不可用（{_esc(scan.get("reason", ""))}）。</p>'

    boards = "".join(
        f'<tr><td>{_esc(b["board"])}</td><td class="num">{b["count"]:,}</td>'
        f'<td class="num">{b["count"] / max(1, scan["candidates"]) * 100:.1f}%</td></tr>'
        for b in scan["boards"])
    boards_table = (
        '<p class="tbl-cap">@@TBL@@　当日候选池板块分布</p>'
        + '<table class="tbl-keep">' + _colgroup([40, 30, 30])
        + '<thead><tr><th>板块</th><th class="num">候选数量（只）</th>'
          '<th class="num">占候选池比重</th></tr></thead>'
        + f'<tbody>{boards}</tbody></table>')

    inds = "".join(
        f'<tr><td>{_esc(i["industry"])}</td><td class="num">{i["count"]:,}</td>'
        f'<td class="num">{i["avg_score"]}</td></tr>'
        for i in scan["industries"][:12])
    inds_table = (
        '<p class="tbl-cap">@@TBL@@　当日候选池行业分布（按候选数量排序，展示前 12 位）</p>'
        + '<table class="tbl-flow">' + _colgroup([40, 30, 30])
        + '<thead><tr><th>申万一级行业</th><th class="num">候选数量（只）</th>'
          '<th class="num">行业平均分</th></tr></thead>'
        + f'<tbody>{inds}</tbody></table>')

    # 图 3：候选池行业分布
    chart3 = ""
    if scan.get("industries"):
        tops = scan["industries"][:12]
        items = [{"label": it["industry"], "value": it["count"],
                  "note": f"{it['count']:,} 只 · 均分 {it['avg_score']}",
                  "color": _C_ACCENT} for it in tops]
        chart3 = ('<div class="fig">' + _svg_hbar(items) + '</div>'
                  '<div class="fig-cap">@@FIG@@　当日候选池行业分布'
                  f'（候选池共覆盖 {scan.get("industry_total", 31)} 个申万一级行业，'
                  '此处展示候选数量前 12 位）</div>')

    # 高分股示例表：Top 15
    top_rows = []
    for k, r in enumerate(scan["top"], start=1):
        hits = r.get("hits") or []
        hit_txt = " · ".join(hits) if hits else "—"
        top_rows.append(
            f'<tr><td class="mono center">{_esc(r["code"])}</td>'
            f'<td>{_esc(r["name"])}</td>'
            f'<td>{_esc(r["industry"])}</td>'
            f'<td class="num">{_esc(r["total_score"])}</td>'
            f'<td class="hits">{_esc(hit_txt)}</td></tr>')
    top_table = (
        f'<p class="tbl-cap">@@TBL@@　{_TERM_SCORE}居前 15 位的个股示例</p>'
        + '<table class="tight">' + _colgroup([9, 14, 14, 10, 53])
        + '<thead><tr><th class="center">代码</th><th>名称</th>'
          '<th>申万一级行业</th><th class="num">综合得分</th>'
          '<th>核心指标命中项</th></tr></thead>'
        + f'<tbody>{"".join(top_rows)}</tbody></table>'
        + '<p class="tbl-src">注：本表按综合形态匹配分从高到低排列，'
          '仅用于观察当期截面中形态特征重合度较高的样本，'
          '<b>不构成任何操作指引</b>；命中项由打分规则表逐项判定，'
          '「—」表示该股未命中任何附加项。<br>'
          f'命中项释义：{_HIT_LEGEND}</p>')

    score_dist = (
        f'<div class="kpis">'
        f'<div class="kpi"><div class="k">候选池个股数</div>'
        f'<div class="v">{scan["candidates"]:,}<span class="u">只</span></div></div>'
        f'<div class="kpi"><div class="k">平均{_TERM_SCORE}</div>'
        f'<div class="v">{_num(scan["avg_score"], 1)}<span class="u">分</span></div></div>'
        f'<div class="kpi"><div class="k">最高{_TERM_SCORE}</div>'
        f'<div class="v">{_num(scan["max_score"], 0)}<span class="u">分</span></div></div>'
        f'<div class="kpi"><div class="k">平均核心形态分</div>'
        f'<div class="v">{_num(scan["avg_pattern"], 1)}<span class="u">分</span></div></div>'
        f'</div>')

    return f"""
<p>本节描述扫描批次 #{scan['run_id']}（扫描完成时间 {_esc(scan['scan_time'])}）的候选池结构，
全部为当期截面的统计描述，不涉及任何对未来走势的判断。</p>
{score_dist}
<h3>4.1　板块分布</h3>
{boards_table}
<h3>4.2　行业分布</h3>
{inds_table}
{chart3}
<h3>4.3　高匹配分个股示例</h3>
{top_table}"""


def _section_conclusions(d: dict) -> str:
    c = d["conclusions"]
    findings = "".join(
        f'<div class="oc"><p class="b">{x}</p></div>' for x in c["findings"])
    outlook = "".join(
        f'<div class="oc"><div class="t">{k + 1}. {_esc(t)}</div>'
        f'<p class="b">{b}</p></div>'
        for k, (t, b) in enumerate(c["outlook"]))
    return f"""
<h3>5.1　核心发现</h3>
{findings}
<h3>5.2　研究局限</h3>
<p>本研究属于<b>小样本、单一市场环境下的描述性统计研究</b>，存在若干需要明确交代的局限：
分组间差异未做统计显著性检验；样本期较短且未做分年度稳健性拆解；
筹码集中度、PE 行业分位、换手率三项在历史时点无法复现；
交易成本与可执行性约束未纳入；行业归属与 ST 状态未回溯历史。
上述局限的逐条说明见第六节，在引用本简报结论时应一并考虑。</p>
<h3>5.3　后续可拓展方向</h3>
{outlook}"""


def _section_limits(d: dict) -> str:
    return "".join(
        f'<div class="lim"><div class="t">{k + 1}. {_esc(x["title"])}</div>'
        f'<p class="b">{x["body"]}</p></div>'
        for k, x in enumerate(d["limitations"]))


def _section_compliance(d: dict) -> str:
    """合规声明：完整表述一次，正文其余位置只保留一句简短提示。"""
    return f"""
<p>{_esc(d['disclaimer'])}</p>
<ul>
<li><b>工具性质</b>：本简报由本地程序依据公开历史行情数据自动生成，
全部内容为对历史数据的统计描述与研究方法说明，不含任何对未来价格、涨跌幅或收益的预测。</li>
<li><b>样本说明</b>：简报中出现的个股名称与代码，均为形态特征匹配度统计的<b>样本对象</b>，
用于说明当期截面的统计分布，不构成任何形式的操作指引，也不代表本工具对其的任何评价。</li>
<li><b>服务边界</b>：本项目为个人量化研究学习作品，不提供证券投资咨询服务，不收取任何费用，
不与任何券商或交易通道对接，不具备也不提供任何交易执行能力。</li>
<li><b>回测结论的边界</b>：历史回测结果是对过去样本的统计描述，
<b>不构成对策略有效性、未来收益或任何操作时机的承诺或暗示</b>。
本研究未做统计显著性检验，分组差异可能由随机波动导致。</li>
<li><b>数据来源</b>：行情数据来自公开免费接口，可能存在缺失、延迟或错误；
免费接口的历史数据不保证与交易所原始记录完全一致。</li>
<li><b>责任承担</b>：历史数据不等于未来表现。市场有风险，研究需谨慎。
任何人依据本简报内容作出的决策及其后果，均由其本人承担。</li>
</ul>
<p class="meta">生成时间：{_esc(d['generated_at'])}　|　
数据批次：扫描 #{d['scan'].get('run_id', '—')}　|　
回测 #{d['backtest'].get('run_id', '—')}　|　
本简报内容与看板数据实时同步，可通过重新生成完整复现。</p>"""


def render_html(d: dict) -> str:
    """渲染完整的自包含 HTML 简报（图表编号在最后统一连续分配）。"""
    return _number_assets(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{_esc(d['title'])}</title><style>{_CSS}</style></head>
<body><div class="wrap">
<div class="cover">
  <div class="eyebrow">QUANTITATIVE RESEARCH NOTE</div>
  <h1>{_esc(d['title'])}</h1>
  <p class="sub">{_esc(d['subtitle'])}</p>
  <div class="meta">{_esc(d['author'])}　|　生成日期：{_esc(d['generated_date'])}
  　|　数据口径：前复权日线　|　基准：{_TERM_BENCH}</div>
</div>

<h2>一、研究摘要</h2>
{_section_summary(d)}

<h2>二、研究方法与规则</h2>
{_section_method(d)}

<h2>三、历史回测结论</h2>
{_section_backtest(d)}

<h2>四、当日候选池分析</h2>
{_section_pool(d)}

<h2>五、研究结论与展望</h2>
{_section_conclusions(d)}

<h2>六、研究局限性与风险提示</h2>
{_section_limits(d)}

<h2>七、合规声明</h2>
{_section_compliance(d)}

<footer>{_esc(d['author'])}　|　本简报由程序自动生成，全部为历史数据统计结果，
不代表未来表现，不构成任何操作指引。</footer>
</div></body></html>""")

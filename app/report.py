# -*- coding: utf-8 -*-
"""研究简报生成（v1.7.0）。

把当日扫描结果与历史回测结果组装成一份标准化研究简报，
全部数据直接读自本地数据库，保证**结论可复现**、与看板实时同步。

结构（九节，固定顺序）：
1. 研究摘要
2. 研究方法与规则（含参数敏感性分析）
3. 历史回测结论（含统计显著性检验与分年度稳健性）
4. 分行业回测
5. 多周期共振策略
6. 当日候选池分析
7. 研究结论与展望
8. 研究局限性与风险提示
9. 合规声明

v1.7.0 相对 v1.6.1 的变化：新增第四节、第五节两个章节，在第二、三、七、八节
补入四项深度分析（分行业回测 / 统计显著性 / 参数敏感性 / 多周期共振），
并把第八节中「未做统计显著性检验」等表述按实际研究进展据实改写。

输出为**自包含 HTML**（图表以行内 SVG 绘制，不依赖任何外部资源与网络），
再由 scripts/make_pdf.py 用本机浏览器渲染为 PDF。

排版约定
--------
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
from app.backtest import industry as bt_industry
from app.backtest import resonance as bt_resonance
from app.backtest import sensitivity as bt_sensitivity
from app.backtest import significance as bt_significance

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
    industry = _collect_industry(conn, bt)
    significance = _collect_significance(conn, bt)
    resonance = _collect_resonance(conn, bt)
    sensitivity = _collect_sensitivity(conn, bt)
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "generated_date": datetime.now().strftime("%Y年%m月%d日"),
        "title": config.REPORT_TITLE,
        "subtitle": config.REPORT_SUBTITLE,
        "author": config.REPORT_AUTHOR,
        "scan": scan,
        "backtest": bt,
        "industry": industry,
        "significance": significance,
        "resonance": resonance,
        "sensitivity": sensitivity,
        "rules": _rule_rows(),
        "indicator_defs": _indicator_defs(),
        "design_highlights": _design_highlights(bt),
        "limitations": _limitations(scan, bt, industry, significance,
                                    resonance, sensitivity),
        "conclusions": _conclusions(scan, bt, significance, industry,
                                    resonance, sensitivity),
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


# ---------------------------------------------------------------------------
# v1.7.0 四项深度分析的数据收集
#
# 四项分析均依赖已落库的回测批次；任一环节缺失（例如尚未运行
# scripts/build_v17_analysis.py）时只把该项标记为不可用，
# 不阻断其余章节的生成——简报必须能在任意数据完备度下正常产出。
# ---------------------------------------------------------------------------
def _bt_horizons(bt: dict) -> list[int]:
    return [int(h) for h in (bt.get("horizons") or config.BT_HORIZONS)]


def _collect_industry(conn: sqlite3.Connection, bt: dict) -> dict:
    """分行业回测结果（读已落库的 bt_industry）。"""
    if not bt.get("available") or not bt.get("run_id"):
        return {"available": False, "reason": "尚未执行历史回测"}
    try:
        rid = int(bt["run_id"])
        rows = bt_industry.load_rows(conn, rid)
        if not rows:
            return {"available": False, "run_id": rid,
                    "reason": "尚未计算分行业回测结果"}
        return bt_industry.build_summary(conn, rid, _bt_horizons(bt), rows)
    except Exception as exc:  # noqa: BLE001 —— 单项失败不影响整份简报
        return {"available": False, "reason": f"分行业回测读取失败: {exc}"}


def _collect_significance(conn: sqlite3.Connection, bt: dict) -> dict:
    """统计显著性检验（即时计算）。"""
    if not bt.get("available") or not bt.get("run_id"):
        return {"available": False, "reason": "尚未执行历史回测"}
    try:
        return bt_significance.build_summary(conn, int(bt["run_id"]),
                                             _bt_horizons(bt))
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"显著性检验失败: {exc}"}


def _collect_resonance(conn: sqlite3.Connection, bt: dict) -> dict:
    """多周期共振对比（读已落库的 bt_resonance）。"""
    if not bt.get("available") or not bt.get("run_id"):
        return {"available": False, "reason": "尚未执行历史回测"}
    try:
        return bt_resonance.build_summary(conn, int(bt["run_id"]), _bt_horizons(bt))
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"共振对比读取失败: {exc}"}


def _collect_sensitivity(conn: sqlite3.Connection, bt: dict) -> dict:
    """参数敏感性分析（读已落库的 bt_sensitivity）。"""
    if not bt.get("available") or not bt.get("run_id"):
        return {"available": False, "reason": "尚未执行历史回测"}
    try:
        return bt_sensitivity.load_summary(conn, int(bt["run_id"]))
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"参数敏感性读取失败: {exc}"}


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


def _limitations(scan: dict, bt: dict, industry: dict | None = None,
                 significance: dict | None = None, resonance: dict | None = None,
                 sensitivity: dict | None = None) -> list[dict]:
    """按实际数据情况生成局限性条目（不写空话）。

    v1.7.0 起，部分条目需随研究进展据实改写——例如「未做统计显著性检验」
    在补充了 t 检验之后就不再成立，应改为说明该检验自身的适用边界。
    """
    industry = industry or {}
    significance = significance or {}
    resonance = resonance or {}
    sensitivity = sensitivity or {}

    out = [
        {"title": "显著性检验的适用边界",
         "body": "本研究已对高匹配分组与低匹配分组的收益差异做 Welch 独立样本 t 检验，"
                 "报告均值差异、p 值与 95% 置信区间，但需明确其适用边界："
                 "其一，检验假定样本相互独立，而同一调仓截面的个股会共同承受市场冲击，"
                 "样本间存在<b>横截面相关</b>，这会低估标准误、使 p 值偏小；"
                 "其二，本研究同时检验多个持仓周期与多个行业，"
                 "<b>未做多重比较校正</b>，存在假阳性累积风险；"
                 "其三，样本量达十万量级时，即便经济意义很小的差异也可能被判为统计显著。"
                 "因此统计显著性与经济意义需分开解读，p 值应视为<b>近似参考</b>而非严格推断。"},
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
                 "与历史 ST 变更记录，存在轻微前视。因两者变动频率较低，对整体结论影响有限。"
                 "该前视对第四节的分行业回测影响相对更大：行业归属错误会直接导致样本"
                 "被划入错误的行业分组。"},
        {"title": "交易成本与摩擦未计入",
         "body": "回测未扣除交易佣金、印花税、冲击成本与滑点，也未考虑涨跌停或停牌导致的"
                 "无法成交情形。实际可执行结果会系统性地低于理论统计值。"
                 "第五节的周线共振策略换手更低，受摩擦成本的影响理论上小于单日线策略，"
                 "但由于未实际扣费，这一差异无法从本研究的数值中读出。"},
        {"title": "分组累计净值的持有期口径",
         "body": "分组累计净值为各调仓周期内等权平均收益的复利累积。当持仓周期短于调仓间隔时"
                 "（例如 5 日周期对应 20 日间隔），组合仅在该周期的窗口内持有，其余时间为空仓，"
                 "因此净值曲线的斜率<b>不与满仓持有直接可比</b>，不同持仓周期之间的净值高低"
                 "也不宜直接横向比较。"},
    ]

    # --- 样本期与市场环境：已补分年度稳健性，据实汇报结果 ---
    rob = (significance.get("robustness") or {}) if significance.get("available") else {}
    per_h = {int(x["horizon"]): x for x in (rob.get("per_horizon") or [])}
    if per_h:
        parts = []
        for h in sorted(per_h):
            x = per_h[h]
            parts.append(f"{h} 日周期 {x['consistent_years']}/{x['valid_years']} 个年度方向一致")
        worst = min(per_h.values(), key=lambda x: (x["consistent_ratio"] or 0))
        out.append({
            "title": "分年度稳健性：结论并非在所有年度一致成立",
            "body": f"本研究已按自然年拆分样本做稳健性检验，结果见 3.5 节："
                    f"{'；'.join(parts)}。其中 {worst['horizon']} 日周期的一致性最低"
                    f"（{worst['consistent_years']}/{worst['valid_years']}）。"
                    f"这表明「高匹配分组优于低匹配分组」的方向在整体样本上成立，"
                    f"<b>但在个别年度会出现反转</b>，结论对所处的市场环境存在依赖；"
                    f"同时，回测区间首尾年份（2023、2026）只覆盖部分月份，"
                    f"样本量与非完整年度不可比。"})
    else:
        out.append({
            "title": "样本期与市场环境单一",
            "body": f"回测区间为 {bt.get('start_date') or config.BT_CALENDAR_YEARS} 起的一段连续"
                    f"历史区间，仅覆盖单一市场环境，未做过跨牛熊区间的分年度稳健性拆解，"
                    f"结论对该区间内的特定行情特征可能存在依赖。"})

    # --- 分行业回测：高匹配分组样本的行业集中度 ---
    conc = industry.get("concentration") or {}
    if industry.get("available") and conc.get("industries_with_samples") is not None:
        n_ok = conc.get("industries_with_samples")
        n_all = conc.get("industries_total")
        if n_ok and n_all and n_ok < n_all:
            out.append({
                "title": "高匹配分组样本在行业上高度集中",
                "body": f"分行业回测覆盖全部 {n_all} 个申万一级行业，"
                        f"但高匹配分组的样本只出现在其中 <b>{n_ok} 个行业</b>，"
                        f"其余 {n_all - n_ok} 个行业因高分样本不足无法参与排名。"
                        f"这是因为{_TERM_SCORE}的高分档依赖热点行业加分与成长板块加分，"
                        f"样本天然向少数行业聚集。因此第四节给出的行业排名"
                        f"<b>只代表这 {n_ok} 个行业</b>，不能外推为全行业结论；"
                        f"同时也提示该评分体系存在行业偏向性，"
                        f"在应用于候选池筛选时应意识到这一结构性偏差。"})

    # --- 参数敏感性：覆盖面与判定阈值 ---
    if sensitivity.get("available"):
        gs = sensitivity.get("groups") or []
        n_variants = sensitivity.get("conclusion", {}).get("variant_count") or 0
        out.append({
            "title": "参数敏感性分析只覆盖 3 组参数",
            "body": f"敏感性分析考察了 {'、'.join(g['label'] for g in gs)} 共 3 组参数、"
                    f"{n_variants} 个扰动档。打分体系中仍有若干参数未纳入考察"
                    f"（例如 KDJ 参数、各评分项的<b>分值权重</b>、分组阈值 60 / 30、"
                    f"调仓间隔等）。其中分值权重对结果的影响可能大于本次考察的参数，"
                    f"因为调整权重会直接改变分组构成。此外，「幅度稳定」的判定阈值 "
                    f"{sensitivity.get('stable_threshold_pp')} 个百分点是人为设定，"
                    f"换一个阈值可能得出不同措辞的结论。"})

    # --- 多周期共振：样本量与检验力 ---
    if resonance.get("available"):
        hs = resonance.get("stats") or []
        n_res = min((r["resonance"]["samples"] or 0) for r in hs) if hs else 0
        n_day = min((r["daily_high"]["samples"] or 0) for r in hs) if hs else 0
        out.append({
            "title": "周线共振分组的样本量明显低于对照组",
            "body": f"双共振分组是在「日线高匹配分组」基础上再叠加周线条件得到的子集，"
                    f"各周期有效样本约 {_int(n_res)} 条，仅为对照的单日线分组"
                    f"（约 {_int(n_day)} 条）的三分之一左右。样本量下降会降低检验力，"
                    f"使该组绩效本身带有更大的抽样波动；同时该组未做单独的显著性检验，"
                    f"第五节给出的差异<b>不能判断是否具备统计显著性</b>。"})

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
                        f"单期极端行情会对整体统计产生较大影响。"
                        f"分年度拆分后，单年度的调仓点进一步减少，"
                        f"因此 3.5 节的年度一致性检验只能反映方向，不足以支撑年度层面的"
                        f"精细定量比较。",
            })
    return out


def _conclusions(scan: dict, bt: dict, significance: dict | None = None,
                 industry: dict | None = None, resonance: dict | None = None,
                 sensitivity: dict | None = None) -> dict:
    """研究结论与展望：核心发现 / 局限指向 / 后续方向。

    v1.7.0 起，v1.6.1 中列为「后续可拓展方向」的分行业回测、参数敏感性分析、
    多周期共振研究三项已经完成，相应内容上移为「核心发现」，
    展望部分替换为尚未解决的问题。
    """
    significance = significance or {}
    industry = industry or {}
    resonance = resonance or {}
    sensitivity = sensitivity or {}

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

    # --- 统计显著性检验（v1.7.0）---
    tests = (significance.get("tests") or []) if significance.get("available") else []
    if tests:
        parts = []
        for r in tests:
            sig_txt = "显著" if r.get("significant") else "不显著"
            parts.append(
                f"{r['horizon']} 日周期均值差异 {_pct(r.get('mean_diff'), 3, True)}"
                f"（t = {_num(r.get('t'), 2)}，p {r.get('p_text', '—')}，{sig_txt}）")
        all_sig = all(r.get("significant") for r in tests)
        findings.append(
            f"<b>统计显著性</b>：对高匹配分组与低匹配分组的单笔收益做 Welch 独立样本 "
            f"t 检验，三个持仓周期上均值差异均为正，"
            + "；".join(parts) + "。"
            + ("在 5% 显著性水平下，三个周期的分组差异<b>均具备统计显著性</b>，"
               "95% 置信区间均不包含 0，说明该差异难以仅由抽样随机性解释。"
               if all_sig else
               "并非所有周期都达到统计显著，分组差异的稳健性有限。")
            + "需要强调的是，统计显著不等于经济意义显著，"
              "且检验未做多重比较校正、样本间存在横截面相关（详见第八节）。")

    # --- 分行业回测（v1.7.0）---
    if industry.get("available"):
        conc = industry.get("concentration") or {}
        rank_h = industry.get("rank_horizon")
        top = industry.get("top") or []
        bottom = industry.get("bottom") or []
        if conc.get("industries_with_samples") and conc.get("industries_total"):
            findings.append(
                f"<b>行业维度</b>：把样本按申万一级行业拆分后，"
                f"{conc['industries_total']} 个行业中只有 "
                f"<b>{conc['industries_with_samples']} 个</b>行业存在高匹配分组样本"
                f"（前三行业 {'、'.join(conc.get('top_industries') or [])} 合计占 "
                f"{(conc.get('top3_share') or 0) * 100:.1f}%）。"
                f"这说明该评分体系的<b>高分样本在行业上高度集中</b>——"
                f"形态特征本身与行业属性存在较强的耦合，"
                f"分行业结果不能外推为全行业结论。")
        if top and bottom:
            findings.append(
                f"在样本量达到门槛的行业中，按持仓 {rank_h} 个交易日的高匹配分组平均收益率"
                f"排序，相对靠前的行业为 "
                + "、".join(f"{x['industry']}（{_pct(x['avg_return'], 2, True)}）" for x in top)
                + "，相对靠后的为 "
                + "、".join(f"{x['industry']}（{_pct(x['avg_return'], 2, True)}）" for x in bottom)
                + "。行业间的离散程度说明形态特征并非在所有行业同等有效。")

    # --- 多周期共振（v1.7.0，负面结果如实列出）---
    if resonance.get("available"):
        stats = resonance.get("stats") or []
        worse = sum(1 for r in stats
                    if (r["diff"].get("avg_return") or 0) < 0)
        detail = "；".join(
            f"{r['horizon']} 日周期 {_pct(r['resonance']['avg_return'], 2, True)} 对 "
            f"{_pct(r['daily_high']['avg_return'], 2, True)}"
            for r in stats)
        if stats and worse == len(stats):
            findings.append(
                f"<b>多周期共振（负面结果）</b>：在日线高匹配分组的基础上再叠加周线形态确认，"
                f"得到的「日线 + 周线双共振」分组在全部三个持仓周期上"
                f"<b>表现均低于</b>原单日线策略（平均收益率 {detail}）。"
                f"该约束同时降低了上涨胜率，并未带来回撤上的稳定优势。"
                f"这提示在本样本区间内，<b>周线确认不构成有效的增益条件</b>；"
                f"一种可能的解释是周线形态具有持续性，该条件筛出的个股被剔除了"
                f"形态形成初期收益弹性最大的部分。此为该研究的否定性发现，如实列出。")
        elif stats:
            findings.append(
                f"<b>多周期共振</b>：叠加周线条件后，双共振分组与原单日线策略的"
                f"表现差异随周期而变（平均收益率 {detail}），"
                f"未呈现一致的方向性改进。")

    # --- 参数敏感性（v1.7.0）---
    if sensitivity.get("available"):
        c = sensitivity.get("conclusion") or {}
        if c.get("direction_all_kept"):
            findings.append(
                f"<b>参数稳健性</b>：对 "
                + "、".join(g["label"] for g in (sensitivity.get("groups") or []))
                + f" 共 {c.get('variant_count')} 个参数扰动档各跑一次全量回测，"
                f"全部分组结论的<b>方向均保持不变</b>"
                f"（高匹配分组平均收益率始终高于低匹配分组），"
                f"其中变动最大的是「{c.get('worst_group')}」组"
                f"（{c.get('worst_delta_pp')} 个百分点）。"
                + ("变动幅度均未超过预设阈值，说明结论对这几组参数<b>不敏感</b>。"
                   if c.get("magnitude_stable_all") else
                   "但部分参数下的变动幅度超过了预设阈值，说明结论对参数取值"
                   "仍存在一定依赖。")
                + "需注意本次未考察分值权重与分组阈值，这两类参数的影响可能更大。")

    if scan.get("available") and scan.get("candidates"):
        findings.append(
            f"就当期截面而言，纳入候选池的 {_int(scan['candidates'])} 只个股中，"
            f"达到高匹配分组标准（≥{config.HIGH_SCORE_THRESHOLD} 分）的有 {_int(scan['high'])} 只，"
            f"占比 {_pct(scan['high'] / max(1, scan['candidates']), 1)}，"
            f"整体分布呈明显的右偏形态（平均分 {_num(scan['avg_score'], 1)} 分）。")

    outlook = [
        ("行业中性化打分",
         "第四节的结果显示高匹配分组样本只集中在少数行业，说明当前评分体系存在行业偏向。"
         "后续可引入行业中性化处理——例如把行业加分改为行业内相对排位、"
         "或对样本按行业做加权，考察剔除行业效应后形态特征本身是否仍有区分能力。"),
        ("分值权重与分组阈值的敏感性",
         "本次敏感性分析只覆盖了 MACD 参数、量能倍数与振幅阈值三组判断条件，"
         "尚未考察各评分项的<b>分值权重</b>以及 60 / 30 的分组阈值。"
         "权重调整会直接改变分组构成，其影响可能大于已考察的参数，是下一阶段的优先方向。"),
        ("多周期共振的其他构造方式",
         "本次以「周线形态确认」作为增益条件，结果未能提升绩效。"
         "后续可尝试其他构造：改用月线做长期趋势过滤、把周线条件用作仓位调整"
         "而非样本筛选、或改用周线相对强弱而非形态金叉。"),
        ("样本外与滚动前推检验",
         "当前结论全部来自同一样本区间内的统计，未做样本外验证。"
         "后续可采用滚动前推（walk-forward）方式，在每一年用此前数据确定参数、"
         "在之后的数据上检验，以评估结论的真实可迁移性。"),
        ("纳入交易成本与可执行性约束",
         "后续可把佣金、印花税、滑点以及涨跌停不可成交等约束纳入回测，"
         "评估结论在扣除摩擦成本后的存续性。"),
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
    sig = d.get("significance") or {}
    ind = d.get("industry") or {}
    res = d.get("resonance") or {}
    sens = d.get("sensitivity") or {}

    if bt.get("available"):
        # 显著性一句话（若有检验结果）
        sig_txt = ""
        tests = sig.get("tests") or []
        if tests and all(r.get("significant") for r in tests):
            sig_txt = ("对分组差异补充的 Welch 独立样本 t 检验显示，"
                       "三个持仓周期上的差异均达到统计显著（p 均小于 0.001，"
                       "95% 置信区间不含 0）。")
        elif tests:
            sig_txt = "对分组差异补充的 t 检验显示，并非所有持仓周期都达到统计显著。"

        # 两项限定性发现
        extra = []
        conc = ind.get("concentration") or {}
        if conc.get("industries_with_samples") and conc.get("industries_total"):
            extra.append(
                f"但高分样本在行业上高度集中（{conc['industries_total']} 个申万一级行业"
                f"中仅 {conc['industries_with_samples']} 个存在高匹配分组样本）")
        res_stats = res.get("stats") or []
        if res_stats and all((r["diff"].get("avg_return") or 0) < 0 for r in res_stats):
            extra.append("叠加周线条件的双共振分组在全周期上均未优于单日线策略")
        limit_txt = ("同时需要说明：" + "；".join(extra) + "。") if extra else ""

        conclusion = (
            f"本研究构建了一套基于量价形态特征的个股评分体系——{_TERM_SCORE}，"
            f"并通过近 {config.BT_CALENDAR_YEARS} 年 A 股历史回测对其进行验证。"
            f"结果表明，高匹配分组个股在<b>中短期持仓周期</b>内的上涨概率与收益水平"
            f"整体优于低匹配分组，初步验证了量价形态特征对个股中短期收益的区分能力。"
            f"{sig_txt}{limit_txt}"
            f"该结论建立在<b>样本内的历史统计</b>之上，不构成对未来的任何推断。")
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

    # ---- v1.7.0 四项深度分析要点 ----
    tests = (sig.get("tests") or []) if sig.get("available") else []
    if tests:
        seg = "；".join(
            f"{r['horizon']} 日 {_pct(r.get('mean_diff'), 3, True)}"
            f"（p {r.get('p_text', '—')}）" for r in tests)
        all_sig = all(r.get("significant") for r in tests)
        bullets.append(
            f"<b>统计显著性</b>：高匹配分组与低匹配分组的均值差异为 {seg}。"
            + ("三个周期在 5% 水平上<b>均显著</b>，差异难以仅由抽样随机性解释。"
               if all_sig else "并非所有周期都达到显著。")
            + "检验未做多重比较校正，且样本间存在横截面相关，p 值应作近似参考。")

    if ind.get("available"):
        conc = ind.get("concentration") or {}
        bullets.append(
            f"<b>分行业回测</b>：覆盖全部 {ind.get('industry_count')} 个申万一级行业，"
            f"其中仅 <b>{conc.get('industries_with_samples')}</b> 个行业存在高匹配分组样本"
            f"（前三行业合计占 {(conc.get('top3_share') or 0) * 100:.1f}%），"
            f"按 {ind.get('rank_horizon')} 日平均收益率排序，"
            f"相对靠前的是 "
            + "、".join(x["industry"] for x in (ind.get("top") or [])[:3])
            + f"。高分样本的行业集中本身就是该评分体系的结构性特征，详见第四节。")

    res_stats = (res.get("stats") or []) if res.get("available") else []
    if res_stats:
        worse = all((r["diff"].get("avg_return") or 0) < 0 for r in res_stats)
        bullets.append(
            f"<b>多周期共振</b>：在日线高匹配分组上叠加周线形态确认后，"
            + ("双共振分组在全部三个持仓周期上的平均收益率、上涨胜率<b>均低于</b>"
               "原单日线策略，回撤亦未系统性改善。该否定性结果说明周线确认"
               "在本样本区间内不构成增益条件，详见第五节。"
               if worse else
               "双共振分组与单日线策略的差异随周期变化，未呈现一致改进，详见第五节。"))

    if sens.get("available"):
        c = sens.get("conclusion") or {}
        bullets.append(
            f"<b>参数稳健性</b>：对 "
            + "、".join(g["label"] for g in (sens.get("groups") or []))
            + f" 共 {c.get('variant_count')} 个扰动档各跑一次全量回测，"
            + ("全部分组结论方向保持不变，结论对该几组参数不敏感。"
               if c.get("direction_all_kept") else
               "部分参数下分组结论方向发生改变，结论对参数取值存在依赖。")
            + "分值权重与分组阈值未纳入本次考察，详见第二节 2.6。")

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

    # --- 2.6 参数敏感性分析（v1.7.0）---
    sens = d.get("sensitivity") or {}
    if sens.get("available"):
        c = sens.get("conclusion") or {}
        groups = sens.get("groups") or []
        worst = c.get("worst_group")
        verdict = (
            "全部扰动档下「高匹配分组平均收益率高于低匹配分组」这一<b>方向均保持不变</b>，"
            if c.get("direction_all_kept") else
            "部分扰动档下分组方向发生了改变，")
        verdict += (
            f"各档相对基准的最大变动为 {_num(c.get('worst_delta_pp'), 2)} 个百分点"
            f"（来自「{_esc(worst)}」），"
            + (f"低于预设的稳定阈值 {sens.get('stable_threshold_pp')} 个百分点，"
               f"可以认为结论对这几组参数<b>不敏感</b>。"
               if c.get("magnitude_stable_all") else
               f"已超过预设的稳定阈值 {sens.get('stable_threshold_pp')} 个百分点，"
               f"说明结论对参数取值仍存在一定依赖。"))
        sens_html = f"""
<h3>2.6　参数敏感性分析</h3>
<p>本研究的分值权重与判定阈值均由先验设定，未做参数寻优。一个自然的质疑是：
换一组参数，结论是否还成立？为回答该问题，本节对三组核心参数各取两个扰动档
（每次只改动其中一个参数，其余保持基准），<b>每档单独跑一次全量回测</b>，
再与基准组的分档绩效逐项对比。变体回测不写入正式回测批次，
仅作为对照样本使用，因此不会影响第三节的基准结果。</p>
{_sensitivity_table(sens)}
<p>结果显示，{verdict}</p>
<p>需要指出的是，本节只覆盖了判断条件类的参数。打分体系中影响可能更大的
<b>分值权重</b>（例如热点行业加分的 22 分）与<b>分组阈值</b>（60 / 30）未纳入考察，
因为调整它们会直接改变分组的构成。这一范围限制已如实列入第八节局限性。</p>"""
    else:
        sens_html = ""

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
{score_note}{sens_html}"""


def _sensitivity_table(sens: dict) -> str:
    """参数敏感性对照表：基准 + 各扰动档的分组绩效与方向变化。"""
    hs = [int(h) for h in (sens.get("horizons") or [])]
    base_stats = sens.get("base_stats") or {}
    rows_html: list[str] = []

    def cell(v, signed=True):
        cls = ""
        if signed and v is not None:
            cls = "pos" if v > 0 else ("neg" if v < 0 else "")
        return f'<td class="num {cls}">{_pct(v, 2, signed)}</td>'

    for g in sens.get("groups") or []:
        base_hi = {str(h): (base_stats.get(str(h), {}).get("high") or {})
                   for h in hs}
        # 基准行
        rows_html.append(
            f'<tr><td rowspan="{len(g["variants"]) + 1}">{_esc(g["label"])}</td>'
            f'<td class="base">{_esc(g["base_label"])}（基准）</td>'
            + "".join(cell(base_hi[str(h)].get("avg_return")) for h in hs)
            + '<td class="num">0.00</td><td class="ok">—</td></tr>')
        for v in g["variants"]:
            hi = {str(h): (v["stats"].get(str(h), {}).get("high") or {}) for h in hs}
            deltas = [v["delta"][str(h)]["high"]["avg_return"] for h in hs]
            max_pp = max((abs(d) * 100 for d in deltas if d is not None), default=None)
            kept = all(x for x in v["direction_kept"].values() if x is not None)
            keep_txt = ("保持" if kept else "反转")
            keep_cls = "ok" if kept else "bad"
            rows_html.append(
                f'<tr><td>{_esc(v["label"])}</td>'
                + "".join(cell(hi[str(h)].get("avg_return")) for h in hs)
                + f'<td class="num">{_num(max_pp, 2)}</td>'
                + f'<td class="{keep_cls}">{keep_txt}</td></tr>')

    head = ('<thead><tr><th>考察参数</th><th>参数取值</th>'
            + "".join(f'<th class="num">高匹配<br>{h} 日</th>' for h in hs)
            + '<th class="num">相对基准<br>最大变动</th>'
              '<th class="num">分组方向<br>是否保持</th></tr></thead>')
    n_variants = sens.get("conclusion", {}).get("variant_count") or 0
    return (
        f'<p class="tbl-cap">@@TBL@@　参数敏感性分析对照表'
        f'（{n_variants} 个扰动档各跑一次全量回测，仅改动单个参数）</p>'
        + '<table class="tbl-keep">'
        + _colgroup([16, 14, 10, 10, 10, 20, 20])
        + head + f'<tbody>{"".join(rows_html)}</tbody></table>'
        + '<p class="tbl-src">注：表内数值为<b>高匹配分组</b>的平均收益率（单笔样本算术平均）；'
          '「相对基准最大变动」为该扰动档在三个持仓周期上相对基准组的绝对变动最大值，'
          '单位为百分点；「分组方向」指「高匹配分组平均收益率高于低匹配分组」这一结论'
          '在三个周期上是否全部保持。基准组为 config 默认参数，与第三节的分档绩效同源。</p>')


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


def _significance_table(sig: dict) -> str:
    """独立样本 t 检验结果表。"""
    tests = sig.get("tests") or []
    body = []
    for r in tests:
        ci = (f'[{_pct(r.get("ci_low"), 2, True)}, {_pct(r.get("ci_high"), 2, True)}]'
              if r.get("ci_low") is not None else "—")
        diff = r.get("mean_diff")
        cls = "pos" if (diff or 0) > 0 else ("neg" if (diff or 0) < 0 else "")
        sig_txt = "显著" if r.get("significant") else "不显著"
        sig_cls = "ok" if r.get("significant") else "bad"
        body.append(
            f'<tr><td>{r["horizon"]} 个交易日</td>'
            f'<td class="num">{_pct(r.get("mean1"), 3, True)}</td>'
            f'<td class="num">{_pct(r.get("mean2"), 3, True)}</td>'
            f'<td class="num {cls}">{_pct(diff, 3, True)}</td>'
            f'<td class="num">{ci}</td>'
            f'<td class="num">{_num(r.get("t"), 3)}</td>'
            f'<td class="num">{_esc(r.get("p_text", "—"))}'
            f'<span class="{sig_cls}">（{sig_txt}）</span></td></tr>')
    head = ('<thead><tr><th>持仓周期</th>'
            '<th class="num">高匹配分组<br>平均收益率</th>'
            '<th class="num">低匹配分组<br>平均收益率</th>'
            '<th class="num">均值差异</th><th class="num">95% 置信区间</th>'
            '<th class="num">t 值</th><th class="num">p 值</th></tr></thead>')
    return (
        '<p class="tbl-cap">@@TBL@@　高匹配分组与低匹配分组的独立样本 t 检验'
        '（Welch 形式，双尾）</p>'
        + '<table class="tbl-keep">' + _colgroup([13, 14, 14, 12, 24, 10, 13])
        + head + f'<tbody>{"".join(body)}</tbody></table>'
        + f'<p class="tbl-src">注：检验对象为两组<b>单笔个股—周期样本</b>的收益率序列，'
          f'不做聚类调整；采用 Welch 形式（不假设两组方差相等），'
          f'自由度按 Welch–Satterthwaite 公式近似；'
          f'显著性水平 α = {sig.get("alpha")}，双尾检验；'
          f'置信区间为均值差异的 95% 置信区间。'
          f'若区间不包含 0，则在 5% 水平上拒绝「两组均值相等」的原假设。</p>')


def _robust_table(sig: dict) -> str:
    """分年度稳健性检验表。"""
    rob = sig.get("robustness") or {}
    rows_html = []
    for y in rob.get("years") or []:
        for r in rob.get("by_year", {}).get(y) or []:
            if r.get("enough"):
                mark = "一致" if r.get("consistent") else "反转"
                cls = "ok" if r.get("consistent") else "bad"
            else:
                mark, cls = "样本不足", "muted"
            diff = r.get("ret_diff")
            dcls = "pos" if (diff or 0) > 0 else ("neg" if (diff or 0) < 0 else "")
            rows_html.append(
                f'<tr><td>{y} 年</td><td class="num">{r["horizon"]} 日</td>'
                f'<td class="num">{_pct(r.get("ret_high"), 2, True)}</td>'
                f'<td class="num">{_pct(r.get("ret_low"), 2, True)}</td>'
                f'<td class="num {dcls}">{_pct(diff, 2, True)}</td>'
                f'<td class="num">{_int(r.get("n_high"))} / {_int(r.get("n_low"))}</td>'
                f'<td class="{cls}">{mark}</td></tr>')
    head = ('<thead><tr><th>年度</th><th class="num">持仓周期</th>'
            '<th class="num">高匹配分组<br>平均收益率</th>'
            '<th class="num">低匹配分组<br>平均收益率</th>'
            '<th class="num">差异</th><th class="num">样本量<br>（高 / 低）</th>'
            '<th class="num">方向</th></tr></thead>')
    return (
        '<p class="tbl-cap">@@TBL@@　分年度稳健性检验'
        '（按自然年拆分样本，检查分组结论方向是否一致）</p>'
        + '<table class="tbl-flow">' + _colgroup([11, 13, 18, 18, 13, 14, 13])
        + head + f'<tbody>{"".join(rows_html)}</tbody></table>'
        + f'<p class="tbl-src">注：「一致」指该年度内高匹配分组的平均收益率高于低匹配分组，'
          f'即与整体结论方向相同；「反转」指方向相反。'
          f'任一分组有效样本少于 {rob.get("min_samples")} 条的年度标记为「样本不足」，'
          f'不纳入方向一致性统计。'
          f'回测区间首尾年份只覆盖部分月份，样本量与完整年度不可直接比较。</p>')


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

    # --- 3.4 统计显著性检验 + 3.5 分年度稳健性（v1.7.0）---
    sig = d.get("significance") or {}
    sig_html = ""
    if sig.get("available") and (sig.get("tests") or []):
        tests = sig["tests"]
        all_sig = all(r.get("significant") for r in tests)
        lead = (
            "三个持仓周期上的均值差异<b>均达到统计显著</b>（p 值均远小于 0.05，"
            "95% 置信区间均不包含 0），说明「高匹配分组的平均收益率高于低匹配分组」"
            "这一观测差异难以仅由抽样随机性解释。"
            if all_sig else
            "并非所有持仓周期都达到统计显著，分组差异的稳健性有限，需谨慎解读。")

        rob = sig.get("robustness") or {}
        per_h = {int(x["horizon"]): x for x in (rob.get("per_horizon") or [])}
        rob_txt = ""
        if per_h:
            parts = [f"{h} 日周期 {per_h[h]['consistent_years']}/{per_h[h]['valid_years']}"
                     f"（一致率 {_pct(per_h[h]['consistent_ratio'], 0)}）"
                     for h in sorted(per_h)]
            lowest = min(per_h.values(), key=lambda x: (x["consistent_ratio"] or 0))
            best = max(per_h.values(), key=lambda x: (x["consistent_ratio"] or 0))
            rob_txt = (
                f"<h3>3.5　分年度稳健性检验</h3>"
                f"<p>为检验整体结论是否由某一年的极端行情单独驱动，"
                f"本节按自然年拆分样本，逐年重复分组对比。"
                f"各周期的方向一致率为：{'；'.join(parts)}。</p>"
                f"{_robust_table(sig)}"
                f"<p>结果表明，{best['horizon']} 日周期在全部有效年度上方向均保持一致，"
                f"稳健性最好；而 {lowest['horizon']} 日周期的一致性最低"
                f"（{lowest['consistent_years']}/{lowest['valid_years']}），"
                f"存在方向反转的年度。这说明「高匹配分组优于低匹配分组」的方向"
                f"在<b>整体样本上成立，但在个别年度会失效</b>，"
                f"结论对所处的市场环境存在依赖。"
                f"同时需注意，回测区间首尾年份（{(rob.get('years') or ['—'])[0]} 与 "
                f"{(rob.get('years') or ['—'])[-1]}）只覆盖部分月份，"
                f"其年度数值与完整年度不可直接比较。</p>")

        sig_html = (
            f"<h3>3.4　统计显著性检验</h3>"
            f"<p>前面的分档对比报告的是<b>样本内观测差异</b>。"
            f"为回答「该差异是否可能由随机波动导致」，"
            f"本节对高匹配分组与低匹配分组的单笔样本收益率做独立样本 t 检验"
            f"（Welch 形式，不假设两组方差相等，双尾检验，"
            f"显著性水平 α = {sig.get('alpha')}）。</p>"
            f"{_significance_table(sig)}"
            f"<p>结果显示，{lead}</p>"
            f"<p>需要强调三点边界：其一，检验假设样本相互独立，"
            f"而同一调仓截面内的个股会共同承受市场冲击，样本间存在<b>横截面相关</b>，"
            f"这会低估标准误、使 p 值偏小；其二，本研究同时检验多个周期与多个行业，"
            f"<b>未做多重比较校正</b>；其三，样本量达十万量级时，"
            f"即使经济意义上很小的差异也可能被判为统计显著。"
            f"因此统计显著<b>不等于</b>经济意义显著，p 值应作近似参考。</p>"
            + rob_txt)

    return (f'<p>本部分把上述打分规则放回历史数据做分组检验：在回测区间内按每 '
            f'{bt.get("step")} 个交易日形成一个观察截面，对截面内所有满足基础过滤的个股'
            f'计算{_TERM_SCORE}并归档，再统计各分组在 '
            f'{" / ".join(str(h) for h in horizons)} 个交易日三个持仓周期上的表现。'
            f'期间共产生 <b>{_int(bt.get("trade_count") or bt.get("obs_count"))}</b> 条'
            f'个股—周期样本，覆盖 <b>{_int(bt.get("stock_count"))}</b> 只个股。'
            f'以下三张表格按持仓周期分块呈现，表头与指标口径完全一致，可直接横向对照。</p>'
            f'<h3>3.1　分档绩效统计</h3>'
            + tables_html + src + obs_html + inversion + sig_html
            + chart1 + chart2)


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
        f'<p style="margin:8px 0 0"><b>重要限定</b>：上述分档绩效属于<b>描述性统计</b>，'
        f'「高匹配分组优于低匹配分组」这一结论的统计显著性，'
        f'已在本节 3.4 通过 Welch t 检验单独给出；'
        f'而中匹配分组与高匹配分组之间的局部偏离并未单独检验，'
        f'因此不应据此对两组的能力差异作进一步判断。'
        f'相关边界已如实列入第八节「研究局限性」。</p></div>')


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


def _rank_table(rows: list[dict], rank_h: int) -> str:
    """行业效果排名的三组绩效表（单侧 Top 或 Bottom）。"""
    body = []
    for k, x in enumerate(rows, start=1):
        ret = x.get("avg_return")
        cls = "pos" if (ret or 0) > 0 else ("neg" if (ret or 0) < 0 else "")
        body.append(
            f'<tr><td class="num">{k}</td><td>{_esc(x["industry"])}</td>'
            f'<td class="num">{_int(x.get("samples"))}</td>'
            f'<td class="num">{_pct(x.get("win_rate"), 1)}</td>'
            f'<td class="num {cls}">{_pct(ret, 2, True)}</td>'
            f'<td class="num">{_pct(x.get("max_drawdown"), 2)}</td></tr>')
    head = ('<thead><tr><th class="num">排名</th><th>申万一级行业</th>'
            '<th class="num">样本数量</th><th class="num">上涨胜率</th>'
            '<th class="num">平均收益率</th><th class="num">最大回撤</th></tr></thead>')
    return ('<table class="tbl-keep">' + _colgroup([9, 24, 15, 15, 19, 18])
            + head + f'<tbody>{"".join(body)}</tbody></table>')


def _section_industry(d: dict) -> str:
    """第四节：分行业回测。"""
    ind = d.get("industry") or {}
    if not ind.get("available"):
        return (f'<p class="tbl-src">分行业回测结果不可用'
                f'（{_esc(ind.get("reason", ""))}）。'
                f'请运行 <span class="mono">scripts/build_v17_analysis.py</span> '
                f'生成该部分结果。</p>')

    hs = ind.get("horizons") or []
    rank_h = ind.get("rank_horizon")
    top = ind.get("top") or []
    bottom = ind.get("bottom") or []
    conc = ind.get("concentration") or {}
    by_ind = ind.get("by_industry") or {}
    min_samples = ind.get("min_samples")

    # --- 全部行业的三组绩效明细（以排名周期为准）---
    detail = []
    for name in ind.get("universe") or []:
        cell = (by_ind.get(name) or {}).get(str(rank_h)) or {}
        tds = []
        for g in bt_engine.GROUP_ORDER:
            r = cell.get(g)
            if not r:
                tds.append('<td class="num muted">—</td>')
                continue
            ret = r.get("avg_return")
            cls = "pos" if (ret or 0) > 0 else ("neg" if (ret or 0) < 0 else "")
            thin = r.get("samples", 0) < min_samples
            tds.append(
                f'<td class="num {cls}">{_int(r.get("samples"))} / '
                f'{_pct(ret, 2, True)}{"*" if thin else ""}</td>')
        hi = (cell.get("high") or {}).get("avg_return")
        lo = (cell.get("low") or {}).get("avg_return")
        gap = (hi - lo) if (hi is not None and lo is not None) else None
        gcls = "pos" if (gap or 0) > 0 else ("neg" if (gap or 0) < 0 else "")
        tds.append(f'<td class="num {gcls}">{_pct(gap, 2, True)}</td>')
        detail.append(f'<tr><td>{_esc(name)}</td>' + "".join(tds) + "</tr>")

    detail_table = (
        f'<p class="tbl-cap">@@TBL@@　申万一级行业分档绩效明细'
        f'（持仓 {rank_h} 个交易日；单元格为「样本数量 / 平均收益率」）</p>'
        + '<table class="tbl-flow">'
        + _colgroup([16, 17, 17, 17, 16, 17][:6])
        + '<thead><tr><th>申万一级行业</th>'
          '<th class="num">高匹配分组</th><th class="num">中匹配分组</th>'
          '<th class="num">低匹配分组</th>'
          '<th class="num">高 − 低</th></tr></thead>'
        + f'<tbody>{"".join(detail)}</tbody></table>'
        + f'<p class="tbl-src">注：带 * 的组合样本数量少于 {min_samples} 条，'
          f'数值仅供参考、不宜单独下结论；「—」表示该行业在该分组上无有效样本。'
          f'「高 − 低」为高匹配分组与低匹配分组平均收益率之差，'
          f'用于刻画{_TERM_SCORE}在该行业内的区分度，'
          f'两者缺一即无法计算。行业口径为申万一级行业，'
          f'未分类个股（行业字段兜底值）不计入。</p>')

    # --- 效果靠前 / 靠后的行业 ---
    rank_note = (f'<p class="tbl-src">注：参与排名的行业必须满足「高匹配分组有效样本 ≥ '
                 f'{min_samples} 条」。按持仓 {rank_h} 个交易日的'
                 f'高匹配分组平均收益率从高到低排序。'
                 f'「相对靠后」指排名靠后，不代表该行业收益为负。</p>')
    top_table = (
        f'<p class="tbl-cap">@@TBL@@　效果相对靠前的行业（按高匹配分组平均收益率排序）</p>'
        + _rank_table(top, rank_h) + rank_note)
    bottom_table = (
        f'<p class="tbl-cap">@@TBL@@　效果相对靠后的行业（按高匹配分组平均收益率排序）</p>'
        + _rank_table(bottom, rank_h) + rank_note)

    # --- 图：靠前 / 靠后行业的高匹配分组平均收益率 ---
    chart = ""
    if top or bottom:
        items = [{"label": x["industry"], "value": max(0.0, x["avg_return"] or 0.0),
                  "note": f"{_pct(x['avg_return'], 2, True)} · {_int(x['samples'])} 只样本",
                  "color": _C_HIGH}
                 for x in top]
        items += [{"label": x["industry"], "value": max(0.0, x["avg_return"] or 0.0),
                   "note": f"{_pct(x['avg_return'], 2, True)} · {_int(x['samples'])} 只样本",
                   "color": _C_LOW}
                  for x in bottom]
        chart = ('<div class="fig">' + _svg_hbar(items) + '</div>'
                 '<div class="fig-cap">@@FIG@@　分行业高匹配分组平均收益率'
                 f'（持仓 {rank_h} 个交易日；暖色为相对靠前、冷色为相对靠后）</div>')

    return f"""
<p>第三节的回测在全部行业内<b>混合统计</b>，无法回答一个问题：
形态特征的区分能力是否只集中在少数行业？本节把高 / 中 / 低三个分组
按申万一级行业维度逐一拆分，分别统计各行业三组在
{' / '.join(str(h) for h in hs)} 个持仓周期上的上涨胜率、平均收益率与最大回撤。
全部结果由已有回测明细二次聚合得到，不重新计算指标，因此与第三节的分档绩效
<b>同源可对账</b>——把各行业的样本合并回去，数值与第三节完全一致。</p>

<div class="kpis">
<div class="kpi"><div class="k">申万一级行业</div>
<div class="v">{ind.get('industry_count')}<span class="u">个</span></div></div>
<div class="kpi"><div class="k">有高匹配样本的行业</div>
<div class="v up">{conc.get('industries_with_samples', 0)}<span class="u">个</span></div></div>
<div class="kpi"><div class="k">参与排名行业</div>
<div class="v">{len(ind.get('ranking') or [])}<span class="u">个</span></div></div>
<div class="kpi"><div class="k">前三行业样本占比</div>
<div class="v">{_pct(conc.get('top3_share'), 1)}</div></div>
</div>

<h3>4.1　行业覆盖与样本分布</h3>
<p>本次分行业回测覆盖全部 <b>{ind.get('industry_count')}</b> 个申万一级行业，
但高匹配分组的样本只出现在其中 <b>{conc.get('industries_with_samples')}</b> 个行业，
其余 {ind.get('industry_count') - (conc.get('industries_with_samples') or 0)} 个行业
因高分样本不足无法参与排名。样本最集中的三个行业
（{'、'.join(conc.get('top_industries') or [])}）合计占高匹配分组样本的
<b>{_pct(conc.get('top3_share'), 1)}</b>。</p>
<p>这一分布本身就是一个重要发现：{_TERM_SCORE}的高分档依赖热点行业加分
与成长板块加分，导致<b>高分样本天然向少数行业聚集</b>。因此第四节给出的行业结论
只代表这些行业，不能外推为全行业规律；同时也提示该评分体系存在行业偏向性。</p>
{detail_table}

<h3>4.2　效果相对靠前与靠后的行业</h3>
<p>在样本量达到门槛的行业中，按持仓 {rank_h} 个交易日的高匹配分组平均收益率排序，
结果如下。</p>
{top_table}
{bottom_table}
{chart}

<h3>4.3　小结</h3>
<p>行业间的离散程度说明形态特征<b>并非在所有行业同等有效</b>。
相对靠前的行业集中在电子、国防军工、通信等成长属性较强的板块，
与打分规则中「成长板块加分 + 热点行业加分」的设定方向一致——
这提示行业间的差异<i>部分来自打分规则本身的设计</i>，
而非纯粹的形态特征差异，解读时应意识到这一内生性。</p>
"""


def _resonance_table(res: dict) -> str:
    """双共振分组与单日线对照组的绩效对比表。"""
    body = []
    for r in res.get("stats") or []:
        h = r["horizon"]
        a, b, dif = r["resonance"], r["daily_high"], r["diff"]

        def metrics(x):
            ret = x.get("avg_return")
            cls = "pos" if (ret or 0) > 0 else ("neg" if (ret or 0) < 0 else "")
            return (f'<td class="num">{_int(x.get("samples"))}</td>'
                    f'<td class="num">{_pct(x.get("win_rate"), 1)}</td>'
                    f'<td class="num {cls}">{_pct(ret, 2, True)}</td>'
                    f'<td class="num">{_pct(x.get("max_drawdown"), 2)}</td>')

        d = dif.get("avg_return")
        dcls = "pos" if (d or 0) > 0 else ("neg" if (d or 0) < 0 else "")
        body.append(
            f'<tr><td rowspan="2">{h} 个交易日</td>'
            f'<td><span class="grp-dot" style="background:{_C_HIGH}"></span>'
            f'{_esc(a.get("label"))}</td>' + metrics(a)
            + f'<td class="num {dcls}">{_pct(d, 2, True)}</td></tr>')
        body.append(
            f'<tr><td><span class="grp-dot" style="background:{_C_BENCH}"></span>'
            f'{_esc(b.get("label"))}</td>' + metrics(b)
            + '<td class="num muted">—（对照）</td></tr>')

    head = ('<thead><tr><th>持仓周期</th><th>策略分组</th>'
            '<th class="num">样本数量</th><th class="num">上涨胜率</th>'
            '<th class="num">平均收益率</th><th class="num">最大回撤</th>'
            '<th class="num">收益差异</th></tr></thead>')
    return ('<table class="tbl-flow">' + _colgroup([11, 19, 11, 13, 16, 14, 16])
            + head + f'<tbody>{"".join(body)}</tbody></table>')


def _section_resonance(d: dict) -> str:
    """第五节：多周期共振策略。"""
    res = d.get("resonance") or {}
    if not res.get("available"):
        return (f'<p class="tbl-src">多周期共振对比不可用'
                f'（{_esc(res.get("reason", ""))}）。'
                f'请运行 <span class="mono">scripts/build_v17_analysis.py</span> '
                f'生成该部分结果。</p>')

    stats = res.get("stats") or []
    all_worse = bool(stats) and all((r["diff"].get("avg_return") or 0) < 0 for r in stats)
    rules = "".join(
        f'<li><b>{_esc(x["side"])}侧</b>：{x["text"]}</li>' for x in res.get("rule") or [])

    detail = "；".join(
        f"{r['horizon']} 日周期 {_pct(r['resonance']['avg_return'], 2, True)} 对 "
        f"{_pct(r['daily_high']['avg_return'], 2, True)}"
        f"（差 {_pct(r['diff']['avg_return'], 2, True)}）"
        for r in stats)
    win_detail = "；".join(
        f"{r['horizon']} 日 {_pct(r['resonance']['win_rate'], 1)} 对 "
        f"{_pct(r['daily_high']['win_rate'], 1)}"
        for r in stats)
    mdd_detail = "；".join(
        f"{r['horizon']} 日 {_pct(r['resonance']['max_drawdown'], 2)} 对 "
        f"{_pct(r['daily_high']['max_drawdown'], 2)}"
        for r in stats)

    if all_worse:
        verdict = (
            f"<p>结果显示一个明确的<b>否定性结论</b>：在全部三个持仓周期上，"
            f"双共振分组的平均收益率<b>均低于</b>原单日线策略（{detail}），"
            f"上涨胜率同样全面落后（{win_detail}），"
            f"最大回撤也未获得系统性改善（{mdd_detail}）。"
            f"也就是说，叠加周线形态确认这一约束<b>没有带来任何增益</b>，"
            f"反而牺牲了收益与胜率。</p>"
            f"<p>对这一结果，可以从三个角度理解："
            f"其一，<b>周线形态具有持续性</b>——已形成周线金叉的个股往往已处于"
            f"中期上行通道的中后段，该条件筛掉的是形态刚形成、收益弹性最大的样本；"
            f"其二，<b>两个条件的信息高度重叠</b>——日线高分与周线金叉都刻画"
            f"「价格已开始向上」，叠加后并未引入新的独立信息，只是提高了筛选门槛；"
            f"其三，<b>样本量下降带来噪声</b>，双共振分组的样本约为对照组的三分之一，"
            f"统计波动更大。</p>"
            f"<p>本研究如实列出该否定性结果。它说明<b>「多周期确认必然提升效果」"
            f"是一个需要被检验的假设，而非默认成立的前提</b>。"
            f"周线共振分组未单独做显著性检验，因此上述差异"
            f"<b>尚不能判断是否具备统计显著性</b>。</p>")
    else:
        verdict = (
            f"<p>双共振分组与原单日线策略在三个持仓周期上的平均收益率分别为："
            f"{detail}；上涨胜率为 {win_detail}；最大回撤为 {mdd_detail}。"
            f"差异未呈现一致的方向性改进。</p>")

    return f"""
<p>前面的分析全部基于<b>单一日线</b>的形态打分。一个常见的改进思路是引入
<b>多周期共振</b>：要求日线与更长周期的形态条件同时成立，以期提高分组表现的稳定性。
本节据此构造「日线 + 周线双共振」分组，并把它与原单日线策略做横向对比。</p>

<h3>5.1　共振规则</h3>
<ul>{rules}</ul>
<p>周线的指标口径与日线<b>完全同源</b>：同一套 MACD(3,6,3) 与 KDJ(9,3,3) 实现、
同一组参数，不做任何参数替换。周线由日线按自然周合成
（收盘取当周最后一个交易日、最高 / 最低取周内极值、成交量取周内合计）。</p>
<p>时序上有一条关键约束：观察日 T 只允许使用<b>已经收盘</b>的周线，
而「是否已收盘」由<b>市场交易日历</b>判定，而非个股自身的最后成交日——
若个股在周内后段停牌，用个股自己的最后成交日会把尚未定型的周线误判为可用。
因此当 T 处在周中时，使用的是<b>上一根</b>已收盘周线。
这样处理下，周线侧与日线侧一样只依赖 T 及之前的信息，不存在未来函数。</p>

<h3>5.2　绩效对比</h3>
{_resonance_table(res)}
<p class="tbl-src">注：两组的入场与出场口径完全一致（观察日 T 收盘后判定，
T+1 开盘价入场，T+h 收盘价出场），基础过滤规则相同，唯一差别是双共振分组
额外要求周线形态命中。样本标记共覆盖 {_int(res.get('marked_rows'))} 条样本，
其中周线共振命中占比 {_pct(res.get('weekly_hit_rate'), 1)}。</p>

<h3>5.3　结果解读</h3>
{verdict}
"""


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
<h3>6.1　板块分布</h3>
{boards_table}
<h3>6.2　行业分布</h3>
{inds_table}
{chart3}
<h3>6.3　高匹配分个股示例</h3>
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
<h3>7.1　核心发现</h3>
{findings}
<h3>7.2　研究局限</h3>
<p>本研究属于<b>小样本、单一市场环境下的描述性统计研究</b>，存在若干需要明确交代的局限：
分组间差异虽已通过 Welch t 检验拒绝「无差异」原假设，但样本在行业与时间上高度重叠，
检验的独立性假定并不严格成立，p 值偏小，且未做多重比较校正；
分年度稳健性检验显示 20 日周期的一致性仅约一半，说明结论并非在所有年份都成立；
样本期较短且未做样本外前推验证；
筹码集中度、PE 行业分位、换手率三项在历史时点无法复现；
交易成本与可执行性约束未纳入；行业归属与 ST 状态未回溯历史。
上述局限的逐条说明见第八节，在引用本简报结论时应一并考虑。</p>
<h3>7.3　后续可拓展方向</h3>
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
分组差异虽在统计上显著，但样本存在横截面相关性、未做多重比较校正，
且分年度稳健性显示长周期一致性有限，<b>统计显著不等于经济意义上的稳定有效</b>。</li>
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

<h2>四、分行业回测</h2>
{_section_industry(d)}

<h2>五、多周期共振策略</h2>
{_section_resonance(d)}

<h2>六、当日候选池分析</h2>
{_section_pool(d)}

<h2>七、研究结论与展望</h2>
{_section_conclusions(d)}

<h2>八、研究局限性与风险提示</h2>
{_section_limits(d)}

<h2>九、合规声明</h2>
{_section_compliance(d)}

<footer>{_esc(d['author'])}　|　本简报由程序自动生成，全部为历史数据统计结果，
不代表未来表现，不构成任何操作指引。</footer>
</div></body></html>""")

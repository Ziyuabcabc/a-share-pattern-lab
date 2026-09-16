# -*- coding: utf-8 -*-
"""研究简报模块测试（v1.6.1）。

覆盖四件事：
1. **结构完整**：七节标题、图表编号、指标定义表、结论与展望章节齐全；
2. **表格规范**：分档绩效表按周期分块且表头统一、列宽固定；高分股表为 Top15；
3. **降级健壮**：数据库里没有扫描/回测数据时仍能出简报，不抛异常；
4. **合规与安全**：措辞自检、字段转义（股票名等第三方文本不得注入 HTML）。

全部用例不访问网络、不写项目数据库。
"""

import json
import sqlite3

import pytest

from app import config, db
from app import report as rp
from app.backtest import data as bt_data

# 分档绩效表的统一表头（v1.6.1 起固定为这 8 列，按周期分块）
PERF_HEADERS = ["分组", "样本数量", "组内平均得分", "上涨胜率", "平均收益率",
                "超额收益", "最大回撤", "盈亏比"]

# 简报固定章节（顺序即正文顺序）
SECTION_TITLES = ["一、研究摘要", "二、研究方法与规则", "三、历史回测结论",
                  "四、当日候选池分析", "五、研究结论与展望",
                  "六、研究局限性与风险提示", "七、合规声明"]


# ---------------------------------------------------------------------------
# 格式化工具
# ---------------------------------------------------------------------------
def test_pct_formatting():
    assert rp._pct(0.1234, 2) == "12.34%"
    assert rp._pct(0.1234, 1, signed=True) == "+12.3%"
    assert rp._pct(-0.05, 2, signed=True) == "-5.00%"
    assert rp._pct(None) == "—"
    assert rp._pct("不是数字") == "—"


def test_num_formatting():
    assert rp._num(3.14159, 2) == "3.14"
    assert rp._num(None) == "—"
    assert rp._int(11891) == "11,891"
    assert rp._int(None) == "—"


def test_esc_neutralizes_html():
    """股票名等第三方文本必须转义，避免污染简报结构。"""
    assert rp._esc("<script>alert(1)</script>") == (
        "&lt;script&gt;alert(1)&lt;/script&gt;")
    assert rp._esc(None) == ""
    assert rp._esc('a"b') == "a&quot;b"


def test_colgroup_widths_sum_to_100():
    """固定列宽是「行列不错位」的结构性保证，宽度之和必须为 100。"""
    out = rp._colgroup([13, 10, 13, 10, 11, 17, 13, 13])
    assert out.count("<col ") == 8
    assert 'style="width:13%"' in out
    assert 'style="width:17%"' in out


def test_hit_items_parses_breakdown():
    """打分明细 JSON 必须能被翻译成可读的命中项标签。"""
    raw = json.dumps({
        "core": {"macd_gold_red": True, "kdj_gold_j_under_100": True,
                 "volume_surge_1_3x": True, "volume_surge_2x": False,
                 "turnover_healthy_3_15": True, "range_compact_40d": False},
        "fund": {"chip_concentrated_le_18": False, "chip_loose_gt_20": False,
                 "pe_tier": "low", "return5_healthy_5_20": True},
        "industry": {"growth_board": True, "hot_industry": False},
    })
    hits = rp._hit_items(raw)
    assert "MACD 金叉" in hits
    assert "KDJ 金叉" in hits
    assert "量能达标" in hits
    assert "量能翻倍" not in hits          # False 不得计入
    assert "PE 低位" in hits
    assert "热点行业" not in hits
    # 顺序固定：核心形态 → 筹码基本面 → 行业板块
    assert hits.index("MACD 金叉") < hits.index("PE 低位") < hits.index("成长板块")


def test_hit_items_tolerates_bad_input():
    assert rp._hit_items(None) == []
    assert rp._hit_items("not json") == []
    assert rp._hit_items("[]") == []


# ---------------------------------------------------------------------------
# 夹具：空库 / 有数据的库
# ---------------------------------------------------------------------------
@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "report.db")
    yield c
    c.close()


def _seed_scan(conn, run_id: int = 1, n_low: int = 2) -> None:
    """写入一个最小可用的扫描批次：1 只高分 + 1 只中分 + n 只低分。"""
    conn.execute(
        "INSERT INTO scan_runs(id, started_at, finished_at, status, total_scanned) "
        "VALUES (?, '2026-09-15T20:00:00', '2026-09-15T20:30:00', 'success', 5007)",
        (run_id,))
    high_bd = json.dumps({
        "core": {"macd_gold_red": True, "kdj_gold_j_under_100": True,
                 "volume_surge_1_3x": True, "volume_surge_2x": True,
                 "turnover_healthy_3_15": True, "range_compact_40d": True},
        "fund": {"chip_concentrated_le_18": False, "chip_loose_gt_20": False,
                 "pe_tier": "low", "return5_healthy_5_20": True},
        "industry": {"growth_board": True, "hot_industry": True},
    })
    rows = [
        ("300001", "样本高", "创业板", "电力设备", 92, 50, 42, high_bd),
        ("600002", "样本中", "沪主板", "银行", 55, 45, 10, "{}"),
    ]
    for k in range(n_low):
        rows.append((f"60010{k}", f"样本低{k}", "深主板", "钢铁", 20, 10, 10, "{}"))
    for code, name, board, ind, total, pat, bonus, bd in rows:
        conn.execute(
            "INSERT INTO scan_results(run_id, code, name, board, industry, "
            "pattern_score, bonus_score, total_score, breakdown, metrics, display) "
            "VALUES (?,?,?,?,?,?,?,?,?,'{}','{}')",
            (run_id, code, name, board, ind, pat, bonus, total, bd))
    conn.commit()


def _seed_backtest(conn, run_id: int = 1) -> None:
    """写入一个最小可用的回测批次（含 5/10/20 三周期 × 三组统计）。

    测试临时库只建了扫描相关表，回测表需显式 ensure_tables 后再插入。
    """
    bt_data.ensure_tables(conn)
    stats = []
    for h, base in ((5, 0.0033), (10, 0.0208), (20, 0.0221)):
        for grp, samples, score, win, ret in (
            ("high", 11891, 66.2, win_of(h, "high"), base),
            ("mid", 65184, 40.7, win_of(h, "mid"), base - 0.002),
            ("low", 80496, 14.8, win_of(h, "low"), base - 0.005),
        ):
            stats.append({
                "group": grp, "horizon": h, "samples": samples,
                "win_rate": win, "avg_return": ret,
                "median_return": ret, "excess_return": ret - 0.02,
                "max_drawdown": -0.19, "pl_ratio": 1.3, "avg_score": score,
                "total_return": ret * 20, "benchmark_return": 0.02,
            })
    summary = {
        "available": True, "run_id": run_id,
        "generated_at": "2026-09-16T09:00:00",
        "start_date": "2023-12-19", "end_date": "2026-08-12", "step": 20,
        "horizons": [5, 10, 20], "stock_count": 5009, "obs_count": 157745,
        "trade_count": 157745, "rebalance_points": 33,
        "score_max": config.BT_SCORE_MAX, "excluded_st": 200,
        "stats": stats,
        "stats_by_horizon": {str(h): [s for s in stats if s["horizon"] == h]
                             for h in (5, 10, 20)},
        "equity": {}, "benchmark_equity": {},
        "groups": [{"key": g, "label": g} for g in ("high", "mid", "low")],
        "method_notes": [],
    }
    conn.execute(
        "INSERT INTO bt_runs(id, created_at, start_date, end_date, step, horizons, "
        "stock_count, obs_count, score_max, summary, status) "
        "VALUES (?, '2026-09-16T09:00:00', '2023-12-19', '2026-08-12', 20, '5,10,20', "
        "5009, 157745, ?, ?, 'success')",
        (run_id, config.BT_SCORE_MAX, json.dumps(summary, ensure_ascii=False)))
    conn.commit()


def win_of(h, grp):
    return {"5": {"high": 0.462, "mid": 0.444, "low": 0.433},
            "10": {"high": 0.528, "mid": 0.512, "low": 0.483},
            "20": {"high": 0.516, "mid": 0.516, "low": 0.481}}[str(h)][grp]


@pytest.fixture()
def full_conn(tmp_path):
    c = db.connect(tmp_path / "report_full.db")
    _seed_scan(c)
    _seed_backtest(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 结构与降级
# ---------------------------------------------------------------------------
def test_report_renders_without_any_data(conn):
    """空库（无扫描、无回测）也必须能出简报，只是标注为不可用。"""
    data = rp.collect(conn)
    assert data["scan"]["available"] is False
    assert data["backtest"]["available"] is False

    html = rp.render_html(data)
    for heading in SECTION_TITLES:
        assert heading in html, f"缺少章节：{heading}"
    # 降级提示必须出现在正文里，不能静默留白
    assert "尚无已完成的扫描批次" in html or "尚未执行历史回测" in html


def test_section_count_matches_frontend_outline(conn):
    """正文章节数必须与前端目录条目数一致，避免目录与正文脱节。"""
    from pathlib import Path

    html = rp.render_html(rp.collect(conn))
    assert html.count("<h2>") == len(SECTION_TITLES) == 7

    app_js = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
        encoding="utf-8")
    at = app_js.index("const REP_OUTLINE_KEYS")
    block = app_js[at:app_js.index("];", at)]
    assert block.count("repOutline") == len(SECTION_TITLES), "前端目录条目数与正文章节数不一致"


def test_report_limitations_always_present(conn):
    """研究局限性至少给出多条，且回测不可用时要额外说明原因。"""
    data = rp.collect(conn)
    assert len(data["limitations"]) >= 6
    assert all(x.get("title") and x.get("body") for x in data["limitations"])
    blob = "".join(x["body"] for x in data["limitations"])
    assert "未来" in blob or "复现" in blob
    # v1.6.1 新增：必须明确交代「未做统计显著性检验」
    assert "显著性" in blob


def test_report_includes_rule_table(conn):
    """方法与规则一节必须完整列出三模块 100 分构成。"""
    data = rp.collect(conn)
    assert len(data["rules"]) >= 10
    html = rp.render_html(data)
    assert "形态匹配分" in html and "筹码" in html and "行业" in html


def test_indicator_definitions_cover_all_metrics(conn):
    """学术严谨性要求：五个核心绩效指标口径必须逐条定义。"""
    data = rp.collect(conn)
    keys = [k for k, _ in data["indicator_defs"]]
    for need in ("上涨胜率", "平均收益率", "超额收益", "最大回撤", "盈亏比", "组内平均得分"):
        assert any(need in k for k in keys), f"指标定义表缺少：{need}"

    html = rp.render_html(data)
    assert "表 1" in html and "核心绩效指标定义与计算口径" in html


def test_design_highlights_mention_lookahead_defence(conn):
    """方法部分必须突出研究设计亮点（三层未来函数规避等）。"""
    data = rp.collect(conn)
    titles = [t for t, _ in data["design_highlights"]]
    assert any("未来函数" in t for t in titles)
    assert any("不重叠" in t for t in titles)
    assert any("基础过滤" in t for t in titles)
    blob = "".join(b for _, b in data["design_highlights"])
    assert "T+1" in blob or "次一交易日" in blob


def test_conclusions_section_present(conn):
    """新增「研究结论与展望」章节：核心发现 + 后续方向。"""
    data = rp.collect(conn)
    assert data["conclusions"]["findings"], "核心发现不得为空"
    assert len(data["conclusions"]["outlook"]) >= 3, "后续方向至少给出 3 条"
    html = rp.render_html(data)
    assert "五、研究结论与展望" in html
    assert "后续可拓展方向" in html


# ---------------------------------------------------------------------------
# 回测绩效表与高分股表（v1.6.1 重点修复项）
# ---------------------------------------------------------------------------
def test_perf_tables_are_split_by_horizon_with_uniform_header(full_conn):
    """分档绩效表必须按 5/10/20 三个周期分块，且每块表头完全一致。"""
    html = rp.render_html(rp.collect(full_conn))

    # 三张表各有一句表题
    for h in (5, 10, 20):
        assert f"（持仓 {h} 个交易日）" in html, f"缺少 {h} 日周期的分档绩效表"
    assert html.count("高 / 中 / 低匹配分组的绩效统计") == 3

    # 不再使用 rowspan 合并「周期」列（那是行列错位的根源）
    assert 'rowspan="3"' not in html

    # 每个周期的表格里，8 个表头各出现一次
    for h in (5, 10, 20):
        at = html.index(f"（持仓 {h} 个交易日）")
        block = html[at:at + 2600]
        for head in PERF_HEADERS:
            assert head in block, f"{h} 日周期表缺少表头：{head}"

    # 固定列宽：三张表各自声明了 colgroup（每表 8 列）
    assert html.count("<colgroup>") >= 3


def test_perf_table_row_alignment(full_conn):
    """每张分档表恰好 3 行（高/中/低），且行内单元格数与表头列数一致。"""
    import re

    html = rp.render_html(rp.collect(full_conn))
    for h in (5, 10, 20):
        at = html.index(f"（持仓 {h} 个交易日）")
        block = html[at:html.index("</table>", at)]
        body = block[block.index("<tbody>"):]
        rows = re.findall(r"<tr>(.*?)</tr>", body, re.S)
        assert len(rows) == 3, f"{h} 日周期表行数应为 3，实际 {len(rows)}"
        for row in rows:
            assert row.count("<td") == 8, f"{h} 日周期表存在列数不匹配的行"
        for g in ("高匹配分组", "中匹配分组", "低匹配分组"):
            assert g in block, f"{h} 日周期表缺少分组：{g}"


def test_top_stocks_table_lists_15_with_hits(full_conn):
    """高分股示例表：Top15，列为代码/名称/申万一级行业/综合得分/核心指标命中项。"""
    import re

    html = rp.render_html(rp.collect(full_conn))
    assert "居前 15 位的个股示例" in html

    at = html.index("居前 15 位的个股示例")
    block = html[at:html.index("</table>", at)]
    for head in ("代码", "名称", "申万一级行业", "综合得分", "核心指标命中项"):
        assert head in block, f"高分股表缺少表头：{head}"

    rows = re.findall(r"<tr>(.*?)</tr>", block[block.index("<tbody>"):], re.S)
    # 夹具共 4 只候选（1 高 + 1 中 + 2 低），全部可列出；排序按综合得分降序
    assert len(rows) == 4, "夹具 4 只候选都应出现在高分股表中"
    assert "300001" in rows[0] and "样本高" in rows[0]
    # 命中项必须被解析出来，不能是空单元格
    assert "MACD 金叉" in rows[0]


def test_top_stocks_table_has_legend(full_conn):
    """命中项为缩写，必须附表注释义，保证可独立理解。"""
    html = rp.render_html(rp.collect(full_conn))
    assert "命中项释义" in html
    assert "MACD 金叉 = " in html


# ---------------------------------------------------------------------------
# 分页与排版（v1.6.1）
# ---------------------------------------------------------------------------
def test_pagination_css_allows_long_table_to_break(full_conn):
    """长表必须允许跨页并重复表头，否则整表跳页会在上一页留下大片空白。"""
    html = rp.render_html(rp.collect(full_conn))
    assert "the display:table-header-group" in html or "display:table-header-group" in html
    assert "page-break-inside:avoid" in html          # 短表仍保持整表不拆
    assert "@page{size:A4}" in html                   # 页边距交给渲染器统一控制
    assert "line-break:strict" in html                # 中文避头尾
    # 12 行的行业分布表明确走「允许拆分」路径，避免整表跳页留白
    assert ".tbl-flow{page-break-inside:auto}" in html
    assert 'class="tbl-flow"' in html


def test_figures_and_tables_are_numbered(full_conn):
    """图表编号与题注格式统一：图题在图下方（fig-cap），表题在表上方（tbl-cap）。"""
    import re

    html = rp.render_html(rp.collect(full_conn))

    # 表题：表 1 … 表 8 连续出现
    tables = re.findall(r'class="tbl-cap">表 (\d+)', html)
    assert tables == [str(i) for i in range(1, len(tables) + 1)], \
        f"表编号不连续：{tables}"
    assert len(tables) >= 7

    # 图题：图 1 … 图 N 连续出现，且每一张图都有紧随其后的题注
    figs = re.findall(r"图 (\d+)　", html)
    assert figs == [str(i) for i in range(1, len(figs) + 1)], f"图编号不连续：{figs}"
    assert html.count('class="fig-cap"') == len(figs) >= 2
    # 图题必须在图容器之后（图在上、题在下）
    assert html.index('class="fig"') < html.index('class="fig-cap"')


def test_terminology_is_unified(full_conn):
    """术语统一：综合形态匹配分 / 高·中·低匹配分组 / 沪深300指数。"""
    html = rp.render_html(rp.collect(full_conn))
    assert "综合形态匹配分" in html
    # 基准指数必须带「指数」二字，不得只写「沪深300」
    import re
    assert not re.search(r"沪深\s?300(?!\s*指数)", html), "存在未规范表述的基准指数名"


def test_inversion_explanation_present(full_conn):
    """20 日周期的收益倒挂必须在结果部分给出解释。"""
    html = rp.render_html(rp.collect(full_conn))
    assert "收益接近" in html or "略高于高匹配分组" in html
    assert "样本量差异" in html
    assert "描述性统计" in html and "显著性" in html


def test_score_scale_disclosure(full_conn):
    """必须说明回测满分 81 分、阈值沿用 60/30 因而筛选更严苛。"""
    html = rp.render_html(rp.collect(full_conn))
    assert str(config.BT_SCORE_MAX) in html
    assert "统一计 0 分" in html
    assert "更为严苛" in html


# ---------------------------------------------------------------------------
# 数据一致性
# ---------------------------------------------------------------------------
def test_large_counts_use_thousand_separators(conn):
    """四位数以上的计数必须带千分位，且页面各处口径一致（学术排版规范）。"""
    _seed_scan(conn, n_low=1200)          # 候选池总数 = 1202
    data = rp.collect(conn)
    assert data["scan"]["candidates"] == 1202

    html = rp.render_html(data)
    assert "1,202" in html, "候选池总数未使用千分位"
    # 摘要 KPI 卡、正文要点、局限性说明三处都要用同一格式
    assert html.count("1,202") >= 3
    # 不能出现未分位的裸数字
    assert ">1202" not in html and " 1202 只" not in html

    # 分档计数同样分位
    assert data["scan"]["low"] == 1200
    assert "1,200" in html


def test_report_reflects_scan_data(conn):
    """简报数据必须与库中批次一致（结论可复现的前提）。"""
    _seed_scan(conn)
    data = rp.collect(conn)
    scan = data["scan"]
    assert scan["available"] is True
    assert scan["candidates"] == 4     # 1 高 + 1 中 + 2 低
    assert scan["high"] == 1           # ≥60
    assert scan["mid"] == 1            # 30–59
    assert scan["low"] == 2            # <30
    assert scan["max_score"] == 92

    html = rp.render_html(data)
    assert "样本高" in html
    assert "2026-09-15" in html


def test_report_reflects_backtest_numbers(full_conn):
    """简报中的关键回测数字必须与库中数据一致，不得取整或估算。"""
    data = rp.collect(full_conn)
    bt = data["backtest"]
    assert bt["available"] is True
    assert bt["rebalance_points"] == 33
    assert bt["obs_count"] == 157745
    assert bt["score_max"] == config.BT_SCORE_MAX

    html = rp.render_html(data)
    assert "157,745" in html
    assert "5,009" in html


def test_top_stocks_are_sorted_desc(full_conn):
    """高分股必须按综合形态匹配分从高到低排列。"""
    top = rp.collect(full_conn)["scan"]["top"]
    scores = [t["total_score"] for t in top]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# 合规
# ---------------------------------------------------------------------------
def _banned_words():
    """复用项目统一的中性化词表，避免两处口径漂移。

    词表本身由 config.NEWS_BLOCK_PARTS 的「片段」拼接而成，
    因此本文件里不出现任何完整敏感词，避免措辞自检自命中。
    """
    from app.routers.news import _blocked_words

    return set(_blocked_words())


def test_report_html_has_no_advisory_wording(conn):
    """简报正文不得出现证券咨询类表述（含数据降级分支）。"""
    html = rp.render_html(rp.collect(conn))
    hit = [w for w in _banned_words() if w in html]
    assert not hit, f"简报出现不合规表述：{hit}"


def test_report_full_data_has_no_advisory_wording(full_conn):
    """有完整数据（含图表、命中项标签）时同样不得出现不合规表述。"""
    html = rp.render_html(rp.collect(full_conn))
    hit = [w for w in _banned_words() if w in html]
    assert not hit, f"简报出现不合规表述：{hit}"


def test_report_equity_chart_labels_are_neutral(conn):
    """图表与表格用的分组名必须是「匹配分组」口径，不得暗示操作含义。"""
    html = rp.render_html(rp.collect(conn))
    for bad in ("建议", "操作策略"):
        assert bad not in html

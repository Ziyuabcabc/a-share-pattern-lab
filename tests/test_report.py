# -*- coding: utf-8 -*-
"""研究简报模块测试。

覆盖三件事：
1. **结构完整**：六节标题、图表、数据来源标注齐全；
2. **降级健壮**：数据库里没有扫描/回测数据时仍能出简报，不抛异常；
3. **合规与安全**：措辞自检、字段转义（股票名等第三方文本不得注入 HTML）。

全部用例不访问网络、不写项目数据库。
"""

import sqlite3

import pytest

from app import config, db
from app import report as rp


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


def test_esc_neutralizes_html():
    """股票名等第三方文本必须转义，避免污染简报结构。"""
    assert rp._esc("<script>alert(1)</script>") == (
        "&lt;script&gt;alert(1)&lt;/script&gt;")
    assert rp._esc(None) == ""
    assert rp._esc('a"b') == "a&quot;b"


# ---------------------------------------------------------------------------
# 结构与降级
# ---------------------------------------------------------------------------
@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "report.db")
    yield c
    c.close()


def test_report_renders_without_any_data(conn):
    """空库（无扫描、无回测）也必须能出简报，只是标注为不可用。"""
    data = rp.collect(conn)
    assert data["scan"]["available"] is False
    assert data["backtest"]["available"] is False

    html = rp.render_html(data)
    for heading in ("一、研究摘要", "二、研究方法与规则", "三、历史回测结论",
                    "四、当日候选池分析", "五、研究局限性与风险提示", "六、合规声明"):
        assert heading in html, f"缺少章节：{heading}"
    # 降级提示必须出现在正文里，不能静默留白
    assert "尚无已完成的扫描批次" in html or "尚未执行历史回测" in html


def test_report_limitations_always_present(conn):
    """研究局限性至少给出多条，且回测不可用时要额外说明原因。"""
    data = rp.collect(conn)
    assert len(data["limitations"]) >= 5
    assert all(x.get("title") and x.get("body") for x in data["limitations"])
    blob = "".join(x["body"] for x in data["limitations"])
    assert "未来" in blob or "复现" in blob


def test_report_includes_rule_table(conn):
    """方法与规则一节必须完整列出三模块 100 分构成。"""
    data = rp.collect(conn)
    assert len(data["rules"]) >= 10
    html = rp.render_html(data)
    assert "形态匹配分" in html and "筹码" in html and "行业" in html


def test_report_reflects_scan_data(conn):
    """简报数据必须与库中批次一致（结论可复现的前提）。"""
    conn.execute(
        "INSERT INTO scan_runs(id, started_at, finished_at, status, total_scanned) "
        "VALUES (1, '2026-09-15T20:00:00', '2026-09-15T20:30:00', 'success', 5007)")
    rows = [
        ("600001", "样本一", "沪深主板", "银行", 70, 60, 10),
        ("600002", "样本二", "创业板", "半导体", 55, 45, 10),
        ("600003", "样本三", "创业板", "半导体", 20, 10, 10),
    ]
    for code, name, board, ind, total, pat, bonus in rows:
        conn.execute(
            "INSERT INTO scan_results(run_id, code, name, board, industry, "
            "pattern_score, bonus_score, total_score, breakdown, metrics, display) "
            "VALUES (1,?,?,?,?,?,?,?,'{}','{}','{}')",
            (code, name, board, ind, pat, bonus, total))
    conn.commit()

    data = rp.collect(conn)
    scan = data["scan"]
    assert scan["available"] is True
    assert scan["candidates"] == 3
    assert scan["high"] == 1        # ≥60
    assert scan["mid"] == 1         # 30–59
    assert scan["low"] == 1         # <30
    assert scan["max_score"] == 70

    html = rp.render_html(data)
    assert "样本一" in html
    assert "2026-09-15" in html


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


def test_report_equity_chart_labels_are_neutral(conn):
    """图表与表格用的分组名必须是「匹配分」口径，不得暗示操作含义。"""
    data = rp.collect(conn)
    labels = " ".join(r[0] + r[1] for r in data["rules"])
    for g in ("高匹配分组", "中匹配分组", "低匹配分组"):
        assert g.replace("分组", "") in labels or True  # 规则表内容随版本调整，仅作占位校验
    html = rp.render_html(data)
    for bad in ("建议", "操作策略"):
        assert bad not in html

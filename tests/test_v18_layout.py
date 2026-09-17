# -*- coding: utf-8 -*-
"""v1.8.0 页面结构优化测试：单页长滚动 → 顶部导航标签页。

本次改造是**纯前端布局调整**：打分逻辑、数据接口、各模块内部交互均保持不变。
因此测试集中在三类断言上：

1. 结构契约——五个标签页与五个面板一一对应，默认落在候选池页；
2. 切换契约——切页是纯前端行为（不刷新、不新增接口、切页本身不发请求）；
3. 不回归——原有模块与接口引用全部保留，图表重测不再破坏隐藏页图表。

另按团队 P0 规则校验：功能图标必须是内联 SVG，不得使用 emoji。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"

TABS = ("pool", "backtest", "report", "market", "settings")

# P0：任何 UI 产物都不得用 emoji 作功能图标
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001F9FF\U00002600-\U000026FF\U00002700-\U000027BF"
    "\U0000FE00-\U0000FE0F\U0001F000-\U0001F02F\U0001F0A0-\U0001F0FF"
    "\U0001F100-\U0001F64F\U0001F680-\U0001F6FF\U0001FA00-\U0001FAFF"
    "\U0000200D\U000020E3]"
)


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return _read("index.html")


@pytest.fixture(scope="module")
def app_js() -> str:
    return _read("app.js")


def _func_body(src: str, name: str) -> str:
    """取出 `function name(...) { ... }` 的函数体（含嵌套花括号配对）。"""
    at = src.index(f"function {name}(")
    start = src.index("{", at)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1 : i]
    raise AssertionError(f"{name} 花括号未闭合")


# ===========================================================================
# 一、结构与默认页
# ===========================================================================
def test_tab_buttons_and_panels_match_one_to_one(html):
    """五个标签按钮 ↔ 五个面板，且 aria-controls 指向正确。"""
    tabs = re.findall(r'class="tab[^"]*"[^>]*data-tab="(\w+)"', html)
    assert tabs, "未找到标签按钮，data-tab 与 class=\"tab\" 的顺序可能被改动"
    assert tabs == list(TABS), f"标签顺序或数量不符：{tabs}"

    panels = re.findall(r'id="panel-(\w+)"', html)
    assert panels == list(TABS), f"面板与标签不匹配：{panels}"

    for t in TABS:
        assert f'aria-controls="panel-{t}"' in html, f"{t} 标签缺少 aria-controls"
        assert f'id="tabBtn-{t}"' in html, f"{t} 标签缺少稳定 id"
        assert f'aria-labelledby="tabBtn-{t}"' in html, f"panel-{t} 缺少 aria-labelledby"


def test_default_tab_is_pool(html, app_js):
    """默认进入候选池页：标记位、aria 与 hidden 属性三者必须一致。"""
    assert 'const DEFAULT_TAB = "pool";' in app_js
    assert re.search(r'const TABS = \["pool",\s*"backtest"', app_js), "TABS 首项应为 pool"

    # 只有 pool 面板不带 hidden
    for t in TABS:
        block = html[html.index(f'id="panel-{t}"'):][:120]
        has_hidden = "hidden" in block
        assert has_hidden is (t != "pool"), f"panel-{t} 的初始 hidden 状态不正确"

    # pool 标签为选中态（按 id 定位，避免切到别的 data-tab 片段）
    pool_btn = html[html.index('id="tabBtn-pool"') - 220:html.index('id="tabBtn-pool"') + 160]
    assert "is-on" in pool_btn and 'aria-selected="true"' in pool_btn


def test_every_original_module_is_still_on_the_page(html):
    """原有模块与容器 id 一个都不能少（只挪位置，不删功能）。"""
    required = [
        # 全局
        "scanPanel", "scanLog", "drawer", "drawerMask", "dChart", "dGroups", "dHero",
        # 候选池页
        "statStrip", "poolTable", "poolBody", "poolEmpty", "poolCount", "btnMore",
        "fBoard", "fIndustry", "fMinScore", "fOrder", "fSearch",
        # 回测页
        "btCard", "btChart", "btTable", "btBody", "btResTable", "btResBody",
        "btMode", "btHorizon", "btnBtCsv",
        "indBtCard", "indSel", "indHorizon", "indBtChart", "indBtBody", "btnIndCsv",
        "robCard", "sigTable", "sigBody", "robTable", "robBody",
        "sensTable", "sensBody",
        # 简报页
        "repCard", "repOutline", "repMeta", "btnRepPreview", "btnRepPdf", "btnRepInline",
        # 市场概览页
        "macroDomestic", "macroOverseas", "macroUpdated",
        "industryChart", "indCoverage",
        "newsCard", "newsTabs", "newsList", "newsMeta", "newsLangBadge",
    ]
    for el_id in required:
        n = html.count(f'id="{el_id}"')
        assert n == 1, f"#{el_id} 出现 {n} 次（应为 1 次）"


def test_modules_are_grouped_into_the_intended_panels(html):
    """按使用场景归类：选股 / 回测 / 简报 / 行情，各归其位。"""
    def panel(name: str) -> str:
        start = html.index(f'id="panel-{name}"')
        nxt = [html.index(f'id="panel-{t}"') for t in TABS
               if f'id="panel-{t}"' in html and html.index(f'id="panel-{t}"') > start]
        return html[start:min(nxt)] if nxt else html[start:]

    pool = panel("pool")
    assert "poolTable" in pool and "statStrip" in pool

    bt = panel("backtest")
    for el_id in ("btChart", "indBtChart", "sigTable", "sensTable"):
        assert el_id in bt, f"回测页缺少 {el_id}"

    rep = panel("report")
    assert "repOutline" in rep and "repFrame" in rep

    mkt = panel("market")
    for el_id in ("macroDomestic", "industryChart", "newsList"):
        assert el_id in mkt, f"市场概览页缺少 {el_id}"

    # 扫描面板与页脚是全局元素，不属于任何标签页
    assert "scanPanel" not in "".join(panel(t) for t in TABS)


# ===========================================================================
# 二、切换契约：纯前端、零接口
# ===========================================================================
def test_tabs_are_buttons_not_links(html):
    """标签必须是 button —— 用 <a href> 会触发整页导航，破坏无刷新切换。"""
    block = html[html.index('id="tabbar"'):]
    block = block[:block.index("</nav>")]
    assert "<a " not in block, "标签栏内出现了链接元素，会触发页面跳转"
    assert block.count("<button") >= len(TABS)


def test_switch_tab_makes_no_network_call(app_js):
    """切页本身不得发请求：数据仍在首屏统一加载，切页只做显隐与重绘。"""
    body = _func_body(app_js, "switchTab")
    for probe in ("fetch(", "await get(", "await post("):
        assert probe not in body, f"switchTab 内出现 {probe}，切页不应触发接口请求"

    # 面板显隐是切换的核心动作
    assert "panel.hidden = key !== tab" in body
    assert "redrawTabCharts(tab)" in body


def test_tab_switch_rewrites_only_the_hash(app_js):
    """切页只改 URL hash（便于直达与回退），不产生整页导航。"""
    body = _func_body(app_js, "switchTab")
    assert "history.pushState" in body
    assert "location.reload" not in body and "location.href =" not in body
    assert "popstate" in app_js, "缺少浏览器前进/后退支持"


def test_burger_menu_for_narrow_screens(html, app_js):
    """窄屏折叠为汉堡菜单：按钮 + 菜单同源，且用内联 SVG 图标。"""
    assert 'id="btnNavMenu"' in html and 'id="navMenu"' in html
    btn = html[html.index('id="btnNavMenu"'):][:600]
    assert "<svg" in btn, "汉堡按钮应使用内联 SVG 图标"
    assert 'aria-expanded="false"' in btn and 'aria-controls="navMenu"' in btn

    items = re.findall(r'class="nav-menu-item[^"]*"[^>]*data-tab="(\w+)"', html)
    assert items == list(TABS), f"下拉菜单项应与标签一致：{items}"

    css = _read("app.css")
    assert re.search(r"@media \(max-width: 900px\)[\s\S]*?\.tab-list \{ display: none; \}", css), \
        "窄屏未折叠标签栏"
    assert "closeNavMenu" in app_js and "toggleNavMenu" in app_js


def test_no_emoji_as_functional_icons(html, app_js):
    """P0：功能图标不得使用 emoji / 符号字符。"""
    for name in ("index.html", "app.js", "i18n.js", "app.css"):
        src = html if name == "index.html" else (app_js if name == "app.js" else _read(name))
        hits = EMOJI_RE.findall(src)
        assert not hits, f"{name} 中出现 emoji 字符：{hits}"

    # 状态类图标必须是内联 SVG
    assert "const ICON = {" in app_js, "缺少统一的内联 SVG 图标入口"
    assert 'class="ico"' in app_js and ".ico {" in _read("app.css")
    for probe in ('ICON.check(', ):
        assert probe in app_js


# ===========================================================================
# 三、不回归：接口引用、图表重测、交互绑定
# ===========================================================================
def test_backend_interfaces_are_untouched(app_js):
    """前端引用的接口路径保持与 v1.7.0 一致，未因改版增删。"""
    for path in ("/api/scan/status", "/api/scan/run", "/api/pool/",
                 "/api/pool/industries", "/api/pool/export",
                 "/api/market/indices", "/api/market/industry-stats", "/api/news",
                 "/api/backtest/summary", "/api/backtest/trades.csv",
                 "/api/analysis/industry", "/api/analysis/industry.csv",
                 "/api/analysis/significance", "/api/analysis/resonance",
                 "/api/analysis/sensitivity",
                 "/api/report/html", "/api/report/export"):
        assert path in app_js, f"前端不再引用 {path}"


def test_resize_skips_hidden_charts(app_js):
    """隐藏面板内容器尺寸为 0，对其调用 resize() 会把画布压成 0。

    因此 resizeCharts 必须先判断容器可见性再重测。
    """
    body = _func_body(app_js, "resizeCharts")
    assert "clientWidth" in body and "clientHeight" in body, \
        "resizeCharts 未做可见性判断，隐藏页图表可能被压成 0 尺寸"
    for key in ("chart", "detailChart", "btChart", "indChart"):
        assert key in body, f"resizeCharts 漏掉了 {key}"


def test_original_interactions_still_bound(app_js):
    """原有交互绑定全部保留（筛选、分页、抽屉、资讯分类、回测切换、导出）。"""
    for probe in ("btnScanStart", "btnExport", "fSearch", "btnMore",
                  "btnDrawerClose", "drawerMask", ".news-tab", ".lang-opt",
                  "btnBtCsv", "btnIndCsv", "btnRepPdf"):
        assert probe in app_js, f"交互绑定丢失：{probe}"
    # 语言切换仍然是无刷新的重绘
    assert "I18N.onChange" in app_js and "rerenderAll" in app_js


def test_language_switch_keeps_tab_state(app_js):
    """切语言不跳页：rerenderAll 不得重置 state.tab 或面板显隐。"""
    body = _func_body(app_js, "rerenderAll")
    assert "state.tab" not in body, "切语言不应改写当前标签页"
    assert "switchTab" not in body, "切语言不应触发切页"


# ===========================================================================
# 四、研究简报在线预览 + 规则设置占位页
# ===========================================================================
def test_report_page_has_inline_preview(html, app_js):
    assert 'id="repFrame"' in html, "简报页缺少在线预览容器"
    frame = html[html.index('id="repFrame"'):][:300]
    assert "src=" not in frame, "预览应为惰性加载，HTML 中不应写死 src"

    body = _func_body(app_js, "loadReportFrame")
    assert "/api/report/html" in body
    assert "state.repFrameKey" in body, "预览需与扫描批次联动，避免展示旧批次"
    assert "btnRepInline" in app_js, "缺少手动刷新预览的入口"


def test_report_frame_guard_survives_null_run_id(app_js):
    """首屏直接以 #report 进入时，state.runId 仍为 null。

    若加载守卫写成「上次批次 === 当前批次」，两侧初始同为 null 会被误判为
    「已加载」而直接返回，简报预览永远不会发起请求——必须用独立标志位。
    """
    body = _func_body(app_js, "loadReportFrame")
    assert "repFrameLoaded" in body, "预览加载守卫必须使用独立布尔标志位"
    assert "state.repFrameRunId === state.runId" not in body, \
        "用批次相等做守卫会在 runId 为 null 时误判已加载"
    # 标志位要在函数里先置位再赋值 src，避免重复触发
    assert body.index("state.repFrameLoaded = true") < body.index("frame.src =")


def test_settings_page_still_documents_pending_conventions(html):
    """v1.9.0 起权重部分已是可用配置，其余三项配置仍须交代现行口径而非空洞占位。"""
    assert 'id="panel-settings"' in html
    assert 'id="setGrid"' in html
    items = re.findall(r'data-i18n="(set(?:Weight|Thresh|Hot|Filter)T)"', html)
    # 权重项已升级为真实配置界面的模块分组，不再是占位卡片
    assert items == ["setThreshT", "setHotT", "setFilterT"], items
    for key in ("setThreshNow", "setHotNow", "setFilterNow"):
        assert f'data-i18n="{key}"' in html, f"占位项缺少现行口径说明：{key}"
    assert 'data-i18n="setBadge"' in html


# ===========================================================================
# 五、双语覆盖
# ===========================================================================
def test_tab_and_new_section_keys_exist_in_both_packs(html):
    """新增的导航 / 预览 / 设置词条在 zh、en 两套语言包里都要有。"""
    src = _read("i18n.js")
    zh = src[src.index("\n    zh: {"):src.index("\n    en: {")]
    en = src[src.index("\n    en: {"):]

    new_keys = {f"tab{k}" for k in ("Pool", "Backtest", "Report", "Market", "Settings")}
    new_keys |= {"tabNavAria", "tabMenuBtn", "btnRepInline", "repFrameTitle",
                 "repFrameHint", "repFrameLoading", "repFrameFail",
                 "setTitle", "setSub", "setBadge", "setNowLabel",
                 "setTagPlanned", "setOtherT", "setOtherD",
                 "setThreshT", "setThreshD", "setThreshNow",
                 "setHotT", "setHotD", "setHotNow",
                 "setFilterT", "setFilterD", "setFilterNow"}
    # v1.9.0 权重配置新增词条
    new_keys |= {"setSumLabel", "setStateDefault", "setStateCustom", "setCapLine",
                 "setDflt", "setReset", "setLoading", "setLoadFail",
                 "setFootDefault", "setFootCustom", "setWarnOff",
                 "pillCustom", "poolSubCustom", "poolCountCustom", "statCustomSub"}

    missing_zh = sorted(k for k in new_keys if f"\n      {k}:" not in zh)
    missing_en = sorted(k for k in new_keys if f"\n      {k}:" not in en)
    assert not missing_zh, f"中文包缺失：{missing_zh}"
    assert not missing_en, f"英文包缺失：{missing_en}"

    # 导航标签在页面上确实按 data-i18n 引用
    for k in ("tabPool", "tabBacktest", "tabReport", "tabMarket", "tabSettings"):
        assert f'data-i18n="{k}"' in html

# -*- coding: utf-8 -*-
"""v1.9.0 打分权重可视化配置测试（纯前端）。

本次改造只在浏览器里按新权重重算候选池展示，**不改后端打分逻辑**，
因此测试集中在四类断言上：

1. 同构性——前端 `WEIGHT_SPEC` 的默认分值必须与 `app/config.py` 的
   `SCORE_*` 常量逐项相等，且三个模块的默认上限回到 50 / 20 / 30；
2. 覆盖性——引擎发出的每一个 `breakdown` 命中标志都要被前端读到位，
   前端不得自行做任何新的判定（只读标志位、只改票面分值）；
3. 边界契约——权重调整全程零接口请求、仅作用于当前会话（不落 localStorage）、
   闸门与恢复默认的行为可验证；
4. 不回归——回测、简报、导出等模块不引用权重，后端常量保持原值。

其中第 1 条用「配置常量 ↔ JS 默认值」双向对齐的方式锁死：
任何一侧改了分值而另一侧没同步，测试立即失败。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import config

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_js() -> str:
    return _read("app.js")


@pytest.fixture(scope="module")
def html() -> str:
    return _read("index.html")


@pytest.fixture(scope="module")
def i18n() -> str:
    return _read("i18n.js")


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
    raise AssertionError(f"函数体括号不配对：{name}")


def _weight_spec(app_js: str) -> list[dict]:
    """解析 app.js 里的 WEIGHT_SPEC，返回三大模块及其指标项。"""
    at = app_js.index("const WEIGHT_SPEC = [")
    end = app_js.index("\n];", at)
    block = app_js[at:end]

    groups: list[dict] = []
    # 模块头： key: "core", name: "groupCore", ref: 50,
    for gm in re.finditer(
        r'key:\s*"(\w+)",\s*name:\s*"(\w+)",\s*ref:\s*(\d+)', block
    ):
        groups.append({"key": gm.group(1), "name": gm.group(2),
                       "ref": int(gm.group(3)), "items": []})

    # 指标行： { id: "macd", dflt: 15, ... }
    for im in re.finditer(r'\{\s*id:\s*"(\w+)",\s*dflt:\s*(\d+)', block):
        # 归属模块 = 该指标之前最近的一个模块头
        prefix = block[: im.start()]
        owner = None
        for gm in re.finditer(r'key:\s*"(\w+)",\s*name:', prefix):
            owner = gm.group(1)
        assert owner, f"指标 {im.group(1)} 未能归属到任何模块"
        grp = next(g for g in groups if g["key"] == owner)
        grp["items"].append({"id": im.group(1), "dflt": int(im.group(2))})
    return groups


# ===========================================================================
# 一、与后端 v1.1 引擎同构
# ===========================================================================
def test_default_weights_match_backend_constants(app_js):
    """前端默认分值 = 后端 config 里的原始分值，逐项相等。"""
    expected = {
        "macd": config.SCORE_MACD_GOLD,
        "kdj": config.SCORE_KDJ_GOLD,
        "vol13": config.SCORE_VOLUME_SURGE,
        "vol2": config.SCORE_VOLUME_DOUBLE,
        "turnover": config.SCORE_TURNOVER_HEALTHY,
        "range": config.SCORE_RANGE_COMPACT,
        "chipBase": config.SCORE_CHIP_CONCENTRATED,
        "chipLoose": config.SCORE_CHIP_LOOSE_EXTRA,
        "peLow": config.SCORE_PE_CHEAP,
        "peMid": config.SCORE_PE_MID,
        "return5": config.SCORE_RETURN5_HEALTHY,
        "growth": config.SCORE_GROWTH_BOARD,
        "hot": config.SCORE_HOT_INDUSTRY,
    }
    got = {
        it["id"]: it["dflt"]
        for g in _weight_spec(app_js)
        for it in g["items"]
    }
    assert got == expected, {
        "缺失": {k: v for k, v in expected.items() if k not in got},
        "多余": {k: v for k, v in got.items() if k not in expected},
        "数值不一致": {
            k: (got[k], expected[k]) for k in expected if k in got and got[k] != expected[k]
        },
    }


def test_module_upper_bounds_match_backend_design(app_js):
    """三个模块的默认上限必须回到 50 / 20 / 30，合计 100。"""
    groups = _weight_spec(app_js)
    assert [g["key"] for g in groups] == ["core", "fund", "industry"]

    # 量能是叠加项（>2x 在 >1.3x 基础上再加），PE 两档互斥，需按引擎口径还原
    def cap(grp):
        excl, total = {}, 0
        for it in grp["items"]:
            if it["id"] == "vol2":
                continue                      # 叠加项，其分值已含在 vol13 的等效上限外
            if it["id"] in ("peLow", "peMid"):
                excl["pe"] = max(excl.get("pe", 0), it["dflt"])
                continue
            if it["id"] == "chipLoose":
                excl["chip"] = max(excl.get("chip", 0),
                                   it["dflt"] + _dflt(groups, "chipBase"))
                continue
            if it["id"] == "chipBase":
                excl["chip"] = max(excl.get("chip", 0), it["dflt"])
                continue
            total += it["dflt"]
        return total + sum(excl.values())

    def _dflt(gs, wid):
        for g in gs:
            for it in g["items"]:
                if it["id"] == wid:
                    return it["dflt"]
        raise AssertionError(wid)

    # 核心形态：15+12+8+5(叠加)+5+5 = 50
    core = next(g for g in groups if g["key"] == "core")
    assert sum(it["dflt"] for it in core["items"]) == 50 == core["ref"]
    # 筹码基本面：chip 8（3+5 叠加，取最大）+ PE 6（两档取最大）+ 5日涨幅 6 = 20
    fund = next(g for g in groups if g["key"] == "fund")
    assert cap(fund) == 20 == fund["ref"]
    # 行业板块：8 + 22 = 30
    ind = next(g for g in groups if g["key"] == "industry")
    assert cap(ind) == 30 == ind["ref"]
    assert sum(g["ref"] for g in groups) == 100


def test_reference_total_is_one_hundred(app_js):
    assert "const WEIGHT_REF_TOTAL = 100;" in app_js
    # 默认权重下 isDefaultWeights() 必须判定为「未偏离」，恢复默认按钮据此置灰
    assert "function isDefaultWeights()" in app_js


def test_every_breakdown_flag_is_read(app_js, ):
    """引擎发出的每个命中标志都被前端读到位（漏一个就会少算分）。"""
    flags = {
        "core": ["macd_gold_red", "kdj_gold_j_under_100", "volume_surge_1_3x",
                 "volume_surge_2x", "turnover_healthy_3_15", "range_compact_40d"],
        "fund": ["chip_concentrated_le_18", "chip_loose_gt_20",
                 "return5_healthy_5_20"],
        "industry": ["growth_board", "hot_industry"],
    }
    spec = app_js[app_js.index("const WEIGHT_SPEC = ["):]
    spec = spec[: spec.index("\n];")]
    for grp, names in flags.items():
        for n in names:
            # 取值前先判空（!!b.fund && !!b.fund.xxx），避免明细缺失时抛异常
            assert f"b.{grp}.{n}" in spec, f"未读取命中标志：{grp}.{n}"
        assert f"b.{grp} &&" in spec, f"未对 {grp} 分组做空值保护"
    # PE 走三值枚举，需显式判定两档
    assert 'b.fund.pe_tier === "low"' in spec
    assert 'b.fund.pe_tier === "mid"' in spec


def test_backend_breakdown_flags_unchanged():
    """标志位命名与后端 ScoreBreakdown.to_dict 一一对应（后端未改动）。"""
    src = (ROOT / "app" / "scoring.py").read_text(encoding="utf-8")
    for name in ("macd_gold_red", "kdj_gold_j_under_100", "volume_surge_1_3x",
                 "volume_surge_2x", "turnover_healthy_3_15", "range_compact_40d",
                 "chip_concentrated_le_18", "chip_loose_gt_20",
                 "return5_healthy_5_20", "growth_board", "hot_industry"):
        assert f'"{name}"' in src, f"后端 breakdown 缺少标志位：{name}"


def test_weight_engine_does_not_rejudge_anything(app_js):
    """前端只读标志位、不做判定：不得出现比较行情数值的表达式。"""
    body = _func_body(app_js, "scoreWithWeights")
    for banned in (">", "<", "pe_percentile", "turnover_rate", "close"):
        assert banned not in body, f"scoreWithWeights 不应包含判定逻辑：{banned}"
    # 逐项分值只能来自权重表
    assert "wEffPts(it, w)" in body


# ===========================================================================
# 二、零接口请求 / 仅作用于当前会话
# ===========================================================================
def test_weight_adjustment_makes_no_network_call(app_js):
    """拖动滑块到表格更新的整条链路上不得出现任何网络调用。"""
    for fn in ("onWeightInput", "scheduleWeightsApply", "scoreWithWeights",
               "moduleCap", "poolViewItems", "updateWeightReadouts",
               "recomputeLocalStats", "renderWeightPanel"):
        body = _func_body(app_js, fn)
        for banned in ("await get(", "await post(", "fetch(", "XMLHttpRequest"):
            assert banned not in body, f"{fn} 不应发起网络请求：{banned}"


def test_pool_render_prefers_local_score(app_js):
    """候选池分值取本地重算结果，没有本地值时才回落到后端原始分。"""
    body = _func_body(app_js, "renderTable")
    assert "x.local_score ?? x.total_score" in body
    # 自定义口径下进度条按 0-100 截断，避免分值超出满分把条压爆
    assert "Math.min(100, score)" in body


def test_local_rescore_covers_all_pool_items(app_js):
    """本地重算的数据源是「全量快照」，不是当前分页的 200 条。"""
    body = _func_body(app_js, "ensurePoolFull")
    assert "limit" in body and "offset" in body, "需分块拉取全量候选池"
    assert "state.poolFull = {" in body
    # 并发调用共享同一次拉取，避免重复打接口
    assert "state.poolFullPromise" in body


def test_stats_are_recomputed_on_every_weight_change(app_js):
    """统计卡分档必须每次改动都重算。

    快照会在进入设置页时被后台预取，于是 `applyWeights` 里
    「首次拉取」分支在真正拖动时根本不会进入——若把重算只放在该分支内，
    分档就会一直停留在上一组权重（甚至后端原始口径）的结果上。
    """
    body = _func_body(app_js, "applyWeights")
    # 顶层缩进（2 空格）= 无条件执行；嵌在 if 块里则是 4 空格
    assert "\n  recomputeLocalStats();" in body, "重算必须放在条件分支之外"
    assert "\n    recomputeLocalStats();" not in body, "重算不得嵌在「首次拉取」分支里"
    # 顺序：先拿到快照，再重算，最后渲染 —— 不能先渲染后重算
    assert body.index("ensurePoolFull()") < body.index("recomputeLocalStats();")
    assert body.index("recomputeLocalStats();") < body.index("renderStatus();")
    # renderStatus 在自定义口径下取本地分档，缺失时才回落后端
    status = _func_body(app_js, "renderStatus")
    assert "weightsActive() ? state.poolFull.stats : null" in status


def test_weights_are_session_scoped(app_js):
    """权重只活在内存里：不落 localStorage / sessionStorage / cookie。"""
    for fn in ("onWeightInput", "resetWeights", "applyWeights", "scheduleWeightsApply"):
        body = _func_body(app_js, fn)
        for banned in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
            assert banned not in body, f"{fn} 不应持久化权重：{banned}"
    # 页面加载时从默认值起步（刷新即回到默认口径）
    assert "state.weights = Object.assign({}, DEFAULT_WEIGHTS);" in app_js


def test_reset_weights_restores_defaults(app_js):
    body = _func_body(app_js, "resetWeights")
    assert "state.weights = Object.assign({}, DEFAULT_WEIGHTS);" in body
    assert "state.weightsCustom = false;" in body
    # 恢复默认后必须回到后端原始口径（重新按服务端结果渲染列表与统计）
    assert "await loadPool()" in body
    assert "renderStatus()" in body
    # 在途的防抖任务要取消，否则松手后又被旧权重覆盖
    assert "clearTimeout(weightApplyTimer)" in body


def test_custom_weights_fall_back_to_backend_when_loading_fails(app_js):
    """全量快照拉取失败时不得白屏：给出提示并保留已调整的权重。"""
    body = _func_body(app_js, "applyWeights")
    assert "setLoadFail" in body
    assert "return" in body
    # 失败路径不应把表格清空
    assert "poolBody" not in body


# ===========================================================================
# 三、界面契约
# ===========================================================================
def test_settings_panel_has_weight_controls(html):
    assert 'id="panel-settings"' in html
    for el in ("sumBar", "wGroups", "wFootHint", "btnWeightReset", "sumState", "sumWarn"):
        assert f'id="{el}"' in html, f"权重配置页缺少元素：{el}"
    assert 'data-i18n="setReset"' in html


def test_slider_range_and_step_are_precise(app_js):
    """滑块范围贴合原始分值尺度（0 ~ 2×默认），步长 1，支持精确调整。"""
    body = _func_body(app_js, "weightRowHtml")
    assert 'type="range"' in body
    assert 'min="0"' in body
    assert 'max="${wMaxOf(it)}"' in body
    assert 'step="1"' in body
    # 当前分值数字 + 默认值标注
    assert "wrow-val" in body and "wrow-dflt" in body
    assert "t(\"setDflt\"" in body
    # 无障碍：滑块带可读标签
    assert "aria-label=" in body


def test_slider_drag_does_not_rebuild_dom(app_js):
    """拖动过程中只更新数字，不重建滑块 DOM（重建会打断拖拽）。"""
    body = _func_body(app_js, "updateWeightReadouts")
    # 分值数字与上限条可以整体重绘，但滑块所属容器必须原地更新
    assert "$(\"wGroups\").innerHTML" not in body
    assert "wVal-" in body
    # 拖动合并 160ms 一次重算，松手立即结算
    assert "160" in _func_body(app_js, "scheduleWeightsApply")
    assert "change" in _func_body(app_js, "bindEvents")


def test_total_is_validated_live(app_js):
    """总分与模块上限实时校验：偏离 100 分时给出提示。"""
    body = _func_body(app_js, "updateWeightReadouts")
    assert "totalCap(w)" in body
    assert "WEIGHT_REF_TOTAL" in body
    assert "setWarnOff" in body
    assert "moduleCap(g, w)" in body
    # 与默认口径一致时只说上限，偏离时才补参考值，避免「上限 X / 参考 X」的冗余
    assert 't("setCapSame"' in body and 't("setCapLine"' in body


def test_slider_shows_filled_progress(app_js):
    """滑块用背景渐变显示已填充进度（不改 DOM 结构，不触发重排）。"""
    body = _func_body(app_js, "paintRange")
    assert "linear-gradient" in body
    assert "backgroundImage" in body
    # 进度由当前值 / 该项上界换算
    assert "wMaxOf(it)" in body
    assert "paintRange(it.id)" in _func_body(app_js, "updateWeightReadouts")
    # 轨道本体必须透明，否则与填充色叠加
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    at = css.index(".wrange::-webkit-slider-runnable-track")
    assert "transparent" in css[at: at + 90]


def test_weight_spec_sums_exclusivity_is_modelled(app_js):
    """两处叠加 / 互斥点必须如实建模，否则重算结果与后端不一致。"""
    body = _func_body(app_js, "moduleCap")
    assert "exclusive" in body and "Math.max" in body
    assert 'id: "chipLoose"' in app_js or 'id:"chipLoose"' in app_js
    # 筹码 >20% 时后端等于「基础分 + 叠加分」，因此需要 extra 合并
    assert "extra" in _func_body(app_js, "wEffPts")


def test_pool_marks_custom_convention(app_js, html):
    """自定义口径必须在候选池上明确标注，避免与后端口径混淆。"""
    assert 'id="poolCustomPill"' in html
    body = _func_body(app_js, "renderTable")
    assert "poolCustomPill" in body
    assert "poolSubCustom" in body and "poolCountCustom" in body


def test_drawer_follows_current_weights(app_js):
    """抽屉总分与明细同步走当前权重，避免与表格口径不一致。"""
    assert "function buildScoreGroups(bd, w)" in app_js
    body = _func_body(app_js, "buildScoreGroups")
    assert "state.weights" in body
    assert "moduleCap(" in body
    header = _func_body(app_js, "renderDetailHeader")
    assert "scoreWithWeights(bd, state.weights)" in header
    # 抽屉打开时改权重，需要重绘
    assert "renderDrawerAll()" in _func_body(app_js, "applyWeights")


def test_switching_tabs_keeps_weight_state(app_js):
    """切走再切回，配置状态保留：切换本身只重绘，绝不重置或重新初始化权重。"""
    body = _func_body(app_js, "switchTab")
    # 进入设置页时按 state 重绘，并后台预取全量快照（保证拖动时零请求）
    assert "renderWeightPanel()" in body
    assert "prefetchPoolFull()" in body
    # 关键契约：切换不得写入权重、不得调用恢复默认
    assert "state.weights =" not in body
    assert "resetWeights" not in body
    assert "DEFAULT_WEIGHTS" not in body
    # 权重只存在 state 上（会话内保留，刷新即回默认），不入 localStorage
    assert "localStorage" not in _func_body(app_js, "applyWeights")
    assert "localStorage" not in _func_body(app_js, "renderWeightPanel")


def test_language_switch_redraws_weight_panel(app_js):
    assert "renderWeightPanel();" in _func_body(app_js, "rerenderAll")
    # 指标名称复用既有词条，逐条存在
    # （指标名来自 WEIGHT_SPEC 的 label / labelKey，均指向语言包）
    assert "wLabelOf" in app_js


# ===========================================================================
# 四、不回归：其他模块与后端
# ===========================================================================
def test_other_modules_do_not_reference_weights(app_js):
    """回测 / 简报 / 导出 / 资讯等模块不得引用权重，口径保持与后端一致。"""
    for fn in ("loadBacktest", "renderBacktest", "renderBacktestTable",
               "exportCsv", "loadAnalysis", "renderSignificance",
               "renderSensitivity", "renderRobustness", "previewReport",
               "exportReportPdf", "loadNews"):
        body = _func_body(app_js, fn)
        assert "state.weights" not in body, f"{fn} 不应受权重影响"
        assert "scoreWithWeights" not in body, f"{fn} 不应受权重影响"


def test_backend_score_constants_keep_original_values():
    """后端原始分值保持不变——本版本不动打分逻辑。"""
    assert config.SCORE_MACD_GOLD == 15
    assert config.SCORE_KDJ_GOLD == 12
    assert config.SCORE_VOLUME_SURGE == 8
    assert config.SCORE_VOLUME_DOUBLE == 5
    assert config.SCORE_TURNOVER_HEALTHY == 5
    assert config.SCORE_RANGE_COMPACT == 5
    assert config.SCORE_CHIP_CONCENTRATED == 3
    assert config.SCORE_CHIP_LOOSE_EXTRA == 5
    assert config.SCORE_PE_CHEAP == 6
    assert config.SCORE_PE_MID == 3
    assert config.SCORE_RETURN5_HEALTHY == 6
    assert config.SCORE_GROWTH_BOARD == 8
    assert config.SCORE_HOT_INDUSTRY == 22


def test_backend_api_untouched(app_js):
    """接口引用集合与 v1.8.0 一致：没有新增 / 删除任何后端接口。"""
    for p in ("/api/scan/status", "/api/scan/run", "/api/pool/?",
              "/api/pool/industries", "/api/pool/export", "/api/market/indices",
              "/api/market/industry-stats", "/api/news", "/api/backtest/summary",
              "/api/analysis/industry", "/api/analysis/significance",
              "/api/analysis/resonance", "/api/analysis/sensitivity",
              "/api/report/html"):
        assert p in app_js, f"接口引用丢失：{p}"
    # 权重配置不得引入新的写接口
    assert "method: \"POST\"" not in _func_body(app_js, "applyWeights")
    assert "post(" not in _func_body(app_js, "onWeightInput")


def test_weight_keys_exist_in_both_packs(i18n):
    """权重配置相关词条在两套语言包里都要有（含 {n} 占位符）。"""
    zh = i18n[i18n.index("\n    zh: {"):i18n.index("\n    en: {")]
    en = i18n[i18n.index("\n    en: {"):]
    keys = ("setTitle", "setSub", "setSumLabel", "setStateDefault", "setStateCustom",
            "setCapLine", "setCapSame", "setDflt", "setReset", "setLoading", "setLoadFail",
            "setFootDefault", "setFootCustom", "setWarnOff",
            "pillCustom", "poolSubCustom", "poolCountCustom", "statCustomSub")
    missing_zh = [k for k in keys if f"\n      {k}:" not in zh]
    missing_en = [k for k in keys if f"\n      {k}:" not in en]
    assert not missing_zh, f"中文包缺失：{missing_zh}"
    assert not missing_en, f"英文包缺失：{missing_en}"
    # 带占位符的词条两侧占位符一致，否则英文会漏渲染数字
    for k in ("setCapLine", "setCapSame", "setDflt", "setFootCustom", "setWarnOff",
              "poolCountCustom"):
        z = re.search(rf"\n      {k}: \"([^\"]*)\"", zh).group(1)
        e = re.search(rf"\n      {k}: \"([^\"]*)\"", en).group(1)
        assert set(re.findall(r"\{(\w+)\}", z)) == set(re.findall(r"\{(\w+)\}", e)), k


def test_pe_tier_labels_exist(app_js, i18n):
    """权重面板里 PE 两档的名字要能取到词条。"""
    assert '"itemPeLow"' in app_js and '"itemPeMid"' in app_js
    for k in ("itemPeLow", "itemPeMid"):
        assert f"\n      {k}:" in i18n

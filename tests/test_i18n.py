# -*- coding: utf-8 -*-
"""前端中英双语（i18n）一致性测试。

覆盖三类容易出问题的地方：
1. 语言包自身：中英词条一一对应，无单边缺失；
2. 页面与脚本引用的词条：index.html 的 data-i18n* 与 app.js 的 t(...) 调用
   必须都能在语言包里找到，避免出现界面上直接裸露键名；
3. 行业 / 板块 / 指数英文映射：申万一级 31 个行业全覆盖。

测试直接解析静态文件文本，不需要浏览器环境。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC = PROJECT_ROOT / "app" / "static"

# 申万一级行业（31 个），英文译名必须全覆盖
SW_LEVEL1 = {
    "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "家用电器", "食品饮料",
    "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输", "房地产",
    "商贸零售", "社会服务", "综合", "建筑材料", "建筑装饰", "电力设备",
    "国防军工", "计算机", "传媒", "通信", "银行", "非银金融", "汽车",
    "机械设备", "煤炭", "石油石化", "环保", "美容护理",
}

# 由代码动态拼接的词条（无法用静态正则提取，此处显式登记）
DYNAMIC_KEYS = {
    # HIT_TAGS: t(`tag${def.key}`) / t(`hit${def.key}`)
    "tagMacd", "tagKdj", "tagVol13", "tagVol2", "tagTurnover", "tagRange",
    "tagChipLow", "tagChipLoose", "tagPe", "tagReturn5", "tagGrowth", "tagHot",
    "hitMacd", "hitKdj", "hitVol13", "hitVol2", "hitTurnover", "hitRange",
    "hitChipLow", "hitChipLoose", "hitPe", "hitReturn5", "hitGrowth", "hitHot",
    # PE_TIER_KEY
    "peTierLow", "peTierMid", "peTierHigh", "peTierMissing",
    # NEWS_CAT_KEY
    "newsCatDomestic", "newsCatOverseas", "newsCatMacro",
    # OVERSEAS_TAG
    "tagOvernight", "tagToday",
}


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _brace_block(text: str, start: int) -> str:
    """从 `{` 所在位置起做括号配对，返回花括号内部的文本。"""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
    raise AssertionError("花括号未闭合，i18n.js 结构可能已被破坏")


def _dict_bodies() -> tuple[str, str]:
    """取出 i18n.js 中 zh / en 两个语言包对象体的文本。"""
    src = _read("i18n.js")
    start = src.index("const DICT = {")
    body = _brace_block(src, src.index("{", start))
    zh_at = body.index("zh: {")
    zh = _brace_block(body, body.index("{", zh_at))
    en_at = body.index("en: {")
    en = _brace_block(body, body.index("{", en_at))
    return zh, en


def _keys(body: str) -> set[str]:
    return set(re.findall(r"^ {6}([A-Za-z0-9_]+):", body, flags=re.M))


@pytest.fixture(scope="module")
def packs() -> tuple[set[str], set[str]]:
    zh, en = _dict_bodies()
    return _keys(zh), _keys(en)


def test_language_packs_have_identical_keys(packs):
    """中英语言包的词条必须完全一一对应，不允许单边缺失。"""
    zh, en = packs
    assert zh, "未能解析出中文语言包，i18n.js 结构可能有变"
    assert zh == en, f"仅中文有: {sorted(zh - en)}; 仅英文有: {sorted(en - zh)}"


def test_referenced_keys_exist_in_both_packs(packs):
    """index.html 与 app.js 引用的每个词条都必须能在语言包里找到。"""
    zh, en = packs

    html_keys = set(re.findall(
        r'data-i18n(?:-ph|-title|-aria)?="([A-Za-z0-9_]+)"', _read("index.html")
    ))
    # 仅匹配 t("key") 形式；模板字符串拼接的词条由 DYNAMIC_KEYS 覆盖
    js_keys = set(re.findall(r'\bt\(\s*"([A-Za-z0-9_]+)"', _read("app.js")))

    referenced = html_keys | js_keys | DYNAMIC_KEYS
    missing = sorted(k for k in referenced if k not in zh or k not in en)
    assert not missing, f"以下词条在语言包中缺失: {missing}"

    # 反向检查：动态词条确实被代码用到，避免登记表长期失效
    app_js = _read("app.js")
    for probe in ("tag${def.key}", "hit${def.key}", "PE_TIER_KEY", "NEWS_CAT_KEY"):
        assert probe in app_js, f"app.js 中未找到动态词条拼接点 {probe}"


def test_industry_map_covers_all_sw_level1():
    """申万一级 31 个行业必须都有英文译名，且译名不重复。"""
    src = _read("i18n.js")
    at = src.index("const SW_INDUSTRY_EN = {")
    body = _brace_block(src, src.index("{", at))
    mapping = dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', body))

    missing = sorted(SW_LEVEL1 - set(mapping))
    assert not missing, f"以下申万一级行业缺少英文译名: {missing}"
    assert "其他" in mapping, "行业映射缺少「其他」兜底项"

    names = [v for k, v in mapping.items() if k != "其他"]
    assert len(names) == len(set(names)), "存在重复的行业英文译名"


def test_board_and_index_maps_are_complete():
    """板块与宏观指数的英文映射需覆盖全部会出现的名称。"""
    src = _read("i18n.js")
    at = src.index("const BOARD_EN = {")
    boards = dict(re.findall(r'"([^"]+)":\s*"([^"]+)"',
                             _brace_block(src, src.index("{", at))))

    at = src.index("const INDEX_EN = {")
    indices = dict(re.findall(r'"([^"]+)":\s*"([^"]+)"',
                              _brace_block(src, src.index("{", at))))

    # 与 app.js 的 BOARDS 常量、宏观面板固定顺序保持一致
    app_js = _read("app.js")
    for name in ("沪主板", "深主板", "创业板", "科创板", "其他"):
        assert name in boards, f"板块映射缺少 {name}"
    for name in ("上证指数", "深证成指", "创业板指", "科创50",
                 "纳斯达克", "标普500", "恒生指数"):
        assert name in indices, f"指数映射缺少 {name}"
        assert name in app_js, f"app.js 未引用指数 {name}"


def test_no_legacy_version_reference():
    """静态资源版本号应统一，避免升级后浏览器沿用旧脚本。"""
    html = _read("index.html")
    assert "?v=1.5.0" in html
    assert "?v=1.4.0" not in html
    assert "/static/i18n.js" in html, "页面未引入语言包"
    # 语言包必须在 app.js 之前加载
    assert html.index("/static/i18n.js") < html.index("/static/app.js")

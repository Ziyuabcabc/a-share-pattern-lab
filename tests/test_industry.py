# -*- coding: utf-8 -*-
"""行业口径归一化测试：保证行业字段始终落在申万一级口径内。

覆盖：申万一级原名直通、常见别名归并、证监会口径映射、
风格/概念类板块名被丢弃（不污染行业分布口径）。
"""

from app import data_source as ds


def test_sw_level1_passthrough():
    for name in ("电子", "医药生物", "机械设备", "美容护理"):
        assert ds._normalize_industry(name) == name


def test_alias_normalized_to_sw():
    cases = {
        "电子信息": "电子",
        "半导体": "电子",
        "软件开发": "计算机",
        "通讯行业": "通信",
        "医药制造": "医药生物",
        "医疗器械": "医药生物",
        "化工行业": "基础化工",
        "机械行业": "机械设备",
        "汽车零部件": "汽车",
        "家电行业": "家用电器",
        "酿酒行业": "食品饮料",
        "纺织服装": "纺织服饰",
        "券商信托": "非银金融",
        "电力行业": "电力设备",
        "房地产开发": "房地产",
        "物流行业": "交通运输",
        "旅游酒店": "社会服务",
        "文化传媒": "传媒",
        "环保行业": "环保",
        "燃气": "公用事业",
    }
    for raw, expect in cases.items():
        assert ds._normalize_industry(raw) == expect, raw


def test_style_board_rejected():
    """风格/概念类板块名（非行业维度）必须丢弃，返回 None。"""
    for name in ("次新股", "ST股", "低价股", "融资融券", "专精特新",
                 "预盈预增", "股权激励", "基金重仓", "创业成份", ""):
        assert ds._normalize_industry(name) is None, name
        assert ds._normalize_industry(None) is None


def test_normalized_result_within_sw_scope():
    """归一化结果必须落在申万一级 31 个名称内。"""
    samples = ["电子信息", "医药制造", "金融行业", "未知行业名", "次新股"]
    for raw in samples:
        got = ds._normalize_industry(raw)
        assert got is None or got in ds.SW_LEVEL1, raw


def test_csrc_industry_mapping():
    """证监会口径（北交所名单）映射到申万一级。"""
    assert ds._csrc_industry_to_sw("汽车制造业") == "汽车"
    assert ds._csrc_industry_to_sw("计算机、通信和其他电子设备制造业") == "电子"
    assert ds._csrc_industry_to_sw("医药制造业") == "医药生物"
    assert ds._csrc_industry_to_sw("软件和信息技术服务业") == "计算机"
    assert ds._csrc_industry_to_sw("") is None

# -*- coding: utf-8 -*-
"""市场资讯模块测试。

覆盖：分类规则、时间归一化、标题清洗、去重、中性化过滤词表，
以及接口在「抓取成功 / 抓取失败 / 分类筛选」三种情形下的返回结构。

所有用例均不访问网络：需要数据的场景通过 monkeypatch 注入合成快照。
"""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app
from app.routers import news

client = TestClient(app)


# ---------------------------------------------------------------------------
# 分类规则
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        # 境内市场强特征：A 股与上市公司公告
        ("宁德时代：全资子公司拟11.2亿元参与投资创业投资基金", "domestic"),
        ("沪指午后翻红 创业板指涨超1%", "domestic"),
        ("世纪恒通：实控人等拟合计减持不超3%公司股份", "domestic"),
        # 海外：市场、央行、公司与国别
        ("美股三大股指小幅低开 半导体股票小幅上涨", "overseas"),
        ("凯雷集团：美联储面临巨大压力", "overseas"),
        ("毕马威拟在英国咨询业务部门再裁员近200人", "overseas"),
        # 宏观：经济数据与货币财政
        ("国家统计局公布最新房价数据", "macro"),
        ("央行研究局局长：人工智能发展带来的水资源消耗值得高度关注", "macro"),
        ("8月社会融资规模增量累计为23.91万亿", "macro"),
    ],
)
def test_classify(text, expected):
    assert news._classify(text) == expected


def test_classify_domestic_wins_over_overseas():
    """跨域标题（海外事件 + A 股市场）应归入国内资讯，避免误判。"""
    assert news._classify("美联储加息落地 A股三大指数集体高开") == "domestic"


def test_classify_defaults_to_domestic():
    assert news._classify("某公司发布新一代产品") == "domestic"


# ---------------------------------------------------------------------------
# 中性化过滤词表
# ---------------------------------------------------------------------------
def test_blocked_words_are_joined_from_fragments():
    words = news._blocked_words()
    assert len(words) == len(config.NEWS_BLOCK_PARTS)
    assert all(len(w) >= 2 for w in words)
    # 词表本身来源于片段拼接，长度应与片段总长一致
    for parts, word in zip(config.NEWS_BLOCK_PARTS, words):
        assert word == "".join(parts)


def test_blocked_title_is_dropped(monkeypatch, tmp_path):
    """标题命中过滤词表的条目应整体跳过，其余条目保留。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    blocked = news._blocked_words()[0]
    monkeypatch.setattr(news, "_fetch_feed", lambda: (
        [
            {"title": f"某机构发布{blocked}清单", "summary": "", "url": "u1",
             "stamp": "2026-09-15 10:00:00", "source": "测试源"},
            {"title": "沪指午后翻红 创业板指涨超1%", "summary": "", "url": "u2",
             "stamp": "2026-09-15 09:00:00", "source": "测试源"},
        ],
        ["测试源"],
        [],
    ))
    data = news._build_feed()
    titles = [x["title"] for x in data["items"]]
    assert all(blocked not in t for t in titles)
    assert "沪指午后翻红 创业板指涨超1%" in titles


# ---------------------------------------------------------------------------
# 字段归一化
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-09-15 21:24:41", "2026-09-15 21:24:41"),
        ("2026-09-15 21:24", "2026-09-15 21:24:00"),
        ("2026-09-15", "2026-09-15 00:00:00"),
        ("20260915", "2026-09-15 00:00:00"),
    ],
)
def test_norm_time(raw, expected):
    assert news._norm_time(raw) == expected


def test_norm_time_blank():
    assert news._norm_time(None) == ""
    assert news._norm_time("   ") == ""


def test_clean_extracts_bracketed_title():
    assert news._clean("【央行开展逆回购操作】央行今日开展1000亿元逆回购操作") == "央行开展逆回购操作"


def test_clean_keeps_plain_title():
    assert news._clean("美股三大股指小幅低开") == "美股三大股指小幅低开"


def test_normalize_rows_maps_columns_and_falls_back_url():
    df = pd.DataFrame(
        {
            "标题": ["沪指收涨"],
            "内容": ["沪指今日收涨 0.5%"],
            "发布时间": ["2026-09-15 15:00:00"],
        }
    )
    rows = news._normalize_rows(df, "测试源", "https://example.com/home")
    assert len(rows) == 1
    assert rows[0]["title"] == "沪指收涨"
    assert rows[0]["url"] == "https://example.com/home"  # 无链接字段时回退源主页
    assert rows[0]["source"] == "测试源"
    assert rows[0]["stamp"] == "2026-09-15 15:00:00"


def test_normalize_rows_uses_body_when_title_missing():
    """财联社 / 富途 / 新浪等源没有标题字段，应以正文首句兜底。"""
    df = pd.DataFrame({"内容": ["财联社9月15日电，现货白银日内涨幅扩大至1%。"], "发布时间": ["2026-09-15 20:56:17"]})
    rows = news._normalize_rows(df, "财联社", "https://example.com")
    assert len(rows) == 1
    assert rows[0]["title"].startswith("财联社9月15日电")


def test_dedupe_keeps_first_occurrence():
    items = [
        {"title": "美股三大股指小幅低开", "source": "A"},
        {"title": "美股三大股指小幅低开！", "source": "B"},
        {"title": "沪指午后翻红", "source": "A"},
    ]
    out = news._dedupe(items)
    assert len(out) == 2
    assert out[0]["source"] == "A"


# ---------------------------------------------------------------------------
# 接口行为
# ---------------------------------------------------------------------------
_SAMPLE = {
    "available": True,
    "items": [
        {"title": "沪指午后翻红", "source": "测试源", "time": "2026-09-15 14:00:00",
         "url": "https://example.com/1", "category": "domestic"},
        {"title": "美股三大股指小幅低开", "source": "测试源", "time": "2026-09-15 21:30:00",
         "url": "https://example.com/2", "category": "overseas"},
        {"title": "8月社融数据发布", "source": "测试源", "time": "2026-09-15 09:00:00",
         "url": "https://example.com/3", "category": "macro"},
    ],
    "counts": {"all": 3, "domestic": 1, "overseas": 1, "macro": 1},
    "sources": ["测试源"],
    "cached_at": "2026-09-15 22:00:00",
    "message": "",
    "errors": [],
    "source_note": config.NEWS_SOURCE_NOTE,
    "categories": [{"key": k, "label": v} for k, v in config.NEWS_CATEGORIES],
    "disclaimer": config.DISCLAIMER,
}


def _patch_feed(monkeypatch, tmp_path, payload):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "news.db")
    monkeypatch.setattr(news, "_build_feed", lambda: dict(payload))


def test_api_returns_all_categories_by_default(monkeypatch, tmp_path):
    _patch_feed(monkeypatch, tmp_path, _SAMPLE)
    body = client.get("/api/news").json()
    assert body["available"] is True
    assert len(body["items"]) == 3
    assert body["categories"] and body["source_note"]


def test_api_filters_by_category(monkeypatch, tmp_path):
    _patch_feed(monkeypatch, tmp_path, _SAMPLE)
    body = client.get("/api/news?category=overseas").json()
    assert [x["category"] for x in body["items"]] == ["overseas"]
    assert body["category"] == "overseas"


def test_api_degrades_gracefully_when_fetch_raises(monkeypatch, tmp_path):
    """抓取异常时必须返回 available=false，且不向调用方抛错。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "news2.db")

    def boom():
        raise RuntimeError("网络不可达")

    monkeypatch.setattr(news, "_build_feed", boom)
    resp = client.get("/api/news")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["items"] == []
    assert body["message"] == "资讯暂不可用"


def test_api_uses_cache_after_first_success(monkeypatch, tmp_path):
    """首次抓取成功后写入缓存，第二次请求不再触发抓取。"""
    calls = {"n": 0}
    _patch_feed(monkeypatch, tmp_path, _SAMPLE)

    def counted():
        calls["n"] += 1
        return dict(_SAMPLE)

    monkeypatch.setattr(news, "_build_feed", counted)
    client.get("/api/news")
    client.get("/api/news")
    assert calls["n"] == 1
    # 缓存确实落库
    with db.get_conn() as conn:
        assert db.meta_get(conn, config.NEWS_CACHE_KEY) is not None

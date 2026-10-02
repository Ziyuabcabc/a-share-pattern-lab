# -*- coding: utf-8 -*-
"""文档口径一致性测试：AI 辅助开发说明。

背景：README 顶部需要如实说明「规则与设计由作者独立完成、代码实现借助 AI 辅助、
全部逻辑经作者核验」，且技术报告须与之一致。此前两份文档均无此说明，外部读者会默认
全部代码为手写，与实际情况不符。

本测试把这份口径**锁进仓库**，防止后续改动（例如重写 README 头部、调整报告前言）
时其中一处被删掉而另一处保留，造成口径不一致。

断言四件事：
1. README 的 AI 辅助说明位于**免责声明之前**（保证读者先看到）；
2. README 同时包含「独立设计」「AI 工具辅助」「核验」三个要素；
3. 技术报告含同一口径的说明，且两份文档共用同一组要素词；
4. 不出现「全部由本人手写」这类与实际不符的反向表述。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
REPORT = ROOT / "docs" / "project_report.md"

# 口径要素：三份文档必须共有的关键表述
KEY_INDEPENDENT = "独立设计"
KEY_AI = "AI 工具辅助"
KEY_VERIFY = "核验"


@pytest.fixture(scope="module")
def readme_text() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def report_text() -> str:
    return REPORT.read_text(encoding="utf-8")


def test_readme_notes_before_disclaimer(readme_text: str) -> None:
    """AI 辅助说明必须排在免责声明之前，读者第一屏即可见。"""
    i_notes = readme_text.find("关于 AI 辅助开发")
    i_disc = readme_text.find("免责声明（必读）")
    assert i_notes != -1, "README 缺少「关于 AI 辅助开发」说明块"
    assert i_disc != -1, "README 缺少顶部免责声明"
    assert i_notes < i_disc, "AI 辅助说明应位于免责声明之前"


def test_readme_has_all_elements(readme_text: str) -> None:
    """三个要素齐全：独立设计 + AI 辅助 + 人工核验。"""
    head = readme_text[:2000]
    for key in (KEY_INDEPENDENT, KEY_AI, KEY_VERIFY):
        assert key in head, f"README 顶部缺少要素：{key}"
    # 覆盖范围应包含规则 / 设计 / 代码实现三类
    for scope in ("打分规则", "代码实现"):
        assert scope in head, f"README 顶部未说明 {scope} 的归属"


def test_readme_asks_not_to_copy(readme_text: str) -> None:
    """README 需含「请勿直接复刻作为个人作品集」的说明。"""
    assert "不要直接复刻" in readme_text


def test_report_keeps_same_wording(report_text: str) -> None:
    """技术报告须与 README 口径一致，且写明 AI 是辅助角色。"""
    head = report_text[:2500]
    assert KEY_INDEPENDENT in head, "技术报告缺少「独立设计」说明"
    assert "AI" in head, "技术报告缺少 AI 使用说明"
    assert "辅助编码工具" in head, "技术报告应写明 AI 为辅助编码工具"
    assert "校验" in head or KEY_VERIFY in head, "技术报告应写明规则由作者校验"


def test_no_contradictory_claim(report_text: str, readme_text: str) -> None:
    """不得在说明所在区域出现与实际不符的反向表述（会与 AI 说明自相矛盾）。

    只检查文档头部：变更日志等后文可能出于「引述被禁止的表述」而出现这些字串，
    那不是矛盾声明，不应误伤。
    """
    for text, name in ((readme_text[:2500], "README 头部"), (report_text[:2500], "技术报告头部")):
        for bad in ("全部由本人手写", "全部手写", "无任何 AI", "未使用任何 AI"):
            assert bad not in text, f"{name} 出现矛盾表述：{bad}"

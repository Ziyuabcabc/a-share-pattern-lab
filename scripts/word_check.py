#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合规自检脚本：检查项目自有文件中是否出现证券咨询类表述。

用法：
    python scripts/word_check.py

- 检查范围：所有源码、文档、配置（排除第三方库 vendor 目录与本地数据）
- 命中即返回非零退出码，可用于 CI 或提交前自检
- 词表以拆分片段存储（见 _BANNED_PARTS），避免本脚本自命中，
  完整词表说明见 CONTRIBUTING.md 的措辞规范
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 检查的文本文件后缀
CHECK_SUFFIXES = {".py", ".md", ".html", ".css", ".js", ".txt", ".toml", ".cfg", ".yml", ".yaml"}

# 第三方库与本地数据不检查（其内容与本工具文案无关）
EXCLUDE_DIRS = {"vendor", "node_modules", ".git", "__pycache__", ".pytest_cache", "data", "output"}

# 词表以「前半 + 后半」片段拆分存储，避免本文件出现完整禁用词而被自检命中。
# 涵盖：方向指示类、价格承诺类、概率承诺类、荐股类、交易导向类常见表述（中文与英文）。
_BANNED_PARTS = [
    ["买", "入"],
    ["卖", "出"],
    ["推", "荐"],
    ["抄", "底"],
    ["逃", "顶"],
    ["目标", "价"],
    ["进", "场"],
    ["信", "号"],
    ["标", "的"],
    ["sig", "nal"],
    ["reco", "mmend"],
    ["target pr", "ice"],
]


def iter_files():
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in CHECK_SUFFIXES:
            continue
        if any(part in EXCLUDE_DIRS for part in path.relative_to(PROJECT_ROOT).parts):
            continue
        if path.name == Path(__file__).name:
            continue
        yield path


def main() -> int:
    words = ["".join(chunks) for chunks in _BANNED_PARTS]
    hits: list[tuple[Path, int, str]] = []
    for path in iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for word in words:
            for line_no, line in enumerate(text.splitlines(), start=1):
                if word in line or word in line.lower():
                    rel = path.relative_to(PROJECT_ROOT)
                    hits.append((rel, line_no, line.strip()[:80]))

    if hits:
        print("发现不合规表述：")
        for rel, line_no, line in hits:
            print(f"  {rel}:{line_no}: {line}")
        print(f"\n共 {len(hits)} 处。请修改为中性表述（见 CONTRIBUTING.md 措辞规范）。")
        return 1

    print("合规自检通过：未发现证券咨询类表述。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

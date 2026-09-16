#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把研究简报 HTML 渲染为 PDF（v1.6.0）。

用法：
    python scripts/make_pdf.py <input.html> <output.pdf> [--timeout 240]

设计要点
--------
- 用**独立子进程**运行 Playwright，避免在 FastAPI 事件循环里执行同步浏览器 API；
- 浏览器优先使用本机已安装的 Edge / Chrome（无需下载 chromium 内核，离线可用）；
- 以 print 媒体查询渲染，保证分页、页边距与背景色正确；
- 失败时返回非零退出码并把错误写 stderr，由调用方决定降级方式。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 本机常见浏览器安装位置（按优先级）
_BROWSER_CANDIDATES = (
    ("msedge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ("msedge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ("chrome", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


def _launch(pw):
    """依次尝试本机浏览器，返回可用的 browser 实例。"""
    errors = []
    for channel, exe in _BROWSER_CANDIDATES:
        if not Path(exe).exists():
            continue
        try:
            return pw.chromium.launch(channel=channel, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001 —— 逐个退化尝试
            errors.append(f"channel={channel} ({exe}): {exc}")
    # 最后退回 Playwright 自带内核（若已下载）
    try:
        return pw.chromium.launch(args=["--no-sandbox"])
    except Exception as exc:  # noqa: BLE001
        errors.append(f"bundled chromium: {exc}")
    raise RuntimeError("无法启动任何可用浏览器：\n  " + "\n  ".join(errors))


def main() -> int:
    ap = argparse.ArgumentParser(description="HTML 简报转 PDF")
    ap.add_argument("html", help="输入 HTML 文件路径")
    ap.add_argument("pdf", help="输出 PDF 文件路径")
    ap.add_argument("--timeout", type=int, default=240, help="整体超时（秒）")
    args = ap.parse_args()

    src = Path(args.html).resolve()
    dst = Path(args.pdf).resolve()
    if not src.exists():
        print(f"输入文件不存在：{src}", file=sys.stderr)
        return 2
    dst.parent.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(f"未安装 playwright：{exc}", file=sys.stderr)
        return 3

    try:
        with sync_playwright() as pw:
            browser = _launch(pw)
            page = browser.new_page(viewport={"width": 900, "height": 1200})
            page.goto(src.as_uri(), wait_until="load", timeout=args.timeout * 1000)
            page.wait_for_timeout(700)          # 等字体与 SVG 布局稳定
            page.emulate_media(media="print")
            page.pdf(
                path=str(dst),
                format="A4",
                print_background=True,
                prefer_css_page_size=True,
                display_header_footer=True,
                header_template="<div></div>",
                footer_template=(
                    '<div style="width:100%;font-size:8px;color:#8a8f98;'
                    'font-family:-apple-system,Segoe UI,sans-serif;'
                    'padding:0 14mm;display:flex;justify-content:space-between;">'
                    '<span>A 股历史形态匹配研究看板 · 研究简报</span>'
                    '<span class="pageNumber"></span>/<span class="totalPages"></span>'
                    "</div>"
                ),
                margin={"top": "14mm", "bottom": "16mm", "left": "13mm", "right": "13mm"},
            )
            browser.close()
    except Exception as exc:  # noqa: BLE001
        print(f"PDF 渲染失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not dst.exists() or dst.stat().st_size < 1024:
        print("PDF 生成异常：文件缺失或过小", file=sys.stderr)
        return 1
    print(f"OK {dst} ({dst.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

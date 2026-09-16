# -*- coding: utf-8 -*-
"""研究简报接口（v1.6.0）。

接口一览：
- GET /api/report/data    结构化简报数据（JSON，供前端预览与二次利用）
- GET /api/report/html    自包含 HTML 简报（浏览器直接预览 / 打印）
- GET /api/report/export  PDF 下载（本机浏览器渲染，子进程执行）

全部数据读自本地数据库，与看板实时同步，保证结论可复现。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from app import config, db, report

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/report", tags=["report"])

# PDF 导出串行化：同一时刻只允许一个渲染任务，避免并发抢占浏览器
_pdf_lock = threading.Lock()
_PDF_KEEP = 6      # 保留最近若干份导出文件


def _build(scan_run_id: int | None, bt_run_id: int | None) -> dict:
    conn = db.connect()
    try:
        return report.collect(conn, scan_run_id=scan_run_id, bt_run_id=bt_run_id)
    finally:
        conn.close()


@router.get("/data")
def report_data(
    scan_run_id: int | None = Query(default=None, description="指定扫描批次"),
    bt_run_id: int | None = Query(default=None, description="指定回测批次"),
):
    """结构化简报数据。"""
    try:
        return _build(scan_run_id, bt_run_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("生成简报数据失败")
        raise HTTPException(status_code=500, detail=f"生成简报数据失败: {exc}") from exc


@router.get("/html", response_class=HTMLResponse)
def report_html(
    scan_run_id: int | None = Query(default=None),
    bt_run_id: int | None = Query(default=None),
    download: int = Query(default=0, description="1 时以附件形式下载"),
):
    """自包含 HTML 简报（含行内 SVG 图表，离线可预览、可直接打印为 PDF）。"""
    try:
        data = _build(scan_run_id, bt_run_id)
        body = report.render_html(data)
    except Exception as exc:  # noqa: BLE001
        logger.exception("生成简报 HTML 失败")
        raise HTTPException(status_code=500, detail=f"生成简报 HTML 失败: {exc}") from exc

    headers = {"Cache-Control": "no-store"}
    if download:
        fname = f"research-note-{datetime.now().strftime('%Y%m%d-%H%M')}.html"
        headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return HTMLResponse(content=body, headers=headers)


def _prune_old_pdfs(keep: int = _PDF_KEEP) -> None:
    """保留最近若干份导出文件，其余清理（导出目录不无限增长）。"""
    try:
        files = sorted(config.OUTPUT_DIR.glob("research-note-*.pdf"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[keep:]:
            p.unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001
        logger.warning("清理历史导出文件失败：%s", exc)


@router.get("/export")
def report_export(
    scan_run_id: int | None = Query(default=None),
    bt_run_id: int | None = Query(default=None),
):
    """导出 PDF：先落 HTML，再用本机浏览器子进程渲染。"""
    try:
        data = _build(scan_run_id, bt_run_id)
        body = report.render_html(data)
    except Exception as exc:  # noqa: BLE001
        logger.exception("生成简报 HTML 失败")
        raise HTTPException(status_code=500, detail=f"生成简报 HTML 失败: {exc}") from exc

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    html_path = config.OUTPUT_DIR / f"research-note-{ts}.html"
    pdf_path = config.OUTPUT_DIR / f"research-note-{ts}.pdf"
    html_path.write_text(body, encoding="utf-8")

    script = config.PROJECT_ROOT / "scripts" / "make_pdf.py"
    acquired = _pdf_lock.acquire(timeout=config.REPORT_PDF_TIMEOUT_SEC)
    if not acquired:
        raise HTTPException(status_code=503, detail="已有导出任务正在执行，请稍后重试")
    try:
        proc = subprocess.run(
            [sys.executable, str(script), str(html_path), str(pdf_path),
             "--timeout", str(config.REPORT_PDF_TIMEOUT_SEC)],
            capture_output=True, text=True, timeout=config.REPORT_PDF_TIMEOUT_SEC + 30,
            cwd=str(config.PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="PDF 渲染超时，请稍后重试") from exc
    finally:
        _pdf_lock.release()

    if proc.returncode != 0 or not pdf_path.exists():
        # 渲染失败时降级：保留 HTML，告知调用方可改用「下载 HTML」再自行打印
        detail = (proc.stderr or proc.stdout or "未知错误").strip()[:400]
        logger.error("PDF 渲染失败：%s", detail)
        raise HTTPException(
            status_code=500,
            detail=f"PDF 渲染失败（可改用 HTML 预览后自行打印为 PDF）：{detail}",
        )

    _prune_old_pdfs()
    fname = f"research-note-{datetime.now().strftime('%Y%m%d')}.pdf"
    return Response(
        content=pdf_path.read_bytes(),
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f"attachment; filename=\"{fname}\"; filename*=UTF-8''{quote(fname)}",
        },
    )

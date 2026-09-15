# -*- coding: utf-8 -*-
"""FastAPI 应用入口：A股历史形态匹配研究看板（本地运行版）。

本服务仅用于个人对 A 股历史公开数据做统计研究，所有输出均为
历史统计结果与指标展示，不构成任何形式的操作指引。

仅限本地部署运行（默认 127.0.0.1），请勿将本服务暴露到公网。
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import API_HOST, API_PORT, PROJECT_ROOT
from app.routers import market, pool, scan_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(
    title="A-Share Pattern Lab API",
    description=(
        "A股历史形态匹配研究看板（本地运行版）。"
        "仅提供历史数据统计、技术指标计算与形态特征匹配度打分。"
        "所有数据与分数均为历史统计结果，不构成任何操作指引；"
        "历史数据不等于未来表现。"
    ),
    version="0.1.0",
)

# 仅允许本机访问（本地看板页面跨域调用）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://127.0.0.1:{API_PORT}",
        f"http://localhost:{API_PORT}",
        "http://127.0.0.1:5500",  # 常见本地静态页预览端口
        "http://localhost:5500",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(pool.router)
app.include_router(scan_api.router)
app.include_router(market.router)

# 静态资源（前端看板页面与本地打包的图表库）
STATIC_DIR = PROJECT_ROOT / "app" / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    """本地看板页面入口（禁止缓存，避免升级后浏览器沿用旧版前端脚本）。"""
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/health")
def root():
    """服务健康检查。"""
    return {"service": "a-share-pattern-lab", "status": "running"}


def launch():
    """以本地模式启动服务（仅绑定 127.0.0.1）。"""
    import uvicorn

    uvicorn.run(app, host=API_HOST, port=API_PORT, log_level="info")


if __name__ == "__main__":
    launch()

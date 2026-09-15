# -*- coding: utf-8 -*-
"""FastAPI 应用入口：A股历史形态匹配研究看板（本地运行版）。

本服务仅用于个人对 A 股历史公开数据做统计研究，所有输出均为
历史统计结果与指标展示，不构成任何形式的操作指引。

仅限本地部署运行（默认 127.0.0.1），请勿将本服务暴露到公网。
"""

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import API_HOST, API_PORT, PROJECT_ROOT
from app.routers import market, news, pool, scan_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def _warm_index_cache() -> None:
    """后台预热宏观指数快照缓存（失败不影响主流程）。"""
    try:
        market.indices()
        logger.info("宏观指数快照缓存预热完成")
    except Exception as exc:  # noqa: BLE001
        logger.warning("宏观指数快照预热失败: %s", exc)


def _warm_news_cache() -> None:
    """后台预热市场资讯缓存（失败不影响主流程）。"""
    try:
        news.news()
        logger.info("市场资讯缓存预热完成")
    except Exception as exc:  # noqa: BLE001
        logger.warning("市场资讯缓存预热失败: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期：启动时后台预热指数快照。

    指数行情源在本机环境下首次请求可能耗时数秒到数十秒；若等用户打开
    看板时才去拉，宏观面板会长时间停留在占位状态。启动时预热一次，
    用户打开页面即可命中缓存（预热在线程中执行，不阻塞服务启动）。
    """
    threading.Thread(target=_warm_index_cache, daemon=True).start()
    threading.Thread(target=_warm_news_cache, daemon=True).start()
    yield


app = FastAPI(
    title="A-Share Pattern Lab API",
    description=(
        "A股历史形态匹配研究看板（本地运行版）。"
        "仅提供历史数据统计、技术指标计算与形态特征匹配度打分。"
        "所有数据与分数均为历史统计结果，不构成任何操作指引；"
        "历史数据不等于未来表现。"
    ),
    version="0.1.0",
    lifespan=lifespan,
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
app.include_router(news.router)

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

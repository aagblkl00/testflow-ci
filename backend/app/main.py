from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import api_router
from .config import settings
from .db import init_db
from .seed import bootstrap_admin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("testflow")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    bootstrap_admin()
    logger.info("TestFlow CI 已启动：执行模式=%s 数据库=%s", settings.executor_mode, settings.database_url)
    yield


app = FastAPI(title="TestFlow CI", version="0.1.0-m1", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_origin_regex=r"https?://(?:localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

app.include_router(api_router)


@app.get("/api/ping")
def ping() -> JSONResponse:
    return JSONResponse({"pong": True})


# 生产构建产物存在时，同端口托管前端（开发期用 Vite 的 5173）
_frontend_dist = settings.project_root / "frontend" / "dist"
if _frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dist), html=True), name="frontend")


def run() -> None:  # pragma: no cover - 便捷入口
    import uvicorn

    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=8000)


__all__ = ["app", "run", "Path"]

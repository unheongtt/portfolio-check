"""持仓体检 PortfolioCheck 入口：装载种子数据、挂载 API 与静态前端。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .config import get_settings
from .db import get_db

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = get_db()
    counts = db.load_seed(settings.seed_dir)
    app.state.seed_counts = counts
    yield


app = FastAPI(
    title="持仓体检 PortfolioCheck — AI Native 组合风险诊断",
    description="个人投资者贴入持仓，1 分钟拿到组合的集中度/相关性/估值/波动/题材暴露风险体检报告。",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

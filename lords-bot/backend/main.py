from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# API routers
from api.routes_analysis import router as analysis_router
from api.routes_option_chain import router as option_router
from api.routes_signals import router as signals_router
from api.routes_profile import router as profile_router
from api.routes_funds import router as funds_router
from api.routes_trading_mode import router as trading_mode_router
from api.routes_dashboard import router as dashboard_router
from api.routes_trade import router as trade_router

# Core modules
from config import settings
from core.logger import configure_logging
from engine.scheduler import scheduler

# Initialize logging
configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application startup and shutdown events.
    """
    await scheduler.start()
    yield
    await scheduler.stop()


# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
for router in [
    option_router,
    analysis_router,
    signals_router,
    profile_router,
    funds_router,
    trading_mode_router,
    dashboard_router,
    trade_router,
]:
    app.include_router(router)

# Frontend static files
frontend_path = Path(settings.frontend_dir)

if frontend_path.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")


@app.get("/")
async def root() -> FileResponse:
    """
    Serve frontend dashboard.
    """
    return FileResponse(frontend_path / "index.html")


@app.get("/health")
async def health() -> dict:
    """
    Health check endpoint.
    """
    return {
        "status": "ok",
        "scheduler_running": scheduler.running,
        "interval_seconds": settings.scheduler_interval,
    }
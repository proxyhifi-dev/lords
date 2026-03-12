from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api.routes_analysis import router as analysis_router
from backend.api.routes_dashboard import router as dashboard_router
from backend.api.routes_funds import router as funds_router
from backend.api.routes_option_chain import router as option_chain_router
from backend.api.routes_profile import router as profile_router
from backend.api.routes_signals import router as signals_router
from backend.api.routes_trading_mode import router as trading_mode_router
from backend.config import settings
from backend.core.logger import configure_logging
from backend.engine.scheduler import scheduler

configure_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await scheduler.start()
    yield
    await scheduler.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

for router in [
    option_chain_router,
    analysis_router,
    signals_router,
    profile_router,
    funds_router,
    trading_mode_router,
    dashboard_router,
]:
    app.include_router(router)

frontend_path = Path(settings.frontend_dir)
app.mount('/static', StaticFiles(directory=str(frontend_path)), name='static')


@app.get('/')
async def root() -> FileResponse:
    return FileResponse(frontend_path / 'index.html')


@app.get('/health')
async def health() -> dict:
    return {'status': 'ok', 'scheduler_running': scheduler.running, 'interval_seconds': settings.scheduler_interval}

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.auth import require_api_key
from api.routes_analysis import router as analysis_router
from api.routes_dashboard import router as dashboard_router
from api.routes_funds import router as funds_router
from api.routes_option_chain import router as option_chain_router
from api.routes_profile import router as profile_router
from api.routes_signals import router as signals_router
from api.routes_trade import router as trade_router
from api.routes_trading_mode import router as trading_mode_router
from config import settings
from core.logger import configure_logging
from engine.scheduler import scheduler

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await scheduler.start()
    yield
    await scheduler.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
allowed_origins = ['http://localhost:8000', 'http://127.0.0.1:8000'] if settings.environment == 'dev' else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=['GET', 'POST'],
    allow_headers=['Content-Type', 'X-API-Key'],
)

secure_router_kwargs = {'prefix': '/api', 'dependencies': [Depends(require_api_key)]}
for route in [
    analysis_router,
    dashboard_router,
    funds_router,
    option_chain_router,
    profile_router,
    signals_router,
    trade_router,
    trading_mode_router,
]:
    app.include_router(route, **secure_router_kwargs)

frontend_path = Path(settings.frontend_dir)
if frontend_path.exists():
    app.mount('/static', StaticFiles(directory=str(frontend_path)), name='static')


@app.get('/')
async def root() -> FileResponse:
    return FileResponse(frontend_path / 'index.html')


@app.get('/health')
async def health() -> dict:
    return {
        'status': 'ok',
        'scheduler_running': scheduler.running,
        'interval_seconds': scheduler.interval_seconds,
        'trading_mode': scheduler.state.trading_mode,
        'system_status': scheduler.state.system_status,
    }

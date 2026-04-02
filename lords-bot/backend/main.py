from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from api.routes_analysis import router as analysis_router
from api.routes_dashboard import router as dashboard_router
from api.routes_funds import router as funds_router
from api.routes_option_chain import router as option_chain_router
from api.routes_profile import router as profile_router
from api.routes_signals import router as signals_router
from api.routes_trade import router as trade_router
from api.routes_trading_mode import router as mode_router
from config import settings
from engine.scheduler import scheduler

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / 'frontend'


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await scheduler.stop()


app = FastAPI(title='LORDS BOT', version='1.0.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

# Existing route modules now exposed under /api
for router in [
    dashboard_router,
    funds_router,
    profile_router,
    signals_router,
    option_chain_router,
    trade_router,
    mode_router,
    analysis_router,
]:
    app.include_router(router, prefix='/api')


class ManualTradePayload(BaseModel):
    symbol_name: str = Field(min_length=3)
    expiry_date: str
    strike_price: str
    option_type: str
    transaction_type: str = 'BUY'
    quantity: int = Field(gt=0)
    price: float | None = None


@app.get('/health')
@app.get('/api/health')
async def health() -> dict:
    return {'status': 'ok', 'bot_running': scheduler.running, 'mode': scheduler.state.trading_mode, 'env': settings.environment}


@app.get('/api/trades')
async def trades() -> dict:
    return {'trades': scheduler.trade_logger.load_trades()[-200:]}


@app.post('/api/start-bot')
async def start_bot() -> dict:
    await scheduler.start()
    return {'status': 'ok', 'running': scheduler.running}


@app.post('/api/stop-bot')
async def stop_bot() -> dict:
    await scheduler.stop()
    return {'status': 'ok', 'running': scheduler.running}


@app.post('/api/manual-trade')
async def manual_trade(payload: ManualTradePayload) -> dict:
    if scheduler.state.trading_mode == 'REAL' and not settings.enable_real_trading:
        raise HTTPException(status_code=403, detail='real_trading_disabled')
    body = {
        'exchange': settings.exchange,
        'symbolName': payload.symbol_name,
        'expiryDate': payload.expiry_date,
        'strikePrice': payload.strike_price,
        'optionType': payload.option_type.upper(),
        'transactionType': payload.transaction_type.upper(),
        'orderType': 'MARKET',
        'productType': 'MIS',
        'quantity': payload.quantity,
        'price': payload.price,
    }
    result = await scheduler.execution_engine.execute(body, scheduler.state.trading_mode)
    return {'status': 'ok', 'result': result}


if FRONTEND_DIR.exists():
    app.mount('/static', StaticFiles(directory=str(FRONTEND_DIR)), name='static')


@app.get('/')
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / 'index.html')

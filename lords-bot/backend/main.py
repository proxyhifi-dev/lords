from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cache import load_cached_snapshot
from config import runtime_state, settings, yaml_config
from models import TradingModeRequest
from paper_trading_engine import paper_trading_engine
from samco_client import samco_client
from scheduler import market_scheduler
from signals import signal_store
from utils import configure_logging, is_market_hours

configure_logging(Path(settings.log_file))


@asynccontextmanager
async def lifespan(_: FastAPI):
    await market_scheduler.start()
    yield
    await market_scheduler.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

frontend_path = Path(settings.frontend_dir)
app.mount('/static', StaticFiles(directory=str(frontend_path)), name='static')


@app.get('/')
async def root() -> FileResponse:
    return FileResponse(frontend_path / 'index.html')


@app.get('/option-chain')
async def option_chain() -> dict:
    rows = market_scheduler.latest_df.to_dict(orient='records') if not market_scheduler.latest_df.empty else []
    if not rows:
        rows = load_cached_snapshot().get('option_chain', [])
    return {'symbol': runtime_state.symbol, 'expiry': runtime_state.expiry, 'data': rows}


@app.get('/analysis')
async def analysis() -> dict:
    if market_scheduler.latest_analysis is None:
        return {'message': 'Waiting for market data...'}
    return market_scheduler.latest_analysis.model_dump()


@app.get('/signals')
async def signals() -> dict:
    latest = signal_store.get()
    return latest.model_dump() if latest else {'signal': 'NO TRADE', 'confidence': 0.0, 'reason': 'Waiting for market data...'}


@app.get('/support-resistance')
async def support_resistance() -> dict:
    analysis = market_scheduler.latest_analysis
    if analysis is None:
        return {'support': None, 'resistance': None, 'atm_strike': None}
    return {'support': analysis.support, 'resistance': analysis.resistance, 'atm_strike': analysis.atm_strike}


@app.get('/profile')
async def profile() -> dict:
    return await samco_client.get_profile()


@app.get('/funds')
async def funds() -> dict:
    return await samco_client.get_funds()


@app.get('/orders')
async def orders() -> dict:
    return await samco_client.get_orders()


@app.get('/paper-trades')
async def paper_trades() -> list[dict]:
    return paper_trading_engine.get_trades()


@app.get('/paper-pnl')
async def paper_pnl() -> dict:
    return paper_trading_engine.get_pnl(market_scheduler.latest_df).model_dump()


@app.get('/underlying-price')
async def underlying_price() -> dict:
    price = market_scheduler.latest_underlying or await samco_client.get_underlying_price(runtime_state.symbol)
    return {'symbol': runtime_state.symbol, 'underlying_price': price}


@app.get('/api-status')
async def api_status() -> dict:
    profile = await samco_client.get_profile()
    return {'status': 'connected' if profile.get('status') != 'fallback' else 'disconnected'}


@app.get('/trading-mode')
async def trading_mode() -> dict:
    return {'mode': runtime_state.trading_mode, 'real_trading_enabled': settings.enable_real_trading}


@app.post('/trading-mode')
async def set_trading_mode(request: TradingModeRequest) -> dict:
    if request.mode == 'REAL' and not settings.enable_real_trading:
        raise HTTPException(status_code=403, detail='ENABLE_REAL_TRADING is false')
    runtime_state.trading_mode = request.mode
    return {'mode': runtime_state.trading_mode}


@app.get('/health')
async def health() -> dict:
    api = await samco_client.get_profile()
    return {
        'status': 'ok',
        'scheduler': 'running' if market_scheduler.running else 'stopped',
        'api': 'connected' if api.get('status') != 'fallback' else 'disconnected',
        'market_hours': is_market_hours(),
        'interval_seconds': yaml_config.scheduler_interval,
    }

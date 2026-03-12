from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import runtime_state, settings
from paper_trading_engine import paper_trading_engine
from scheduler import market_scheduler
from signals import signal_store


@asynccontextmanager
async def lifespan(_: FastAPI):
    await market_scheduler.start()
    yield
    await market_scheduler.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

frontend_path = (Path(__file__).resolve().parent / settings.frontend_dir).resolve()
app.mount('/static', StaticFiles(directory=str(frontend_path)), name='static')


@app.get('/')
async def root() -> FileResponse:
    return FileResponse(frontend_path / 'index.html')


@app.get('/option-chain')
async def option_chain() -> dict:
    return {
        'symbol': runtime_state.symbol,
        'expiry': runtime_state.expiry,
        'data': market_scheduler.latest_df.to_dict(orient='records'),
    }


@app.get('/analysis')
async def analysis() -> dict:
    if market_scheduler.latest_analysis is None:
        return {'message': 'analysis unavailable'}
    return market_scheduler.latest_analysis.model_dump()


@app.get('/signals')
async def signals() -> dict:
    latest = signal_store.get()
    return latest.model_dump() if latest else {'signal': 'NO TRADE', 'confidence': 0.0, 'reason': 'warming up'}


@app.get('/support-resistance')
async def support_resistance() -> dict:
    analysis = market_scheduler.latest_analysis
    if analysis is None:
        return {'support': None, 'resistance': None}
    return {'support': analysis.support, 'resistance': analysis.resistance, 'atm_strike': analysis.atm_strike}


@app.get('/paper-trades')
async def paper_trades() -> list[dict]:
    return paper_trading_engine.get_trades()


@app.get('/paper-pnl')
async def paper_pnl() -> dict:
    return paper_trading_engine.get_pnl(market_scheduler.latest_df).model_dump()

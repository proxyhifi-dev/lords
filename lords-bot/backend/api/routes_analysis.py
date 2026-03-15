from __future__ import annotations

from fastapi import APIRouter

from main_dependencies import backtester, market_data_service, scheduler

router = APIRouter(tags=['analysis'])


@router.get('/analysis/backtest')
async def backtest() -> dict:
    candles = await market_data_service.get_historical_candles('NIFTY', limit=120)
    orb = scheduler.candle_builder.opening_range(candles)
    return backtester.run(candles, orb['high'], orb['low'])


@router.get('/analysis/state')
async def state() -> dict:
    return scheduler.state.to_dict()

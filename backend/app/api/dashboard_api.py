from __future__ import annotations

from fastapi import APIRouter
from backend.app.engine.state_manager import StateManager


def build_dashboard_router(state_manager: StateManager) -> APIRouter:

    router = APIRouter()

    @router.get("/api/dashboard")
    async def dashboard() -> dict:

        state = await state_manager.snapshot()

        return {
            "bot_running":     state.bot_running,
            "trading_enabled": state.trading_enabled,
            "spot_price":      state.spot_price,
            "orb_high":        state.orb_high,
            "orb_low":         state.orb_low,
            "signal":          state.signal,
            "active_trade":    state.active_trade,
            "daily_pnl":       round(state.daily_pnl, 2),
            "live_pnl":        round(state.live_pnl, 2),
            "trade_count":     state.trade_count,
        }

    return router

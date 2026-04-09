from __future__ import annotations

from fastapi import APIRouter
from backend.app.engine.state_manager import StateManager


def build_dashboard_router(state_manager: StateManager) -> APIRouter:

    router = APIRouter()

    @router.get("/api/dashboard")
    async def dashboard() -> dict:

        state = await state_manager.snapshot()

        return {
            "spot_price": state.spot_price,
            "orb_high": state.orb_high,
            "orb_low": state.orb_low,
            "signal": state.signal,
            "active_trade": state.active_trade,
            "daily_pnl": state.daily_pnl,
            "live_pnl": state.live_pnl
        }

    return router
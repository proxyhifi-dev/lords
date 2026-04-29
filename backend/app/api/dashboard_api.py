from __future__ import annotations

import os
from fastapi import APIRouter
from backend.app.broker.samco_client import SamcoClient
from backend.app.engine.state_manager import StateManager


def build_dashboard_router(state_manager: StateManager, broker: SamcoClient | None = None) -> APIRouter:

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

    @router.post("/api/kill-switch")
    async def kill_switch() -> dict:
        os.environ["TRADING_KILL_SWITCH"] = "1"
        await state_manager.update(trading_enabled=False, last_risk_breach="manual_kill_switch")
        if broker:
            await broker.cancel_all_open_orders()
            await broker.close_all_positions_market()
        return {"ok": True, "trading_enabled": False, "timestamp": state.last_updated if (state := await state_manager.snapshot()) else None}

    return router

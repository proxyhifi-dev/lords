from __future__ import annotations

import os
from fastapi import APIRouter
from backend.app.broker.samco_client import SamcoClient
from backend.app.engine.state_manager import StateManager
from backend.app.storage.trade_store import TradeStore


def build_dashboard_router(
    state_manager: StateManager, 
    broker: SamcoClient | None = None,
    trade_store: TradeStore | None = None  # 🔥 Added TradeStore injection
) -> APIRouter:

    router = APIRouter()

    @router.get("/api/dashboard")
    async def dashboard() -> dict:

        state = await state_manager.snapshot()
        
        # 🔥 Fetch trade history for the UI table and PnL graph
        recent_trades = trade_store.get_all_trades() if trade_store else []

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
            "trades":          recent_trades,  # ✅ Frontend will use this for the chart/table
        }

    @router.post("/api/kill-switch")
    async def kill_switch() -> dict:
        os.environ["TRADING_KILL_SWITCH"] = "1"
        await state_manager.update(trading_enabled=False, last_risk_breach="manual_kill_switch")
        
        if broker:
            try:
                await broker.cancel_all_open_orders()
                # Assuming your SamcoClient has this method. If not, it will just pass gracefully.
                if hasattr(broker, "close_all_positions_market"):
                    await broker.close_all_positions_market()
            except Exception as e:
                pass # In production, log this error
                
        # ✅ Cleaned up state fetch for the return payload
        state = await state_manager.snapshot()
        
        return {
            "ok": True, 
            "trading_enabled": False, 
            "timestamp": state.last_updated
        }

    return router
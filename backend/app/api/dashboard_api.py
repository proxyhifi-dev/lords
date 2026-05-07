from __future__ import annotations

import os
from datetime import datetime, time
from zoneinfo import ZoneInfo
from fastapi import APIRouter
from backend.app.broker.samco_client import SamcoClient
from backend.app.engine.state_manager import StateManager
from backend.app.storage.trade_store import TradeStore


def build_dashboard_router(
    state_manager: StateManager, 
    broker: SamcoClient | None = None,
    trade_store: TradeStore | None = None,  # 🔥 Added TradeStore injection
    trading_engine=None  # Add trading_engine parameter
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

    @router.get("/api/iron-condor/stats")
    async def get_iron_condor_stats():
        """Get Iron Condor cycle statistics and active position details."""
        
        if not trading_engine or not trading_engine.iron_condor_strategy:
            return {
                "status": "disabled",
                "message": "Iron Condor strategy not enabled"
            }
        
        state = await state_manager.snapshot()
        IST = ZoneInfo("Asia/Kolkata")
        current_time = datetime.now(IST)
        
        # Check if IC position active
        if not state.active_trade or state.active_trade.get('strategy') != 'IRON_CONDOR':
            return {
                "status": "inactive",
                "last_cycle_month": state.last_iron_condor_month,
                "next_entry_days": get_days_until_next_entry(),
                "current_time": current_time.isoformat(),
            }
        
        trade = state.active_trade
        entry_time = datetime.fromisoformat(trade['entry_time'])
        
        # Calculate current premium
        current_prem = trading_engine.iron_condor_strategy.estimate_current_premium(
            trade['entry_price'],
            entry_time,
            current_time
        )
        
        # Calculate estimated P&L
        pnl_dict = trading_engine.iron_condor_strategy.compute_pnl(
            trade['entry_price'],
            current_prem,
            trade['qty']
        )
        
        # Calculate time metrics
        hours_elapsed = round((current_time - entry_time).total_seconds() / 3600, 1)
        until_theta_peak = get_mins_until(time(14, 0), current_time)
        until_eod = get_mins_until(time(15, 25), current_time)
        
        return {
            "status": "active",
            "entry_time": trade['entry_time'],
            "entry_premium": trade['entry_price'],
            "current_premium": round(current_prem, 2),
            "entry_strikes": trade['strike'],
            "hours_elapsed": hours_elapsed,
            "estimated_pnl": round(pnl_dict['net_pnl'], 2),
            "target_pnl": round(trade['entry_price'] * 0.50, 2),
            "stop_loss": round(trade['entry_price'] * 0.50, 2),  # 1.5x
            "until_theta_peak": until_theta_peak,
            "until_eod": until_eod,
            "current_time": current_time.isoformat(),
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


def get_days_until_next_entry() -> int:
    """Calculate days until next Iron Condor entry window."""
    from datetime import datetime
    from calendar import monthrange

    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    current_day = now.day
    
    # If we're past day 5, next entry is day 1 of next month
    if current_day > 5:
        # Days until end of month + 1
        _, days_in_month = monthrange(now.year, now.month)
        return days_in_month - current_day + 1
    elif current_day < 1:
        # Before day 1, still this month
        return 1 - current_day
    else:
        # Days 1-5, already in window
        return 0


def get_mins_until(target_time: time, current_time: datetime = None) -> int:
    """Calculate minutes until target time today."""
    if current_time is None:
        IST = ZoneInfo("Asia/Kolkata")
        current_time = datetime.now(IST)
    
    target_datetime = datetime.combine(current_time.date(), target_time, tzinfo=current_time.tzinfo)
    
    if target_datetime < current_time:
        # Target time already passed today
        return 0
    
    delta = target_datetime - current_time
    return int(delta.total_seconds() / 60)

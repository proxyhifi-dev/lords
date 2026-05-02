"""
Lords Bot — FastAPI Entry Point  v4.0
Run: uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
Dashboard: http://localhost:8000
"""
from __future__ import annotations
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.app.core.config_loader import get_settings
from backend.app.core.math_engine import full_analytics
from backend.app.utils.logger import configure_logging, get_logger

configure_logging()
logger   = get_logger("main")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from backend.app.core.startup_manager import startup_manager
    from backend.app.scheduler.market_scheduler import scheduler

    logger.info("Lords Bot v5.1 starting — mode=%s", settings.mode.upper())

    # CRITICAL: Perform safe startup synchronization
    logger.info("🔄 Performing safe startup synchronization...")
    startup_success = await startup_manager.perform_safe_startup()

    if not startup_success:
        logger.error("❌ Safe startup failed — refusing to start trading system")
        # In production, you might want to exit here
        # For development, we'll continue but log the failure
        if settings.is_live:
            logger.critical("🚨 LIVE MODE: Startup failed — system will not trade")
            # Could raise an exception here to prevent startup

    # Start scheduler regardless (for development), but mark trading as disabled if startup failed
    await scheduler.start()

    # If startup failed, disable trading
    if not startup_success:
        from backend.app.engine.state_manager import state_manager
        await state_manager.update(trading_enabled=False)
        logger.warning("⚠️ Trading disabled due to startup synchronization failure")

    yield

    logger.info("Lords Bot shutting down")
    await scheduler.stop()


app = FastAPI(title="Lords Bot", version="5.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

fp = Path(settings.frontend_dir)
if fp.exists():
    app.mount("/static", StaticFiles(directory=str(fp)), name="static")


@app.get("/")
async def root():
    idx = fp / "index.html"
    return FileResponse(idx) if idx.exists() else JSONResponse({"message": "Lords Bot API — frontend not found"})


@app.get("/health")
async def health():
    from backend.app.core.startup_manager import startup_manager
    from backend.app.scheduler.market_scheduler import scheduler

    startup_status = "synced" if startup_manager.sync_successful else "failed"

    return {
        "status": "ok",
        "version": "5.1.0",
        "startup_sync": startup_status,
        "scheduler_running": scheduler.running,
        "mode": settings.mode.upper(),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@app.get("/api/dashboard")
async def dashboard():
    from backend.app.scheduler.market_scheduler import scheduler
    try:
        state  = await scheduler.state.snapshot()
        trades = scheduler.trade_store.get_all_trades()
        return {
            "bot_running":     state.bot_running,
            "trading_mode":    state.trading_mode,
            "trading_enabled": state.trading_enabled,
            "nifty_spot":      state.spot_price,
            "orb_high":        state.orb_high,
            "orb_low":         state.orb_low,
            "signal":          state.signal,
            "active_trade":    state.active_trade,
            "daily_pnl":       round(state.daily_pnl, 2),
            "live_pnl":        round(state.live_pnl,  2),
            "trade_count":     state.trade_count,
            "trades":          trades[-50:],  # 🔥 FIXED HERE
            "timestamp":       datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/api/startup-status")
async def startup_status():
    """Get detailed startup synchronization status."""
    from backend.app.core.startup_manager import startup_manager

    positions = getattr(startup_manager, "broker_positions", []) or []
    orders = getattr(startup_manager, "broker_orders", []) or []

    def _to_dict(record: Any, keys: list[str]) -> dict[str, Any]:
        if isinstance(record, dict):
            return {k: record.get(k, None) for k in keys}
        return {k: getattr(record, k, None) for k in keys}

    return {
        "sync_successful": startup_manager.sync_successful,
        "broker_positions_count": len(positions),
        "broker_orders_count": len(orders),
        "positions": [
            _to_dict(pos, ["symbol", "quantity", "pnl"]) for pos in positions
        ],
        "open_orders": [
            _to_dict(order, ["symbol", "side", "quantity", "status"]) for order in orders
        ],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@app.get("/api/analytics")
async def analytics():
    """Advanced quant analytics for the current session."""
    from backend.app.scheduler.market_scheduler import scheduler
    try:
        trades = scheduler.trade_store.get_all_trades()
        pnl_series = [float(t.get("pnl", 0)) for t in trades if t.get("pnl")]
        a = full_analytics(pnl_series, capital=settings.capital)
        return {
            "total_trades":      a.total_trades,
            "win_rate":          a.win_rate,
            "gross_pnl":         a.gross_pnl,
            "net_pnl":           a.net_pnl,
            "avg_win":           a.avg_win,
            "avg_loss":          a.avg_loss,
            "profit_factor":     a.profit_factor,
            "reward_risk":       a.reward_risk,
            "sharpe_ratio":      a.sharpe,
            "sortino_ratio":     a.sortino,
            "max_drawdown":      a.max_drawdown,
            "max_drawdown_pct":  a.max_drawdown_pct,
            "calmar_ratio":      a.calmar_ratio,
            "kelly_fraction_pct": a.kelly_fraction,
            "half_kelly_pct":    a.half_kelly,
            "ev_per_trade":      a.ev_per_trade,
            "capital_min":       a.capital_min,
            "capital_recommended": a.capital_recommended,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/api/status")
async def status():
    from backend.app.scheduler.market_scheduler import scheduler
    state = await scheduler.state.snapshot()
    return {"bot_running": state.bot_running, "trading_mode": state.trading_mode,
            "trading_enabled": state.trading_enabled, "mode": settings.mode.upper(),
            "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/pnl")
async def pnl():
    from backend.app.scheduler.market_scheduler import scheduler
    state = await scheduler.state.snapshot()
    return {"daily_pnl": round(state.daily_pnl, 2),
            "live_pnl":  round(state.live_pnl,  2),
            "trade_count": state.trade_count}


@app.get("/api/trades")
async def trades():
    from backend.app.scheduler.market_scheduler import scheduler
    return {"trades": scheduler.trade_store.get_all_trades()}


@app.post("/api/start")
async def start():
    from backend.app.scheduler.market_scheduler import scheduler
    if scheduler.running: return {"status": "already_running"}
    await scheduler.start()
    return {"status": "started"}


@app.post("/api/stop")
async def stop():
    from backend.app.scheduler.market_scheduler import scheduler
    if not scheduler.running: return {"status": "already_stopped"}
    await scheduler.stop()
    return {"status": "stopped"}


@app.post("/api/trading-mode")
async def set_mode(body: dict):
    """
    MODE is controlled exclusively by .env MODE= setting.
    This endpoint can only set PAPER mode for safety.
    To enable LIVE mode: change MODE=live in .env and restart.
    """
    from backend.app.scheduler.market_scheduler import scheduler
    requested = body.get("mode", "PAPER").upper()
    if requested == "LIVE":
        # Live mode ONLY via .env — never via API
        return {
            "status": "error",
            "message": (
                "LIVE mode cannot be enabled via API for safety. "
                "Set MODE=live in .env and restart the bot."
            )
        }
    # Only PAPER is allowed via API
    await scheduler.state.update(trading_mode="PAPER")
    return {"status": "ok", "mode": "PAPER",
            "note": "To enable LIVE mode, set MODE=live in .env and restart"}


@app.post("/api/trading-enabled")
async def set_trading(body: dict):
    from backend.app.scheduler.market_scheduler import scheduler
    enabled = bool(body.get("enabled", True))
    await scheduler.state.update(trading_enabled=enabled)
    return {"status": "ok", "trading_enabled": enabled}


@app.post("/api/trade/flatten")
async def flatten():
    from backend.app.scheduler.market_scheduler import scheduler
    return await scheduler.flatten_position()


@app.get("/api/iron-condor/stats")
async def get_iron_condor_stats():
    """Get Iron Condor cycle statistics and active position details."""
    from backend.app.scheduler.market_scheduler import scheduler
    from datetime import datetime, time
    from zoneinfo import ZoneInfo
    
    trading_engine = scheduler.trading_engine
    state_manager = scheduler.state
    
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


def get_days_until_next_entry() -> int:
    """Calculate days until next Iron Condor entry window."""
    from datetime import datetime
    from calendar import monthrange
    
    now = datetime.now()
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
    from datetime import datetime
    from zoneinfo import ZoneInfo
    
    if current_time is None:
        IST = ZoneInfo("Asia/Kolkata")
        current_time = datetime.now(IST)
    
    target_datetime = datetime.combine(current_time.date(), target_time, tzinfo=current_time.tzinfo)
    
    if target_datetime < current_time:
        # Target time already passed today
        return 0
    
    delta = target_datetime - current_time
    return int(delta.total_seconds() / 60)
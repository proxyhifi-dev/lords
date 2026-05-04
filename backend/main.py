"""
Lords Bot — FastAPI Entry Point  v6.0 (Iron Condor)
Run: uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
Dashboard: http://localhost:8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

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

    logger.info("Lords Bot v6.0 starting — mode=%s strategy=%s",
                settings.mode.upper(), settings.strategy_type.upper())

    startup_success = await startup_manager.perform_safe_startup()

    if not startup_success:
        logger.error("❌ Safe startup failed — refusing to start trading system")
        if settings.is_live:
            logger.critical("🚨 LIVE MODE: Startup failed — system will not trade")

    await scheduler.start()

    if not startup_success:
        from backend.app.engine.state_manager import state_manager
        await state_manager.update(trading_enabled=False)
        logger.warning("⚠️ Trading disabled due to startup synchronization failure")

    yield

    logger.info("Lords Bot shutting down")
    await scheduler.stop()


app = FastAPI(title="Lords Bot", version="6.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

fp = Path(settings.frontend_dir)
if fp.exists():
    app.mount("/static", StaticFiles(directory=str(fp)), name="static")


@app.get("/")
async def root():
    idx = fp / "index.html"
    return FileResponse(idx) if idx.exists() else JSONResponse(
        {"message": "Lords Bot API — frontend not found"})


@app.get("/health")
async def health():
    from backend.app.core.startup_manager import startup_manager
    from backend.app.scheduler.market_scheduler import scheduler
    return {
        "status": "ok",
        "version": "6.0.0",
        "strategy": settings.strategy_type.upper(),
        "startup_sync": "synced" if startup_manager.sync_successful else "failed",
        "scheduler_running": scheduler.running,
        "mode": settings.mode.upper(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
            "signal":          state.signal,
            "active_trade":    state.active_trade,
            "daily_pnl":       round(state.daily_pnl, 2),
            "live_pnl":        round(state.live_pnl,  2),
            "trade_count":     state.trade_count,
            "last_ic_month":   state.last_iron_condor_month,
            "trades":          trades[-50:],
            "timestamp":       datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/api/startup-status")
async def startup_status():
    from backend.app.core.startup_manager import startup_manager

    positions = getattr(startup_manager, "broker_positions", []) or []
    orders    = getattr(startup_manager, "broker_orders", []) or []

    def _to_dict(record: Any, keys: list[str]) -> dict[str, Any]:
        if isinstance(record, dict):
            return {k: record.get(k) for k in keys}
        return {k: getattr(record, k, None) for k in keys}

    return {
        "sync_successful":        startup_manager.sync_successful,
        "broker_positions_count": len(positions),
        "broker_orders_count":    len(orders),
        "positions":   [_to_dict(p, ["symbol", "quantity", "pnl"]) for p in positions],
        "open_orders": [_to_dict(o, ["symbol", "side", "quantity", "status"]) for o in orders],
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/analytics")
async def analytics():
    from backend.app.scheduler.market_scheduler import scheduler
    try:
        trades = scheduler.trade_store.get_all_trades()
        pnl_series = [float(t.get("pnl", 0)) for t in trades if t.get("pnl")]
        a = full_analytics(pnl_series, capital=settings.capital)
        return {
            "total_trades":          a.total_trades,
            "win_rate":              a.win_rate,
            "gross_pnl":             a.gross_pnl,
            "net_pnl":               a.net_pnl,
            "avg_win":               a.avg_win,
            "avg_loss":              a.avg_loss,
            "profit_factor":         a.profit_factor,
            "reward_risk":           a.reward_risk,
            "sharpe_ratio":          a.sharpe,
            "sortino_ratio":         a.sortino,
            "max_drawdown":          a.max_drawdown,
            "max_drawdown_pct":      a.max_drawdown_pct,
            "calmar_ratio":          a.calmar_ratio,
            "kelly_fraction_pct":    a.kelly_fraction,
            "half_kelly_pct":        a.half_kelly,
            "ev_per_trade":          a.ev_per_trade,
            "capital_min":           a.capital_min,
            "capital_recommended":   a.capital_recommended,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/api/status")
async def status():
    from backend.app.scheduler.market_scheduler import scheduler
    state = await scheduler.state.snapshot()
    return {
        "bot_running":     state.bot_running,
        "trading_mode":    state.trading_mode,
        "trading_enabled": state.trading_enabled,
        "mode":            settings.mode.upper(),
        "strategy":        settings.strategy_type.upper(),
        "timestamp":       datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/pnl")
async def pnl():
    from backend.app.scheduler.market_scheduler import scheduler
    state = await scheduler.state.snapshot()
    return {
        "daily_pnl":   round(state.daily_pnl, 2),
        "live_pnl":    round(state.live_pnl,  2),
        "trade_count": state.trade_count,
    }


@app.get("/api/trades")
async def trades():
    from backend.app.scheduler.market_scheduler import scheduler
    return {"trades": scheduler.trade_store.get_all_trades()}


@app.post("/api/start")
async def start():
    from backend.app.scheduler.market_scheduler import scheduler
    if scheduler.running:
        return {"status": "already_running"}
    await scheduler.start()
    return {"status": "started"}


@app.post("/api/stop")
async def stop():
    from backend.app.scheduler.market_scheduler import scheduler
    if not scheduler.running:
        return {"status": "already_stopped"}
    await scheduler.stop()
    return {"status": "stopped"}


@app.post("/api/trading-mode")
async def set_mode(body: dict):
    """MODE is controlled by .env. API can only set PAPER for safety."""
    from backend.app.scheduler.market_scheduler import scheduler
    if body.get("mode", "").upper() == "LIVE":
        return {
            "status": "error",
            "message": "LIVE mode cannot be enabled via API. Set MODE=live in .env and restart.",
        }
    await scheduler.state.update(trading_mode="PAPER")
    return {"status": "ok", "mode": "PAPER"}


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


# ── Iron Condor stats endpoint ────────────────────────────────────────────────

@app.get("/api/iron-condor/stats")
async def get_iron_condor_stats():
    """Current Iron Condor position details and cycle info."""
    from backend.app.scheduler.market_scheduler import scheduler
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")

    # Fixed: scheduler.engine (not scheduler.trading_engine)
    engine = scheduler.engine
    if not engine or not engine.iron_condor_strategy:
        return {"status": "disabled", "message": "Iron Condor strategy not enabled"}

    state        = await scheduler.state.snapshot()
    current_time = datetime.now(IST)

    if not state.active_trade or state.active_trade.get("strategy") != "IRON_CONDOR":
        return {
            "status":           "inactive",
            "last_cycle_month": state.last_iron_condor_month,
            "next_entry_days":  _days_until_next_entry(),
            "current_time":     current_time.isoformat(),
        }

    trade      = state.active_trade
    entry_time = datetime.fromisoformat(trade["entry_time"])

    current_prem = engine.iron_condor_strategy.estimate_current_premium(
        trade["entry_price"], entry_time, current_time,
    )
    pnl_dict = engine.iron_condor_strategy.compute_pnl(
        trade["entry_price"], current_prem, trade["qty"],
    )
    hours_elapsed       = round((current_time - entry_time).total_seconds() / 3600, 1)
    until_theta_peak    = _mins_until(time(14, 0), current_time)
    until_eod           = _mins_until(time(15, 25), current_time)

    return {
        "status":           "active",
        "entry_time":       trade["entry_time"],
        "entry_premium":    trade["entry_price"],
        "current_premium":  round(current_prem, 2),
        "entry_strikes":    trade["strike"],
        "hours_elapsed":    hours_elapsed,
        "estimated_pnl":    round(pnl_dict["net_pnl"], 2),
        "target_pnl":       round(trade["entry_price"] * settings.ic_target_profit_pct, 2),
        "stop_loss_prem":   round(trade["entry_price"] * settings.ic_stop_loss_multiple, 2),
        "until_theta_peak": until_theta_peak,
        "until_eod":        until_eod,
        "current_time":     current_time.isoformat(),
    }


def _days_until_next_entry() -> int:
    from calendar import monthrange
    now = datetime.now()
    if now.day > settings.ic_entry_day_end:
        _, days_in_month = monthrange(now.year, now.month)
        return days_in_month - now.day + 1
    return max(0, settings.ic_entry_day_start - now.day)


def _mins_until(target_time: time, current_time: datetime) -> int:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    target_dt = datetime.combine(
        current_time.date(), target_time, tzinfo=IST,
    )
    if target_dt <= current_time:
        return 0
    return int((target_dt - current_time).total_seconds() / 60)
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
    from backend.app.scheduler.market_scheduler import scheduler
    logger.info("Lords Bot v4.0 starting — mode=%s", settings.mode.upper())
    await scheduler.start()
    yield
    logger.info("Lords Bot shutting down")
    await scheduler.stop()


app = FastAPI(title="Lords Bot", version="4.0.0", lifespan=lifespan)
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
    from backend.app.scheduler.market_scheduler import scheduler
    return {"status": "ok", "version": "4.0.0",
            "scheduler_running": scheduler.running, "mode": settings.mode.upper()}


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
            "trade_history":   trades[-50:],
            "timestamp":       datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


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

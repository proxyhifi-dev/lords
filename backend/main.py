from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, UTC

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# CONFIG
from backend.config import settings

# LOGGER
from backend.app.utils.logger import configure_logging

# SCHEDULER
from backend.app.scheduler.market_scheduler import scheduler


# -----------------------------
# INIT LOGGING
# -----------------------------
configure_logging()


# -----------------------------
# APP LIFESPAN
# -----------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting Lords Bot Scheduler...")
    await scheduler.start()
    yield
    print("🛑 Stopping Lords Bot Scheduler...")
    await scheduler.stop()


# -----------------------------
# FASTAPI APP
# -----------------------------
app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan
)


# -----------------------------
# CORS
# -----------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------
# FRONTEND
# -----------------------------
frontend_path = Path(settings.frontend_dir)

print("Frontend path:", frontend_path)

if frontend_path.exists():
    app.mount(
        "/static",
        StaticFiles(directory=str(frontend_path)),
        name="static"
    )


# -----------------------------
# ROOT
# -----------------------------
@app.get("/")
async def root():
    index_file = frontend_path / "index.html"

    if index_file.exists():
        return FileResponse(index_file)

    return {"message": "Lords Bot API running"}


# -----------------------------
# HEALTH
# -----------------------------
@app.get("/health")
async def health():

    return {
        "status": "ok",
        "scheduler_running": scheduler.running,
        "app": settings.app_name
    }


# -----------------------------
# DASHBOARD
# -----------------------------
@app.get("/api/dashboard")
async def dashboard():

    try:

        state = await scheduler.engine.state_manager.snapshot()

        trades = scheduler.engine.trade_store.get_all_trades()

        return {

            "bot_status": "RUNNING" if scheduler.running else "STOPPED",

            "trading_mode": getattr(
                scheduler.engine.state_manager,
                "trading_mode",
                "PAPER"
            ),

            "nifty_spot": state.spot_price,

            "orb_high": state.orb_high,
            "orb_low": state.orb_low,

            "signal": state.signal,

            "active_trade": state.active_trade,

            "daily_pnl": state.daily_pnl,

            "trade_count": state.trade_count,

            "trade_history": trades[-20:],   # last 20 trades

            "timestamp": datetime.now(UTC).isoformat()

        }

    except Exception as e:

        return {
            "status": "error",
            "message": str(e)
        }


# -----------------------------
# TRADING MODE
# -----------------------------
@app.post("/api/trading-mode")
async def trading_mode(mode: dict):

    value = mode.get("mode", "PAPER").upper()

    scheduler.engine.state_manager.trading_mode = value

    return {
        "status": "ok",
        "mode": value
    }


# -----------------------------
# FLATTEN POSITION
# -----------------------------
@app.post("/api/trade/flatten")
async def flatten():

    state = await scheduler.engine.state_manager.snapshot()

    if not state.active_trade:

        return {
            "status": "no_active_trade"
        }

    symbol = state.active_trade.get("symbol")

    qty = state.active_trade.get("order", {}).get("quantity", 0)

    try:

        await scheduler.engine.broker.place_order(
            symbol=symbol,
            side="SELL",
            quantity=qty
        )

        await scheduler.engine.state_manager.update(
            active_trade=None
        )

        return {
            "status": "flattened"
        }

    except Exception as e:

        return {
            "status": "error",
            "message": str(e)
        }
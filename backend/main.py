"""
Lords Bot — FastAPI Entry Point
Run: uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
Dashboard: http://localhost:8000
"""
from __future__ import annotations

import math
from contextlib import asynccontextmanager
from datetime import datetime, time, timezone, timedelta
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
logger = get_logger("main")
settings = get_settings()


def _safe_number(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return None
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return _safe_number(value)


def _safe_json_response(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=_json_safe(payload), status_code=status_code)


def _to_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _quote_price_for_leg(side: str, bid: float | None, ask: float | None, ltp: float | None) -> float:
    bid = float(bid or 0.0)
    ask = float(ask or 0.0)
    ltp = float(ltp or 0.0)
    if str(side).upper() == "SELL":
        return bid or ltp or ask or 0.0
    return ask or ltp or bid or 0.0


def _is_trade_closed(trade: dict[str, Any]) -> bool:
    status = _clean_text(trade.get("status")).upper()
    if status == "CLOSED":
        return True

    return any(
        trade.get(key) not in ("", None)
        for key in ("exit_time", "exit_reason", "reason", "exit_price", "exit_premium")
    )


def _is_trade_open(trade: dict[str, Any]) -> bool:
    status = _clean_text(trade.get("status")).upper()
    if status == "OPEN":
        return True
    return not _is_trade_closed(trade)


def _is_iron_condor_trade(trade: dict[str, Any]) -> bool:
    strategy = _clean_text(trade.get("strategy") or trade.get("signal")).upper()
    return strategy == "IRON_CONDOR"


def _get_dashboard_trade_counts(
    trades: list[dict[str, Any]],
    active_trade: dict[str, Any] | None,
) -> dict[str, int]:
    closed_trade_count = sum(
        1
        for trade in trades
        if _is_iron_condor_trade(trade) and _is_trade_closed(trade)
    )
    open_trade_count = 1 if active_trade and _is_iron_condor_trade(active_trade) else 0
    display_trade_count = closed_trade_count + open_trade_count

    return {
        "closed_trade_count": closed_trade_count,
        "active_trade_count": open_trade_count,
        "display_trade_count": display_trade_count,
    }


def _extract_pnl_series(trades: list[dict[str, Any]]) -> list[float]:
    pnl_series: list[float] = []
    for trade in trades:
        if not _is_trade_closed(trade):
            continue
        pnl_value = _to_float(trade.get("net_pnl"), None)
        if pnl_value is None:
            pnl_value = _to_float(trade.get("pnl"), None)
        if pnl_value is not None:
            pnl_series.append(pnl_value)
    return pnl_series


async def _get_live_iron_condor_snapshot(engine, trade: dict[str, Any]) -> dict[str, Any]:
    broker = getattr(engine, "broker", None)
    if broker is None:
        raise RuntimeError("Broker unavailable for live iron condor snapshot")

    legs = trade.get("legs") or []
    if not legs:
        raise RuntimeError("No legs found in active iron condor trade")

    current_legs: list[dict[str, Any]] = []
    current_premium = 0.0

    for leg in legs:
        symbol = leg.get("symbol")
        side = str(leg.get("side", "")).upper()
        if not symbol or side not in {"BUY", "SELL"}:
            raise RuntimeError(f"Invalid iron condor leg: {leg}")

        quote = await broker.get_quote(symbol_name=symbol, exchange="NFO")
        bid, ask = broker.parse_bid_ask(quote)
        ltp = broker.parse_ltp(quote)
        mark_price = _quote_price_for_leg(side, bid, ask, ltp)

        if mark_price <= 0:
            raise RuntimeError(
                f"Invalid live quote for {symbol}: bid={bid} ask={ask} ltp={ltp}"
            )

        if side == "SELL":
            current_premium += mark_price
        else:
            current_premium -= mark_price

        current_legs.append(
            {
                "name": leg.get("name"),
                "symbol": symbol,
                "display_symbol": leg.get("display_symbol") or symbol,
                "side": side,
                "entry_price": leg.get("entry_price"),
                "entry_bid": leg.get("entry_bid"),
                "entry_ask": leg.get("entry_ask"),
                "entry_ltp": leg.get("entry_ltp"),
                "current_price": round(mark_price, 2),
                "current_bid": round(float(bid or 0.0), 2),
                "current_ask": round(float(ask or 0.0), 2),
                "current_ltp": round(float(ltp or 0.0), 2),
                "price_source": "broker_quote_snapshot",
            }
        )

    return {
        "current_premium": round(current_premium, 2),
        "current_legs": current_legs,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    from backend.app.core.startup_manager import startup_manager
    from backend.app.scheduler.market_scheduler import scheduler

    logger.info(
        "Lords Bot starting — mode=%s strategy=%s",
        settings.mode.upper(),
        settings.strategy_type.upper(),
    )

    startup_success = await startup_manager.perform_safe_startup()
    if not startup_success:
        logger.error("Safe startup failed")
        if getattr(settings, "is_live", False):
            logger.critical("LIVE MODE: startup failed — trading must stay disabled")

    await scheduler.start()

    if not startup_success:
        from backend.app.engine.state_manager import state_manager

        await state_manager.update(trading_enabled=False)
        logger.warning("Trading disabled due to startup synchronization failure")

    yield

    logger.info("Lords Bot shutting down")
    await scheduler.stop()


app = FastAPI(title="Lords Bot", version="6.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

frontend_path = Path(settings.frontend_dir)
if frontend_path.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")


@app.get("/")
async def root():
    index_file = frontend_path / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse({"message": "Lords Bot API — frontend not found"})


@app.get("/health")
async def health():
    from backend.app.core.startup_manager import startup_manager
    from backend.app.scheduler.market_scheduler import scheduler

    return _safe_json_response(
        {
            "status": "ok",
            "version": "6.0.0",
            "strategy": settings.strategy_type.upper(),
            "startup_sync": "synced" if startup_manager.sync_successful else "failed",
            "scheduler_running": scheduler.running,
            "mode": settings.mode.upper(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.get("/api/dashboard")
async def dashboard():
    from backend.app.scheduler.market_scheduler import scheduler

    try:
        state = await scheduler.state.snapshot()
        trades = scheduler.trade_store.get_all_trades()
        trade_counts = _get_dashboard_trade_counts(trades, state.active_trade)

        return _safe_json_response(
            {
                "bot_running": state.bot_running,
                "trading_mode": state.trading_mode,
                "trading_enabled": state.trading_enabled,
                "nifty_spot": state.spot_price,
                "signal": state.signal,
                "active_trade": state.active_trade,
                "daily_pnl": round(state.daily_pnl, 2),
                "live_pnl": round(state.live_pnl, 2),
                "trade_count": state.trade_count,
                "closed_trade_count": trade_counts["closed_trade_count"],
                "active_trade_count": trade_counts["active_trade_count"],
                "display_trade_count": trade_counts["display_trade_count"],
                "last_ic_month": getattr(state, "last_iron_condor_month", None),
                "trades": trades[-50:],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
    except Exception as exc:
        logger.exception("Dashboard endpoint failed")
        return _safe_json_response({"status": "error", "message": str(exc)}, status_code=500)


@app.get("/api/startup-status")
async def startup_status():
    from backend.app.core.startup_manager import startup_manager

    positions = getattr(startup_manager, "broker_positions", []) or []
    orders = getattr(startup_manager, "broker_orders", []) or []

    def _to_dict(record: Any, keys: list[str]) -> dict[str, Any]:
        if isinstance(record, dict):
            return {k: record.get(k) for k in keys}
        return {k: getattr(record, k, None) for k in keys}

    return _safe_json_response(
        {
            "sync_successful": startup_manager.sync_successful,
            "broker_positions_count": len(positions),
            "broker_orders_count": len(orders),
            "positions": [_to_dict(p, ["symbol", "quantity", "pnl"]) for p in positions],
            "open_orders": [_to_dict(o, ["symbol", "side", "quantity", "status"]) for o in orders],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.get("/api/analytics")
async def analytics():
    from backend.app.scheduler.market_scheduler import scheduler

    try:
        trades = scheduler.trade_store.get_all_trades()
        pnl_series = _extract_pnl_series(trades)
        analytics_result = full_analytics(pnl_series, capital=settings.capital)

        payload = {
            "total_trades": analytics_result.total_trades,
            "win_rate": analytics_result.win_rate,
            "gross_pnl": analytics_result.gross_pnl,
            "net_pnl": analytics_result.net_pnl,
            "avg_win": analytics_result.avg_win,
            "avg_loss": analytics_result.avg_loss,
            "profit_factor": analytics_result.profit_factor,
            "reward_risk": analytics_result.reward_risk,
            "sharpe_ratio": analytics_result.sharpe,
            "sortino_ratio": analytics_result.sortino,
            "max_drawdown": analytics_result.max_drawdown,
            "max_drawdown_pct": analytics_result.max_drawdown_pct,
            "calmar_ratio": analytics_result.calmar_ratio,
            "kelly_fraction_pct": analytics_result.kelly_fraction,
            "half_kelly_pct": analytics_result.half_kelly,
            "ev_per_trade": analytics_result.ev_per_trade,
            "capital_min": analytics_result.capital_min,
            "capital_recommended": analytics_result.capital_recommended,
        }
        return _safe_json_response(payload)
    except Exception as exc:
        logger.exception("Analytics endpoint failed")
        return _safe_json_response({"status": "error", "message": str(exc)}, status_code=500)


@app.get("/api/status")
async def status():
    from backend.app.scheduler.market_scheduler import scheduler

    state = await scheduler.state.snapshot()
    return _safe_json_response(
        {
            "bot_running": state.bot_running,
            "trading_mode": state.trading_mode,
            "trading_enabled": state.trading_enabled,
            "mode": settings.mode.upper(),
            "strategy": settings.strategy_type.upper(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.get("/api/pnl")
async def pnl():
    from backend.app.scheduler.market_scheduler import scheduler

    state = await scheduler.state.snapshot()
    return _safe_json_response(
        {
            "daily_pnl": round(state.daily_pnl, 2),
            "live_pnl": round(state.live_pnl, 2),
            "trade_count": state.trade_count,
        }
    )


@app.get("/api/trades")
async def trades():
    from backend.app.scheduler.market_scheduler import scheduler

    return _safe_json_response({"trades": scheduler.trade_store.get_all_trades()})


@app.post("/api/start")
async def start():
    from backend.app.scheduler.market_scheduler import scheduler

    if scheduler.running:
        return _safe_json_response({"status": "already_running"})
    await scheduler.start()
    return _safe_json_response({"status": "started"})


@app.post("/api/stop")
async def stop():
    from backend.app.scheduler.market_scheduler import scheduler

    if not scheduler.running:
        return _safe_json_response({"status": "already_stopped"})
    await scheduler.stop()
    return _safe_json_response({"status": "stopped"})


@app.post("/api/trading-mode")
async def set_mode(body: dict):
    from backend.app.scheduler.market_scheduler import scheduler

    if body.get("mode", "").upper() == "LIVE":
        return _safe_json_response(
            {
                "status": "error",
                "message": "LIVE mode cannot be enabled via API. Set MODE=live in .env and restart.",
            },
            status_code=400,
        )

    await scheduler.state.update(trading_mode="PAPER")
    return _safe_json_response({"status": "ok", "mode": "PAPER"})


@app.post("/api/trading-enabled")
async def set_trading(body: dict):
    from backend.app.scheduler.market_scheduler import scheduler

    enabled = bool(body.get("enabled", True))
    await scheduler.state.update(trading_enabled=enabled)
    return _safe_json_response({"status": "ok", "trading_enabled": enabled})


@app.post("/api/trade/flatten")
async def flatten():
    from backend.app.scheduler.market_scheduler import scheduler

    return _safe_json_response(await scheduler.flatten_position())


@app.get("/api/reconciliation")
async def reconciliation_status():
    from backend.app.scheduler.market_scheduler import scheduler

    result = await scheduler._reconciler.run_once()
    return _safe_json_response({"status": "ok", "reconciliation": result})


@app.post("/api/reconcile")
async def reconcile_now():
    from backend.app.scheduler.market_scheduler import scheduler

    result = await scheduler._reconciler.run_once()
    return _safe_json_response({"status": "ok", "reconciliation": result})


@app.post("/api/emergency-flatten")
async def emergency_flatten():
    from backend.app.scheduler.market_scheduler import scheduler

    flatten_result = await scheduler.flatten_position()
    reconcile_result = await scheduler._reconciler.run_once()
    return _safe_json_response(
        {
            "status": "ok",
            "flatten": flatten_result,
            "reconciliation": reconcile_result,
        }
    )


@app.get("/api/iron-condor/stats")
async def get_iron_condor_stats():
    from backend.app.scheduler.market_scheduler import scheduler
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    engine = scheduler.engine

    if not engine or not engine.iron_condor_strategy:
        return _safe_json_response({"status": "disabled", "message": "Iron Condor strategy not enabled"})

    state = await scheduler.state.snapshot()
    current_time = datetime.now(ist)

    if not state.active_trade or state.active_trade.get("strategy") != "IRON_CONDOR":
        return _safe_json_response(
            {
                "status": "inactive",
                "last_cycle_month": getattr(state, "last_iron_condor_month", None),
                "next_entry_days": _days_until_next_entry(),
                "current_time": current_time.isoformat(),
            }
        )

    trade = state.active_trade
    entry_time = datetime.fromisoformat(trade["entry_time"])
    hours_elapsed = round((current_time - entry_time).total_seconds() / 3600, 1)
    until_theta_peak = _mins_until(time(14, 0), current_time)
    until_eod = _mins_until(time(15, 25), current_time)

    try:
        snapshot = await _get_live_iron_condor_snapshot(engine, trade)
        current_premium = float(snapshot["current_premium"])
        current_legs = snapshot["current_legs"]
        premium_source = "broker_quote_snapshot"
    except Exception as exc:
        logger.warning("Live IC snapshot failed, falling back to model pricing: %s", exc)
        current_premium = engine.iron_condor_strategy.estimate_current_premium(
            trade["entry_price"],
            entry_time,
            current_time,
        )
        current_legs = []
        premium_source = "model_fallback"

    pnl_dict = engine.iron_condor_strategy.compute_pnl(
        float(trade["entry_price"]),
        float(current_premium),
        int(trade["qty"]),
    )

    return _safe_json_response(
        {
            "status": "active",
            "entry_time": trade["entry_time"],
            "entry_premium": round(float(trade["entry_price"]), 2),
            "current_premium": round(float(current_premium), 2),
            "entry_strikes": trade["strike"],
            "hours_elapsed": hours_elapsed,
            "estimated_pnl": round(float(pnl_dict["net_pnl"]), 2),
            "gross_pnl": round(float(pnl_dict["gross_pnl"]), 2),
            "charges": round(float(pnl_dict["total_charges"]), 2),
            "target_pnl": round(float(trade["entry_price"]) * settings.ic_target_profit_pct, 2),
            "stop_loss_prem": round(float(trade["entry_price"]) * settings.ic_stop_loss_multiple, 2),
            "until_theta_peak": until_theta_peak,
            "until_eod": until_eod,
            "current_time": current_time.isoformat(),
            "pricing_source": premium_source,
            "current_legs": current_legs,
        }
    )


def _days_until_next_entry() -> int:
    now = datetime.now()
    if bool(getattr(settings, "ic_monthly_only", False)):
        start_day = int(getattr(settings, "ic_entry_day_start", 1))
        end_day = int(getattr(settings, "ic_entry_day_end", 5))
        if start_day <= now.day <= end_day:
            return 0
        if now.day < start_day:
            return start_day - now.day
        next_month = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
        return (next_month.replace(day=start_day) - now).days
    return 0


def _mins_until(target_time: time, current_time: datetime) -> int:
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    target_dt = datetime.combine(current_time.date(), target_time, tzinfo=ist)
    if target_dt <= current_time:
        return 0
    return int((target_dt - current_time).total_seconds() / 60)
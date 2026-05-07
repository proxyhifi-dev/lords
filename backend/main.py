# backend/main.py
"""
Lords Bot — FastAPI Entry Point

Run:
    uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

Dashboard:
    http://localhost:8000
"""
from __future__ import annotations

import math
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
IST = ZoneInfo("Asia/Kolkata")


def _safe_number(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return _safe_number(value)


def _safe_json_response(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=_json_safe(payload), status_code=status_code)


def _clean_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _to_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default

    text = str(value).strip().replace(",", "")
    if not text:
        return default

    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return default

    return parsed if math.isfinite(parsed) else default


def _to_int(value: Any, default: int = 0) -> int:
    parsed = _to_float(value, None)
    if parsed is None:
        return default
    return int(parsed)


def _round_or_blank(value: Any) -> float | str:
    parsed = _to_float(value, None)
    if parsed is None:
        return ""
    return round(parsed, 2)


def _is_numeric_text(value: Any) -> bool:
    return _to_float(value, None) is not None


def _known_exit_reason(value: Any) -> bool:
    text = _clean_text(value).upper()
    if not text:
        return False

    return any(
        token in text
        for token in (
            "TARGET",
            "STOP",
            "STOP_LOSS",
            "SL",
            "EOD",
            "SQUAREOFF",
            "THETA",
            "EXIT",
            "TRAIL",
            "FORCED",
            "LOSS",
            "CLOSED",
            "EXPIRY",
        )
    )


def _looks_like_pricing_source(value: Any) -> bool:
    text = _clean_text(value).lower()
    return text in {
        "broker_quote_snapshot",
        "broker_quote_snapshot_cached",
        "broker_fill",
        "model_fallback",
        "paper",
        "paper_fill",
    }


def _quote_price_for_entry_leg(
    side: str,
    bid: float | None,
    ask: float | None,
    ltp: float | None,
) -> float:
    bid_value = float(bid or 0.0)
    ask_value = float(ask or 0.0)
    ltp_value = float(ltp or 0.0)

    if str(side).upper() == "SELL":
        return bid_value or ltp_value or ask_value or 0.0

    return ask_value or ltp_value or bid_value or 0.0


def _quote_price_for_close_leg(
    side: str,
    bid: float | None,
    ask: float | None,
    ltp: float | None,
) -> float:
    bid_value = float(bid or 0.0)
    ask_value = float(ask or 0.0)
    ltp_value = float(ltp or 0.0)

    if str(side).upper() == "SELL":
        return ask_value or ltp_value or bid_value or 0.0

    return bid_value or ltp_value or ask_value or 0.0


def _safe_cached_ic_snapshot(trade: dict[str, Any]) -> dict[str, Any] | None:
    current_premium = _to_float(trade.get("current_premium"), None)
    current_legs = trade.get("current_legs") or []

    if current_premium is None or current_premium <= 0:
        return None

    return {
        "current_premium": round(current_premium, 2),
        "current_legs": current_legs if isinstance(current_legs, list) else [],
        "pricing_source": trade.get("current_pricing_source")
        or "broker_quote_snapshot_cached",
    }


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


def _normalize_symbol_for_dashboard(trade: dict[str, Any]) -> str:
    strategy = _clean_text(trade.get("strategy") or trade.get("signal")).upper()
    symbol = _clean_text(trade.get("symbol"))
    underlying = _clean_text(trade.get("underlying"))

    if strategy == "IRON_CONDOR":
        if symbol.upper() == "IRON_CONDOR":
            return underlying or settings.nifty_symbol
        if underlying and underlying.upper() not in {"IRON_CONDOR", "NONE"}:
            return underlying
        return symbol or settings.nifty_symbol

    return symbol or underlying or ""


def _repair_shifted_trade_row(trade: dict[str, Any]) -> dict[str, Any]:
    row = dict(trade)

    strategy = _clean_text(row.get("strategy") or row.get("signal")).upper()
    reason = _clean_text(row.get("reason") or row.get("exit_reason"))
    order_id = _clean_text(row.get("order_id"))
    sell_order_id = _clean_text(row.get("sell_order_id"))
    pricing_source = _clean_text(row.get("pricing_source"))

    gross = _to_float(row.get("gross_pnl"), None)
    pnl = _to_float(row.get("pnl"), None)
    net = _to_float(row.get("net_pnl"), None)
    charges = _to_float(row.get("total_charges"), None)

    has_shifted_reason = (
        strategy == "IRON_CONDOR"
        and _known_exit_reason(pricing_source)
        and (not _known_exit_reason(reason) or reason.upper() == "CLOSED")
        and _is_numeric_text(order_id)
    )

    if has_shifted_reason:
        gross_value = gross
        if gross_value is None:
            gross_value = pnl if pnl is not None else net

        charge_value = _to_float(order_id, 0.0) or 0.0
        net_value = round((gross_value or 0.0) - charge_value, 2)

        row["gross_pnl"] = round(gross_value or 0.0, 2)
        row["pnl"] = net_value
        row["net_pnl"] = net_value
        row["total_charges"] = round(charge_value, 2)
        row["reason"] = pricing_source.upper()
        row["exit_reason"] = pricing_source.upper()
        row["order_id"] = ""
        row["sell_order_id"] = ""
        row["pricing_source"] = "broker_quote_snapshot"
        row["brokerage"] = ""
        row["stt"] = ""
        row["exchange_fee"] = ""
        row["gst"] = ""
        row["stamp_duty"] = ""
        return row

    if (
        strategy == "IRON_CONDOR"
        and gross is None
        and pnl is not None
        and charges is not None
        and charges > 0
    ):
        row["gross_pnl"] = round(pnl, 2)
        row["net_pnl"] = round(pnl - charges, 2)
        row["pnl"] = row["net_pnl"]

    if _known_exit_reason(order_id) and not _known_exit_reason(reason):
        row["reason"] = order_id.upper()
        row["exit_reason"] = order_id.upper()
        row["order_id"] = ""

    if _known_exit_reason(sell_order_id) and not _known_exit_reason(row.get("reason")):
        row["reason"] = sell_order_id.upper()
        row["exit_reason"] = sell_order_id.upper()
        row["sell_order_id"] = ""

    if _looks_like_pricing_source(row.get("quality_score")) and not row.get("pricing_source"):
        row["pricing_source"] = row.get("quality_score")
        row["quality_score"] = ""

    return row


def _normalize_dashboard_trade(trade: dict[str, Any]) -> dict[str, Any]:
    row = _repair_shifted_trade_row(trade)

    strategy = _clean_text(row.get("strategy") or row.get("signal"))
    signal = _clean_text(row.get("signal") or strategy)
    symbol = _normalize_symbol_for_dashboard(row)
    underlying = _clean_text(row.get("underlying") or symbol or settings.nifty_symbol)
    expiry = _clean_text(row.get("expiry"))
    strike = _clean_text(row.get("strike"))

    entry_price = _round_or_blank(row.get("entry_price"))
    entry_ltp = _round_or_blank(
        row.get("entry_ltp") if row.get("entry_ltp") not in ("", None) else entry_price
    )
    exit_price = _round_or_blank(
        row.get("exit_price") if row.get("exit_price") not in ("", None) else row.get("exit_premium")
    )
    exit_premium = _round_or_blank(
        row.get("exit_premium") if row.get("exit_premium") not in ("", None) else exit_price
    )
    gross_pnl = _round_or_blank(row.get("gross_pnl"))
    net_pnl = _round_or_blank(
        row.get("net_pnl") if row.get("net_pnl") not in ("", None) else row.get("pnl")
    )
    pnl = _round_or_blank(
        row.get("pnl") if row.get("pnl") not in ("", None) else net_pnl
    )
    total_charges = _round_or_blank(row.get("total_charges"))

    gross_float = _to_float(gross_pnl, None)
    net_float = _to_float(net_pnl, None)
    charges_float = _to_float(total_charges, None)

    if charges_float is None and gross_float is not None and net_float is not None:
        total_charges = round(abs(gross_float - net_float), 2)

    reason = _clean_text(row.get("exit_reason") or row.get("reason"))
    if not _known_exit_reason(reason):
        reason = "CLOSED" if _is_trade_closed(row) else "OPEN"

    pricing_source = _clean_text(row.get("pricing_source"))
    if _known_exit_reason(pricing_source):
        pricing_source = "broker_quote_snapshot"
    if not pricing_source and strategy.upper() == "IRON_CONDOR":
        pricing_source = "broker_quote_snapshot"

    return {
        **row,
        "strategy": strategy,
        "signal": signal,
        "symbol": symbol,
        "underlying": underlying,
        "expiry": expiry,
        "strike": strike,
        "entry_price": entry_price,
        "entry_ltp": entry_ltp,
        "exit_price": exit_price,
        "exit_premium": exit_premium,
        "qty": _to_int(row.get("qty"), 0),
        "gross_pnl": gross_pnl,
        "pnl": pnl,
        "net_pnl": net_pnl,
        "total_charges": total_charges,
        "charges": total_charges,
        "reason": reason.upper(),
        "exit_reason": reason.upper(),
        "order_id": "" if _known_exit_reason(row.get("order_id")) else _clean_text(row.get("order_id")),
        "sell_order_id": ""
        if _known_exit_reason(row.get("sell_order_id"))
        else _clean_text(row.get("sell_order_id")),
        "pricing_source": pricing_source,
    }


def _normalize_dashboard_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_normalize_dashboard_trade(trade) for trade in trades]


def _get_dashboard_trade_counts(
    trades: list[dict[str, Any]],
    active_trade: dict[str, Any] | None,
) -> dict[str, int]:
    closed_trade_count = sum(
        1 for trade in trades if _is_iron_condor_trade(trade) and _is_trade_closed(trade)
    )
    active_trade_count = 1 if active_trade and _is_iron_condor_trade(active_trade) else 0
    display_trade_count = closed_trade_count + active_trade_count

    return {
        "closed_trade_count": closed_trade_count,
        "active_trade_count": active_trade_count,
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
        cached = _safe_cached_ic_snapshot(trade)
        if cached:
            return cached
        raise RuntimeError("Broker unavailable for live iron condor snapshot")

    legs = trade.get("legs") or []
    if not legs:
        cached = _safe_cached_ic_snapshot(trade)
        if cached:
            return cached
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

        close_price = _quote_price_for_close_leg(side, bid, ask, ltp)

        if close_price <= 0:
            cached = _safe_cached_ic_snapshot(trade)
            if cached:
                logger.warning(
                    "Live IC quote invalid for %s; using active_trade cached snapshot",
                    symbol,
                )
                return cached

            raise RuntimeError(
                f"Invalid live quote for {symbol}: bid={bid} ask={ask} ltp={ltp}"
            )

        if side == "SELL":
            current_premium += close_price
        else:
            current_premium -= close_price

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
                "current_price": round(close_price, 2),
                "current_close_price": round(close_price, 2),
                "current_bid": round(float(bid or 0.0), 2),
                "current_ask": round(float(ask or 0.0), 2),
                "current_ltp": round(float(ltp or 0.0), 2),
                "price_source": "broker_quote_snapshot",
            }
        )

    return {
        "current_premium": round(current_premium, 2),
        "current_legs": current_legs,
        "pricing_source": "broker_quote_snapshot",
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
        trades = _normalize_dashboard_trades(scheduler.trade_store.get_all_trades())
        trade_counts = _get_dashboard_trade_counts(trades, state.active_trade)

        return _safe_json_response(
            {
                "bot_running": state.bot_running,
                "trading_mode": state.trading_mode,
                "trading_enabled": state.trading_enabled,
                "nifty_spot": state.spot_price,
                "current_iv": getattr(state, "current_iv", None),
                "signal": state.signal,
                "active_trade": state.active_trade,
                "daily_pnl": round(state.daily_pnl, 2),
                "live_pnl": round(state.live_pnl, 2),
                "max_daily_loss": float(getattr(settings, "max_daily_loss", 0.0) or 0.0),
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
            return {key: record.get(key) for key in keys}
        return {key: getattr(record, key, None) for key in keys}

    return _safe_json_response(
        {
            "sync_successful": startup_manager.sync_successful,
            "broker_positions_count": len(positions),
            "broker_orders_count": len(orders),
            "positions": [
                _to_dict(position, ["symbol", "quantity", "pnl"])
                for position in positions
            ],
            "open_orders": [
                _to_dict(order, ["symbol", "side", "quantity", "status"])
                for order in orders
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.get("/api/analytics")
async def analytics():
    from backend.app.scheduler.market_scheduler import scheduler

    try:
        trades = _normalize_dashboard_trades(scheduler.trade_store.get_all_trades())
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

    normalized_trades = _normalize_dashboard_trades(scheduler.trade_store.get_all_trades())
    return _safe_json_response({"trades": normalized_trades})


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

    engine = scheduler.engine

    if not engine or not engine.iron_condor_strategy:
        return _safe_json_response(
            {"status": "disabled", "message": "Iron Condor strategy not enabled"}
        )

    state = await scheduler.state.snapshot()
    current_time = datetime.now(IST)

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
    try:
        entry_time = datetime.fromisoformat(str(trade.get("entry_time") or ""))
    except (TypeError, ValueError) as exc:
        logger.warning("IC stats: invalid entry_time=%r (%s)", trade.get("entry_time"), exc)
        entry_time = current_time

    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=current_time.tzinfo)

    hours_elapsed = round((current_time - entry_time).total_seconds() / 3600, 1)
    until_theta_peak = _mins_until(time(14, 0), current_time)
    until_eod = _mins_until(time(15, 25), current_time)

    try:
        snapshot = await _get_live_iron_condor_snapshot(engine, trade)
        current_premium = float(snapshot["current_premium"])
        current_legs = snapshot.get("current_legs", [])
        premium_source = snapshot.get("pricing_source", "broker_quote_snapshot")
    except Exception as exc:
        cached = _safe_cached_ic_snapshot(trade)

        if cached:
            logger.warning("Live IC snapshot failed; using active_trade cache: %s", exc)
            current_premium = float(cached["current_premium"])
            current_legs = cached["current_legs"]
            premium_source = cached["pricing_source"]
        else:
            logger.warning("Live IC snapshot failed, falling back to model pricing: %s", exc)
            current_premium = engine.iron_condor_strategy.estimate_current_premium(
                float(trade["entry_price"]),
                entry_time,
                current_time,
            )
            current_legs = []
            premium_source = "model_fallback"

    entry_premium = float(trade["entry_price"])
    qty = int(trade["qty"])
    target_profit_pct = float(getattr(settings, "ic_target_profit_pct", 0.35))
    stop_loss_multiple = float(getattr(settings, "ic_stop_loss_multiple", 1.60))

    target_close_premium = round(entry_premium * (1 - target_profit_pct), 2)
    target_profit_amount = round(entry_premium * target_profit_pct * qty, 2)
    stop_loss_premium = round(entry_premium * stop_loss_multiple, 2)

    pnl_dict = engine.iron_condor_strategy.compute_pnl(
        entry_premium,
        float(current_premium),
        qty,
    )

    return _safe_json_response(
        {
            "status": "active",
            "entry_time": trade["entry_time"],
            "entry_premium": round(entry_premium, 2),
            "current_premium": round(float(current_premium), 2),
            "entry_strikes": trade["strike"],
            "hours_elapsed": hours_elapsed,
            "estimated_pnl": round(float(pnl_dict["net_pnl"]), 2),
            "gross_pnl": round(float(pnl_dict["gross_pnl"]), 2),
            "charges": round(float(pnl_dict["total_charges"]), 2),
            "target_profit_pct": target_profit_pct,
            "stop_loss_multiple": stop_loss_multiple,
            "target_pnl": target_close_premium,
            "target_close_premium": target_close_premium,
            "target_profit_amount": target_profit_amount,
            "stop_loss_prem": stop_loss_premium,
            "until_theta_peak": until_theta_peak,
            "until_eod": until_eod,
            "current_time": current_time.isoformat(),
            "pricing_source": premium_source,
            "current_legs": current_legs,
            "current_iv": getattr(state, "current_iv", None),
        }
    )


def _days_until_next_entry() -> int:
    now = datetime.now(IST)

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
    current_ist = current_time.astimezone(IST)
    target_dt = datetime.combine(current_ist.date(), target_time, tzinfo=IST)

    if target_dt <= current_ist:
        return 0

    return int((target_dt - current_ist).total_seconds() / 60)

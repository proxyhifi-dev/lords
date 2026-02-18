from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from lords_bot.app.risk_engine import RiskEngine
from lords_bot.strategies.option_selector import OptionSelector
from lords_bot.strategies.orb_strategy import ORBStrategy

ui_logger = logging.getLogger("lords_bot.ui")


def _safe_context(request: Request, risk_engine: RiskEngine, signal: dict[str, Any] | None) -> dict[str, Any]:
    """Guarantee template variables exist in every flow to prevent undefined crashes."""
    return {
        "request": request,
        "signal": signal or {},
        "trade": risk_engine.active_trade or {},
        "monitor_result": risk_engine.monitor_result or {},
        "capital": float(risk_engine.current_capital or 0.0),
        "pnl": float(risk_engine.total_pnl or 0.0),
        "mode": risk_engine.mode,
        "risk_status": "shutdown" if risk_engine.shutdown_triggered else "active",
        "orb": {
            "high": getattr(getattr(request.app.state, "strategy", None), "range_high", None),
            "low": getattr(getattr(request.app.state, "strategy", None), "range_low", None),
        },
    }


def create_ui_app(client, order_service, trading_mode: str) -> FastAPI:
    app = FastAPI(title="Lords Bot UI")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    strategy = ORBStrategy(client)
    selector = OptionSelector(client)
    risk_engine = RiskEngine(client, order_service, trading_mode)

    app.state.signal = None
    app.state.strategy = strategy

    @app.on_event("startup")
    async def startup_reconcile() -> None:
        await risk_engine.reconcile_positions_on_startup()

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return templates.TemplateResponse("index.html", _safe_context(request, risk_engine, app.state.signal))

    @app.post("/scan")
    async def scan() -> dict[str, Any]:
        """Safe scan endpoint: returns structured status and never throws upstream to UI."""
        try:
            breakout = await strategy.check_breakout()
            if not breakout:
                app.state.signal = None
                return {"status": "no_signal", "signal": {}, "reason": "breakout_not_found"}

            option = await selector.select_option(str(breakout["direction"]))
            sl, target = order_service.build_sl_target(
                float(option["ltp"]), risk_engine.settings.stop_loss_pct, risk_engine.settings.target_pct
            )
            app.state.signal = {
                **option,
                "breakout": breakout,
                "projected_stop_loss": sl,
                "projected_target": target,
                "capital": risk_engine.current_capital,
            }
            return {"status": "signal_found", "signal": app.state.signal}
        except Exception as exc:  # noqa: BLE001
            ui_logger.exception("Scan failed safely: %s", exc)
            return {"status": "error", "signal": {}, "reason": str(exc)}

    @app.post("/approve")
    async def approve() -> dict[str, Any]:
        if not app.state.signal:
            return {"status": "no_signal"}
        try:
            result = await risk_engine.execute_trade(app.state.signal)
            if result.get("status") == "executed":
                app.state.signal = None
            return result
        except Exception as exc:  # noqa: BLE001
            ui_logger.exception("Approve failed safely: %s", exc)
            return {"status": "error", "reason": str(exc)}

    @app.get("/monitor")
    async def monitor() -> dict[str, Any]:
        """Monitor always returns complete, crash-safe payload."""
        try:
            status = await risk_engine.monitor_trade()
        except Exception as exc:  # noqa: BLE001
            ui_logger.exception("Monitor failed safely: %s", exc)
            status = {"status": "error", "reason": str(exc)}
        return {
            "status": status.get("status", "unknown"),
            "orb": {"high": strategy.range_high, "low": strategy.range_low},
            "ltp": strategy.tick_state.last_price,
            "signal": app.state.signal or {},
            "positions": risk_engine.active_trade or {},
            "pnl": risk_engine.total_pnl,
            "risk_status": "shutdown" if risk_engine.shutdown_triggered else "active",
            "capital": risk_engine.current_capital,
            "details": status,
        }

    @app.post("/reset-day")
    async def reset_day() -> dict[str, Any]:
        risk_engine._refresh_day()
        app.state.signal = None
        return {"status": "ok"}

    return app

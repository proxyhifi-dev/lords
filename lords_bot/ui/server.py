from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from lords_bot.app.risk_engine import RiskEngine
from lords_bot.strategies.option_selector import OptionSelector
from lords_bot.strategies.orb_strategy import ORBStrategy

ui_logger = logging.getLogger("lords_bot.ui")


def create_ui_app(client, order_service, trading_mode: str) -> FastAPI:
    app = FastAPI(title="Lords Bot UI")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    strategy = ORBStrategy(client)
    selector = OptionSelector(client)
    risk_engine = RiskEngine(client, order_service, trading_mode)

    app.state.signal = None

    @app.on_event("startup")
    async def startup_reconcile() -> None:
        await risk_engine.reconcile_positions_on_startup()

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "signal": app.state.signal,
                "trade": risk_engine.active_trade,
                "monitor_result": risk_engine.monitor_result,
                "capital": risk_engine.current_capital,
                "pnl": risk_engine.total_pnl,
                "mode": risk_engine.mode,
            },
        )

    @app.post("/scan")
    async def scan():
        try:
            signal = await strategy.check_breakout()
            if not signal:
                app.state.signal = None
                return {"status": "no_signal"}

            option_data = await selector.select_option(str(signal["direction"]))
            projected_sl, projected_target = order_service.build_sl_target(
                float(option_data["ltp"]),
                risk_engine.stop_loss_pct,
                risk_engine.min_rr_ratio,
            )
            _, risk_amount = risk_engine._compute_quantity(float(option_data["ltp"]))

            app.state.signal = option_data | {
                "breakout": signal,
                "risk_amount": risk_amount,
                "lot_size": int(option_data.get("lot_size", 75)),
                "projected_stop_loss": projected_sl,
                "projected_target": projected_target,
            }
            return {"status": "signal_found", "signal": app.state.signal}
        except Exception as exc:
            ui_logger.exception("Scan failed but UI remains healthy: %s", exc)
            return {"status": "error", "reason": str(exc)}

    @app.post("/approve")
    async def approve():
        try:
            if not app.state.signal:
                return {"status": "no_signal"}
            result = await risk_engine.execute_trade(app.state.signal)
            if result.get("status") == "executed":
                app.state.signal = None
            return result
        except Exception as exc:
            ui_logger.exception("Approve failed safely: %s", exc)
            return {"status": "error", "reason": str(exc)}

    @app.get("/monitor")
    async def monitor():
        try:
            return await risk_engine.monitor_trade()
        except Exception as exc:
            ui_logger.exception("Monitor failed safely: %s", exc)
            return {"status": "error", "reason": str(exc)}

    @app.post("/reset-day")
    async def reset_day():
        try:
            risk_engine.reset_daily_state()
            app.state.signal = None
            return {"status": "ok"}
        except Exception as exc:
            ui_logger.exception("Reset-day failed safely: %s", exc)
            return {"status": "error", "reason": str(exc)}

    return app

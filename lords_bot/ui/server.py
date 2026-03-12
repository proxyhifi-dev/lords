from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from lords_bot.strategies.orb_strategy import ORBStrategy

if TYPE_CHECKING:
    from lords_bot.app.fyers_client import FyersClient
    from lords_bot.app.order_service import OrderService
    from lords_bot.app.risk_engine import RiskEngine

logger = logging.getLogger("lords_bot.ui")


def create_ui_app(
    client: "FyersClient",
    order_service: "OrderService",
    trading_mode: str,
) -> FastAPI:
    app = FastAPI()
    templates = Jinja2Templates(directory="lords_bot/ui/templates")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        strat: "ORBStrategy" | None = getattr(request.app.state, "strategy", None)
        risk: "RiskEngine" | None = getattr(request.app.state, "risk_engine", None)

        settings = getattr(client, "settings", None)
        initial_capital = float(getattr(settings, "initial_capital", 0.0))
        daily_loss = float(getattr(risk, "daily_loss", 0.0))
        pnl = -daily_loss

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "capital": initial_capital,
                "daily_loss": daily_loss,
                "pnl": pnl,
                "circuit_paused": client.is_trading_paused(),
                "range_high": getattr(strat, "range_high", None),
                "range_low": getattr(strat, "range_low", None),
                "trading_mode": trading_mode,
            },
        )

    @app.post("/scan")
    async def scan(request: Request):
        strategy: ORBStrategy | None = getattr(request.app.state, "strategy", None)
        if strategy is None:
            strategy = ORBStrategy(client)
            request.app.state.strategy = strategy

        try:
            signal = await strategy.check_breakout()
            if signal:
                return {"status": "ok", "signal": signal}
            return {"status": "idle", "signal": None}
        except Exception as exc:  # noqa: BLE001
            logger.exception("scan_failed")
            return {"status": "error", "message": str(exc)}

    @app.get("/monitor")
    async def monitor(request: Request):
        risk: "RiskEngine" | None = getattr(request.app.state, "risk_engine", None)
        can_trade = (True, "ok") if risk is None else risk.can_trade_now()
        return {
            "capital": float(getattr(getattr(client, "settings", None), "initial_capital", 0.0)),
            "risk_status": can_trade[1],
            "trading_paused": client.is_trading_paused(),
            "trading_pause_remaining": client.trading_pause_remaining_seconds,
            "trading_mode": trading_mode,
        }

    return app

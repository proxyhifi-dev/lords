from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

if TYPE_CHECKING:
    from lords_bot.app.fyers_client import FyersClient
    from lords_bot.app.order_service import OrderService
    from lords_bot.app.risk_engine import RiskEngine
    from lords_bot.strategies.orb_strategy import ORBStrategy

logger = logging.getLogger("lords_bot.ui")

templates = Jinja2Templates(directory="lords_bot/ui/templates")


def create_ui_app(
    *,
    client: "FyersClient",
    order_service: "OrderService",
    trading_mode: str,
) -> FastAPI:
    """
    Creates FastAPI UI app.

    Strategy and RiskEngine are attached to app.state
    during bootstrap.
    """

    app = FastAPI()

    # -------------------------------
    # Dashboard
    # -------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        strat: "ORBStrategy" | None = getattr(request.app.state, "strategy", None)
        risk: "RiskEngine" | None = getattr(request.app.state, "risk_engine", None)

        # Safe defaults
        initial_capital = float(getattr(client.settings, "initial_capital", 0.0))
        daily_loss = float(getattr(risk, "daily_loss", 0.0))

        # Basic PnL calculation (can later connect to real tracker)
        pnl = -daily_loss

        circuit_paused = client.is_trading_paused()

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "capital": initial_capital,
                "daily_loss": daily_loss,
                "pnl": pnl,  # 🔥 FIXED: always defined
                "circuit_paused": circuit_paused,
                "range_high": getattr(strat, "range_high", None),
                "range_low": getattr(strat, "range_low", None),
                "trading_mode": trading_mode,
            },
        )

    # -------------------------------
    # Reset Daily Risk
    # -------------------------------
    @app.post("/reset-day")
    async def reset_day(request: Request):
        risk: "RiskEngine" | None = getattr(request.app.state, "risk_engine", None)
        if risk:
            risk.daily_loss = 0.0
            logger.info("Daily loss reset via UI")

        return {"status": "ok"}

    # -------------------------------
    # Monitoring Endpoint
    # -------------------------------
    @app.get("/monitor")
    async def monitor():
        return {
            "trading_paused": client.is_trading_paused(),
            "trading_pause_remaining": client.trading_pause_remaining_seconds,
            "trading_mode": trading_mode,
        }

    return app
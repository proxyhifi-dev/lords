from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import asyncio

from strategies.orb_strategy import ORBStrategy
from strategies.option_selector import OptionSelector
from app.risk_engine import RiskEngine


def create_app(client, order_service, trading_mode):

    app = FastAPI()
    templates = Jinja2Templates(directory="ui/templates")

    strategy = ORBStrategy(client)
    selector = OptionSelector(client)
    risk_engine = RiskEngine(client, order_service, trading_mode)

    app.state.signal = None

    # ----------------------------
    # HOME PAGE
    # ----------------------------

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "signal": app.state.signal,
                "trade": risk_engine.active_trade,
                "capital": risk_engine.current_capital,
                "pnl": risk_engine.total_pnl,
                "mode": trading_mode
            },
        )

    # ----------------------------
    # SCAN BUTTON
    # ----------------------------

    @app.post("/scan")
    async def scan():

        await strategy.fetch_orb_range()
        signal = await strategy.check_breakout()

        if not signal:
            app.state.signal = None
            return {"status": "no_signal"}

        option_data = await selector.select_option(signal["direction"])
        app.state.signal = option_data

        return {"status": "signal_found"}

    # ----------------------------
    # APPROVE TRADE
    # ----------------------------

    @app.post("/approve")
    async def approve():

        if not app.state.signal:
            return {"status": "no_signal"}

        result = await risk_engine.execute_trade(app.state.signal)
        app.state.signal = None

        return result

    # ----------------------------
    # MONITOR TRADE
    # ----------------------------

    @app.get("/monitor")
    async def monitor():
        result = await risk_engine.monitor_trade()
        return result

    return app

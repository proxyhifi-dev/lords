from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from lords_bot.app.risk_engine import RiskEngine
from lords_bot.strategies.option_selector import OptionSelector
from lords_bot.strategies.orb_strategy import ORBStrategy


def create_ui_app(client, order_service, trading_mode: str) -> FastAPI:
    app = FastAPI(title="Lords Bot UI")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    strategy = ORBStrategy(client)
    selector = OptionSelector(client)
    risk_engine = RiskEngine(client, order_service, trading_mode)

    app.state.signal = None

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
        signal = await strategy.check_breakout()
        if not signal:
            app.state.signal = None
            return {"status": "no_signal"}

        option_data = await selector.select_option(str(signal["direction"]))
        app.state.signal = option_data | {"breakout": signal}
        return {"status": "signal_found", "signal": app.state.signal}

    @app.post("/approve")
    async def approve():
        if not app.state.signal:
            return {"status": "no_signal"}
        result = await risk_engine.execute_trade(app.state.signal)
        if result.get("status") == "executed":
            app.state.signal = None
        return result

    @app.get("/monitor")
    async def monitor():
        return await risk_engine.monitor_trade()

    @app.post("/reset-day")
    async def reset_day():
        risk_engine.reset_daily_state()
        app.state.signal = None
        return {"status": "ok"}

    return app

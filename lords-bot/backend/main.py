from __future__ import annotations

import threading

import uvicorn
from fastapi import FastAPI

from backend.app.broker.samco_client import SamcoClient
from backend.app.engine.trading_engine import TradingEngine
from backend.app.execution.order_executor import OrderExecutor
from backend.app.market.market_engine import MarketEngine
from backend.app.market.tick_engine import TickEngine
from backend.app.options.option_selector import OptionSelector
from backend.app.risk.risk_manager import RiskManager
from backend.app.scheduler.market_scheduler import MarketScheduler
from backend.app.storage.trade_store import TradeStore
from backend.app.strategy.orb_strategy import OrbStrategy
from backend.app.utils.logger import get_logger
from backend.config import settings

logger = get_logger("main")
app = FastAPI(title="Lords Bot Dashboard API")

samco_client = SamcoClient()
tick_engine = TickEngine()
strategy = OrbStrategy()
risk_manager = RiskManager()
option_selector = OptionSelector()
trade_store = TradeStore()
order_executor = OrderExecutor(samco_client)
trading_engine = TradingEngine(
    tick_engine=tick_engine,
    strategy=strategy,
    risk_manager=risk_manager,
    option_selector=option_selector,
    order_executor=order_executor,
    trade_store=trade_store,
)
market_engine = MarketEngine(samco_client)
market_loop_started = False


def start_market_loop() -> None:
    global market_loop_started
    if market_loop_started:
        return
    market_loop_started = True
    logger.info("Starting market loop")
    market_engine.run_poll_loop(trading_engine.on_quote)


@app.get("/api/dashboard")
def dashboard() -> dict:
    return trading_engine.dashboard_data()


@app.on_event("startup")
def startup() -> None:
    samco_client.login()
    loop_thread = threading.Thread(target=start_market_loop, daemon=True)
    loop_thread.start()

    scheduler = MarketScheduler(trading_engine)
    scheduler.schedule()


if __name__ == "__main__":
    uvicorn.run(app, host=settings.dashboard_host, port=settings.dashboard_port)

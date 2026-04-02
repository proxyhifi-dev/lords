from __future__ import annotations

import asyncio

from fastapi import FastAPI

from backend.app.api.dashboard_api import build_dashboard_router
from backend.app.broker.samco_client import SamcoClient
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
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

logger = get_logger("main")
app = FastAPI(title="Lords Bot Dashboard API")


event_bus = EventBus()
state_manager = StateManager()
trade_store = TradeStore()
broker = SamcoClient()

market_engine = MarketEngine(event_bus=event_bus, samco_client=broker)
tick_engine = TickEngine(event_bus=event_bus)
strategy = OrbStrategy(event_bus=event_bus)
risk_manager = RiskManager(event_bus=event_bus, state_manager=state_manager)
option_selector = OptionSelector()
order_executor = OrderExecutor(event_bus=event_bus, broker=broker, option_selector=option_selector)
trading_engine = TradingEngine(
    event_bus=event_bus,
    state_manager=state_manager,
    trade_store=trade_store,
    broker=broker,
)
market_scheduler = MarketScheduler(event_bus=event_bus)

app.include_router(build_dashboard_router(state_manager))


@app.on_event("startup")
async def on_startup() -> None:
    await state_manager.load()
    await broker.login()
    await event_bus.start()
    market_scheduler.start()

    app.state.tasks = [
        asyncio.create_task(market_engine.run(), name="market-engine"),
        asyncio.create_task(tick_engine.run(), name="tick-engine"),
        asyncio.create_task(strategy.run(), name="strategy-engine"),
        asyncio.create_task(risk_manager.run(), name="risk-engine"),
        asyncio.create_task(order_executor.run(), name="execution-engine"),
        asyncio.create_task(trading_engine.run(), name="trading-engine"),
    ]
    logger.info("Lords bot started")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    for task in getattr(app.state, "tasks", []):
        task.cancel()
    await asyncio.gather(*getattr(app.state, "tasks", []), return_exceptions=True)
    await state_manager.persist()
    await event_bus.stop()
    logger.info("Lords bot stopped")

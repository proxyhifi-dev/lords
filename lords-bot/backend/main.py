from __future__ import annotations

import asyncio
import signal

from broker.samco_broker import SamcoBroker
from core.logger import configure_logging
from engine.execution_engine import ExecutionEngine
from engine.market_feed_engine import MarketFeedEngine
from engine.strategy_engine import StrategyEngine
from risk.risk_manager import RiskManager
from services.market_data_service import MarketDataService
from services.option_chain_service import OptionChainService
from services.state_manager import StateManager
from strategies.orb_strategy import OrbStrategy


async def run_bot() -> None:
    configure_logging()

    tick_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    signal_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    broker = SamcoBroker()
    state_manager = StateManager()
    state = state_manager.load()

    market_data_service = MarketDataService(broker)
    option_chain_service = OptionChainService(broker)
    strategy = OrbStrategy()
    strategy.set_orb(orb_high=22100, orb_low=22000)

    market_engine = MarketFeedEngine(market_data_service, tick_queue)
    strategy_engine = StrategyEngine(strategy, tick_queue, signal_queue)
    execution_engine = ExecutionEngine(broker, option_chain_service, RiskManager(), state_manager, signal_queue, state)

    tasks = [
        asyncio.create_task(market_engine.run(), name='market_feed'),
        asyncio.create_task(strategy_engine.run(), name='strategy_engine'),
        asyncio.create_task(execution_engine.run(), name='execution_engine'),
    ]

    stop_event = asyncio.Event()

    def _shutdown() -> None:
        market_engine.stop()
        strategy_engine.stop()
        execution_engine.stop()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    await stop_event.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(run_bot())

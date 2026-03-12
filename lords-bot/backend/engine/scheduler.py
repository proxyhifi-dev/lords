from __future__ import annotations

import asyncio
import logging

from backend.config import settings
from backend.core.cache import TTLCache
from backend.engine.strategy_engine import strategy_engine
from backend.runtime_state import runtime_state
from backend.services.analysis_service import AnalysisService
from backend.services.option_chain_service import OptionChainService
from backend.trading.trade_executor import trade_executor

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None
        self.cache = TTLCache()
        self.option_chain_service = OptionChainService(self.cache)
        self.analysis_service = AnalysisService()

    async def tick(self) -> None:
        chain = await self.option_chain_service.get_option_chain(runtime_state.symbol, runtime_state.expiry)
        underlying = await self.option_chain_service.get_underlying_price(runtime_state.symbol)
        analysis = self.analysis_service.analyze(chain, runtime_state.symbol, runtime_state.expiry, underlying)
        signal = strategy_engine.run(analysis)
        trade_executor.execute(signal, runtime_state.symbol)

        runtime_state.latest_option_chain = chain
        runtime_state.latest_underlying_price = underlying
        runtime_state.latest_analysis = analysis
        runtime_state.latest_signal = signal
        logger.info('scheduler tick: pcr=%s signal=%s', analysis.get('pcr'), signal.get('signal'))

    async def _run(self) -> None:
        self.running = True
        while self.running:
            await self.tick()
            await asyncio.sleep(settings.scheduler_interval)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.running = False
        if self._task:
            await asyncio.wait([self._task], timeout=2)


scheduler = Scheduler()

from __future__ import annotations

import asyncio
import logging

from config import settings
from core.cache import TTLCache
from engine.strategy_engine import StrategyEngine
from runtime_state import runtime_state
from scanners.option_scanner import option_scanner
from services.analysis_service import AnalysisService
from services.option_chain_service import OptionChainService
from trading.paper_trading_engine import paper_trading_engine

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None
        self._tick_lock = asyncio.Lock()
        self.cache = TTLCache()
        self.option_chain_service = OptionChainService(self.cache)
        self.analysis_service = AnalysisService(self.cache)
        self.strategy_engine = StrategyEngine(self.cache)

    @property
    def interval_seconds(self) -> int:
        return max(60, settings.scheduler_interval)

    async def tick(self) -> None:
        async with self._tick_lock:
            try:
                chain = await self.option_chain_service.get_option_chain(runtime_state.symbol, runtime_state.expiry)
                underlying = await self.option_chain_service.get_underlying_price(runtime_state.symbol)
                analysis = self.analysis_service.analyze(chain, runtime_state.symbol, runtime_state.expiry, underlying)
                signal = self.strategy_engine.run(analysis, chain)
                best_trades = option_scanner.scan(chain, signal, runtime_state.symbol)
                runtime_state.pending_trade = best_trades[0] if best_trades else {}

                runtime_state.latest_option_chain = chain
                runtime_state.latest_underlying_price = underlying
                runtime_state.latest_analysis = analysis
                runtime_state.latest_signal = signal
                runtime_state.latest_best_trades = best_trades
                paper_trading_engine.mark_to_market(chain)
                logger.info('scheduler tick: pcr=%s signal=%s trades=%s', analysis.get('pcr'), signal.get('signal'), len(best_trades))
            except Exception as exc:  # noqa: BLE001
                runtime_state.scheduler_errors.insert(0, str(exc))
                runtime_state.scheduler_errors = runtime_state.scheduler_errors[:10]
                logger.error('scheduler tick failed err=%s', exc)

    async def _run(self) -> None:
        while self.running:
            await self.tick()
            await asyncio.sleep(self.interval_seconds)

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._run())
        logger.info('scheduler started interval=%ss', self.interval_seconds)

    async def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info('scheduler stopped')


scheduler = Scheduler()

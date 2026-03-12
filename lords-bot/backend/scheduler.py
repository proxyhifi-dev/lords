from __future__ import annotations

import asyncio
import logging

import pandas as pd

from analysis_engine import analysis_engine
from config import runtime_state, settings
from option_chain_service import option_chain_service
from paper_trading_engine import paper_trading_engine
from signals import signal_store
from strategy_engine import strategy_engine

logger = logging.getLogger(__name__)


class MarketScheduler:
    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None
        self.latest_df = pd.DataFrame()
        self.latest_analysis = None

    async def tick(self) -> None:
        try:
            df = await option_chain_service.fetch_latest()
            self.latest_df = df
            analysis = analysis_engine.analyze(df, runtime_state.symbol, runtime_state.expiry)
            self.latest_analysis = analysis
            signal = strategy_engine.generate_signal(analysis, df)
            signal_store.set(signal)
            paper_trading_engine.evaluate(signal, df, runtime_state.symbol, analysis.atm_strike)
        except Exception as exc:  # noqa: BLE001
            logger.exception('Scheduler tick failed: %s', exc)

    async def run(self) -> None:
        self.running = True
        while self.running:
            await self.tick()
            await asyncio.sleep(settings.scheduler_interval_seconds)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self.running = False
        if self._task:
            await asyncio.wait([self._task], timeout=2)


market_scheduler = MarketScheduler()

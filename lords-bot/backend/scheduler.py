from __future__ import annotations

import asyncio
import logging

import pandas as pd

from analysis_engine import analysis_engine
from config import runtime_state, yaml_config
from database import database
from option_chain_service import option_chain_service
from position_monitor import position_monitor
from signals import signal_store
from strategy_engine import strategy_engine
from trade_executor import trade_executor
from utils import is_market_hours

logger = logging.getLogger(__name__)


class MarketScheduler:
    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None
        self.latest_df = pd.DataFrame()
        self.latest_analysis = None
        self.latest_underlying = 0.0
        self.previous_pcr: float | None = None

    async def tick(self) -> None:
        if not is_market_hours() and not self.latest_df.empty:
            logger.info('Outside market hours; serving cached data')
            return
        try:
            df, underlying = await option_chain_service.fetch_latest()
            self.latest_df = df
            self.latest_underlying = underlying
            if df.empty:
                return
            analysis = analysis_engine.analyze(df, runtime_state.symbol, runtime_state.expiry, underlying)
            self.latest_analysis = analysis
            signal = strategy_engine.generate_signal(analysis, self.previous_pcr)
            self.previous_pcr = analysis.pcr
            signal_store.set(signal)

            with database.connection() as conn:
                conn.execute(
                    'INSERT INTO analysis_history (pcr, trend, support, resistance, atm_strike, generated_at) VALUES (?, ?, ?, ?, ?, ?)',
                    (analysis.pcr, analysis.trend, analysis.support, analysis.resistance, analysis.atm_strike, analysis.generated_at),
                )
                conn.execute(
                    'INSERT INTO signals (signal, confidence, reason, generated_at) VALUES (?, ?, ?, ?)',
                    (signal.signal, signal.confidence, signal.reason, signal.generated_at),
                )

            trade_executor.execute(signal, df, runtime_state.symbol, analysis.atm_strike)
            position_monitor.update(df)
            logger.info('Scheduler tick completed pcr=%s signal=%s', analysis.pcr, signal.signal)
        except Exception as exc:  # noqa: BLE001
            logger.exception('Scheduler tick failed: %s', exc)

    async def run(self) -> None:
        self.running = True
        while self.running:
            await self.tick()
            await asyncio.sleep(yaml_config.scheduler_interval)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self.running = False
        if self._task:
            await asyncio.wait([self._task], timeout=2)


market_scheduler = MarketScheduler()

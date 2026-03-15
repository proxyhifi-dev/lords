from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from config import settings
from core.cache import TTLCache
from engine.candle_builder import CandleBuilder
from engine.order_manager import OrderManager
from engine.state_manager import StateManager
from models import EngineState, TradePosition
from risk.risk_manager import RiskManager
from services.market_data_service import MarketDataService
from services.option_chain_service import OptionChainService
from services.trade_logger import TradeLogger
from strategies.orb_strategy import OrbStrategy, compute_rsi

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

        cache = TTLCache()
        self.state_manager = StateManager()
        self.state: EngineState = self.state_manager.load()
        self.market_data_service = MarketDataService(cache)
        self.option_chain_service = OptionChainService(cache)
        self.candle_builder = CandleBuilder()
        self.strategy = OrbStrategy()
        self.risk_manager = RiskManager()
        self.order_manager = OrderManager()
        self.trade_logger = TradeLogger()

    @property
    def interval_seconds(self) -> int:
        return max(5, settings.scheduler_interval)

    async def tick(self) -> None:
        async with self._lock:
            try:
                candles = await self.market_data_service.get_historical_candles(settings.symbol)
                orb = self.candle_builder.opening_range(candles)
                spot = await self.market_data_service.get_nifty_spot()
                chain = await self.option_chain_service.get_option_chain(settings.symbol, settings.expiry)
                bias = self.option_chain_service.get_option_chain_bias(chain)

                signal = self.strategy.generate(spot, orb['high'], orb['low'], bias, candles)
                self.state.latest_signal = signal.__dict__
                self.state.orb_range = orb

                system_status = self.risk_manager.circuit_breaker(self.state, broker_ok=True, api_ok=True)
                self.state.system_status = system_status

                if signal.signal == 'BUY':
                    decision = self.risk_manager.pre_trade_check(self.state, capital=100000.0, entry=signal.entry_price, stop=signal.stop_loss)
                    if decision.allowed:
                        order_payload = {
                            'symbol': settings.symbol,
                            'transactionType': 'BUY',
                            'quantity': decision.quantity,
                            'orderType': 'MKT',
                            'productType': 'MIS',
                            'price': signal.entry_price,
                        }
                        placed = await self.order_manager.place_market_order(order_payload, self.state.trading_mode)
                        verification = await self.order_manager.verify_order_status(placed.get('order_id', ''), self.state.trading_mode)
                        if verification.get('order_status', 'COMPLETE') in {'COMPLETE', 'FILLED'}:
                            position = TradePosition(
                                symbol=settings.symbol,
                                side=signal.option_side,
                                quantity=decision.quantity,
                                entry_price=signal.entry_price,
                                stop_loss=signal.stop_loss,
                                target_price=signal.target_price,
                                order_id=placed.get('order_id', ''),
                            )
                            self.state.active_trade = position.to_dict()
                            self.state.trades_today += 1

                if self.state.active_trade:
                    closes = [float(c['close']) for c in candles]
                    rsi = compute_rsi(closes)
                    at = self.state.active_trade
                    should_exit = (
                        (at['side'] == 'CALL' and (spot <= at['stop_loss'] or spot >= at['target_price'] or rsi >= 70))
                        or (at['side'] == 'PUT' and (spot >= at['stop_loss'] or spot <= at['target_price'] or rsi <= 30))
                    )
                    if should_exit:
                        pnl = (spot - at['entry_price']) * at['quantity']
                        if at['side'] == 'PUT':
                            pnl = -pnl
                        self.state.realized_pnl += pnl
                        self.trade_logger.log_trade(
                            {
                                'symbol': at['symbol'],
                                'entry_price': at['entry_price'],
                                'exit_price': spot,
                                'timestamp': datetime.utcnow().isoformat(),
                                'pnl': round(pnl, 2),
                                'strategy': 'ORB',
                                'quantity': at['quantity'],
                            },
                        )
                        self.state.active_trade = {}

                self.state.strategy_state = {'spot': spot, 'oi_bias': bias}
                self.state_manager.save(self.state)
            except Exception as exc:  # noqa: BLE001
                self.state.last_error = str(exc)
                logger.exception('scheduler tick failed')
                self.state_manager.save(self.state)

    async def _run(self) -> None:
        while self.running:
            await self.tick()
            await asyncio.sleep(self.interval_seconds)

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._run())

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


scheduler = Scheduler()

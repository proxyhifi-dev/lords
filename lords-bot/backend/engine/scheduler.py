from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from core.cache import TTLCache
from engine.candle_builder import CandleBuilder
from engine.order_manager import OrderManager
from engine.state_manager import StateManager
from models import EngineState
from risk.risk_manager import RiskManager
from services.market_data_service import MarketDataService
from services.option_chain_service import OptionChainService
from services.trade_logger import TradeLogger
from strategies.orb_strategy import OrbStrategy
from strategies.pcr_strategy import PCRStrategy
from strategies.strategy_manager import StrategyManager

logger = logging.getLogger(__name__)
IST = ZoneInfo('Asia/Kolkata')


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
        self.strategy_manager = StrategyManager([OrbStrategy(), PCRStrategy()])
        self.risk_manager = RiskManager()
        self.order_manager = OrderManager()
        self.trade_logger = TradeLogger()

    @property
    def interval_seconds(self) -> int:
        return max(30, settings.scheduler_interval)

    def _in_market_hours(self, now: datetime) -> bool:
        return now.weekday() < 5 and time(9, 15) <= now.time() <= time(15, 30)

    async def flatten_active_trade(self) -> dict[str, Any]:
        if not self.state.active_trade:
            return {'status': 'ok', 'closed': 0}
        spot = self.state.strategy_state.get('spot') or self.market_data_service._last_price or 0.0
        closed = await self.order_manager.flatten_positions(self.state.trading_mode, float(spot or 0.0))
        total = sum(float(item.get('pnl') or 0.0) for item in closed)
        self.state.realized_pnl += total
        self.state.active_trade = {}
        self.state_manager.save(self.state)
        return {'status': 'ok', 'closed': len(closed), 'pnl': total}

    async def tick(self) -> None:
        if self._lock.locked():
            return

        async with self._lock:
            now = datetime.now(IST)
            try:
                if not self._in_market_hours(now):
                    self.state.system_status = 'MARKET_CLOSED'
                    self.state_manager.save(self.state)
                    return
                self.state.system_status = self.risk_manager.circuit_breaker(self.state, broker_ok=True, api_ok=True)

                try:
                    spot = await self.market_data_service.get_spot_price()
                except Exception as exc:
                    self.state.last_error = f'spot_fetch_failed:{exc}'
                    logger.exception('scheduler_spot_fetch_failed')
                    self.state_manager.save(self.state)
                    return

                self.state.strategy_state['spot'] = spot
                self.market_data_service.add_tick(spot, timestamp=now)
                self.candle_builder.add_tick({'timestamp': now, 'price': spot, 'volume': 0})
                self.candle_builder.prune_ticks()
                candles = self.candle_builder.build_1min_candles()
                orb = self.candle_builder.opening_range(candles)
                self.state.orb_range = orb

                chain: list[dict[str, Any]] = []
                bias = 'NEUTRAL'
                try:
                    chain = await self.option_chain_service.get_option_chain(settings.symbol, settings.expiry)
                    bias = self.option_chain_service.get_option_chain_bias(chain)
                except Exception as exc:
                    self.state.last_error = f'option_chain_failed:{exc}'
                    logger.exception('scheduler_option_chain_fetch_failed')

                signal = self.strategy_manager.choose({
                    'spot_price': spot,
                    'orb_high': orb.get('high', 0.0),
                    'orb_low': orb.get('low', 0.0),
                    'option_chain_bias': bias,
                    'option_chain': chain,
                    'candles': candles,
                })
                self.state.latest_signal = signal

                if signal.get('signal') in {'BUY CALL', 'BUY PUT'} and not self.state.active_trade:
                    option = self.option_chain_service.pick_option_contract(
                        chain,
                        spot,
                        signal.get('option_side', ''),
                        settings.symbol,
                        self.option_chain_service.get_live_expiry(settings.symbol, settings.expiry),
                    )
                    if option and float(option.get('premium') or 0) > 0:
                        risk = self.risk_manager.pre_trade_check(
                            self.state,
                            capital=settings.paper_capital,
                            entry=float(spot),
                            stop=float(signal.get('stop_loss') or 0.0),
                        )
                        if risk.allowed:
                            payload = {
                                'exchange': 'NFO',
                                'symbolName': option['option_symbol'],
                                'expiryDate': option['expiry'],
                                'strikePrice': option['strike'],
                                'optionType': option['option_type'],
                                'transactionType': 'BUY',
                                'orderType': 'MARKET',
                                'productType': 'MIS',
                                'quantity': risk.quantity,
                                'price': option['premium'],
                            }
                            order = await self.order_manager.place_order(payload, self.state.trading_mode)
                            if str(order.get('status', '')).lower() == 'success':
                                self.state.active_trade = {
                                    'order_id': order.get('order_id'),
                                    'symbol': option['option_symbol'],
                                    'entry_price': option['premium'],
                                    'quantity': risk.quantity,
                                    'side': signal.get('option_side'),
                                    'stop_loss': signal.get('stop_loss'),
                                    'target': signal.get('target_price'),
                                }
                                self.state.trades_today += 1

                if self.state.active_trade:
                    pos = self.state.active_trade
                    order_id = pos.get('order_id', '')
                    pnl = self.order_manager.update_pnl(order_id, spot)
                    should_exit = (
                        pos.get('stop_loss', 0) > 0 and pos.get('target', 0) > 0 and (spot <= pos['stop_loss'] or spot >= pos['target'])
                    )
                    if should_exit:
                        trade = self.order_manager.close_position(order_id, spot)
                        trade_pnl = float(trade.get('pnl') or pnl)
                        self.state.realized_pnl += trade_pnl
                        self.state.consecutive_losses = self.state.consecutive_losses + 1 if trade_pnl < 0 else 0
                        self.trade_logger.log_trade(trade)
                        self.state.active_trade = {}

                self.state.system_status = self.risk_manager.circuit_breaker(self.state, broker_ok=True, api_ok=True)
                self.state_manager.save(self.state)
            except Exception:
                logger.exception('scheduler_tick_failed')
                self.state.last_error = 'scheduler_tick_failed'
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
        logger.info('scheduler_started interval_seconds=%s', self.interval_seconds)

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

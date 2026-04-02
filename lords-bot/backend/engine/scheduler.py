from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from config import settings
from core.cache import TTLCache
from engine.candle_builder import CandleBuilder
from engine.execution_engine import ExecutionEngine
from engine.market_data_engine import MarketFeedEngine, TickEvent
from engine.order_manager import OrderManager
from engine.state_manager import StateManager
from engine.strategy_engine import StrategyEngine
from models import EngineState
from risk.risk_manager import RiskManager
from services.market_data_service import MarketDataService
from services.option_chain_service import OptionChainService
from services.trade_logger import TradeLogger
from strategies.custom_signal_strategy import CustomSignalStrategy
from strategies.orb_strategy import OrbStrategy
from strategies.pcr_strategy import PCRStrategy

logger = logging.getLogger(__name__)
IST = ZoneInfo('Asia/Kolkata')


class Scheduler:
    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None

        cache = TTLCache()
        self.state_manager = StateManager()
        self.state: EngineState = self.state_manager.load()

        self.market_data_service = MarketDataService(cache)
        self.option_chain_service = OptionChainService(cache)
        self.candle_builder = CandleBuilder()
        self.strategy_engine = StrategyEngine([OrbStrategy(), PCRStrategy(), CustomSignalStrategy()])
        self.risk_manager = RiskManager()
        self.order_manager = OrderManager()
        self.execution_engine = ExecutionEngine(self.order_manager)
        self.trade_logger = TradeLogger()
        self.market_feed = MarketFeedEngine(self.market_data_service)

    @property
    def interval_seconds(self) -> int:
        return max(1, settings.scheduler_interval)

    def _in_market_hours(self, now: datetime) -> bool:
        return now.weekday() < 5 and time(9, 15) <= now.time() <= time(15, 30)

    async def flatten_active_trade(self) -> dict:
        if not self.state.active_trade:
            return {'status': 'ok', 'closed': 0}
        spot = self.state.strategy_state.get('spot') or self.market_data_service._last_price or 0.0
        closed = await self.order_manager.flatten_positions(self.state.trading_mode, float(spot or 0.0))
        total = sum(float(item.get('pnl') or 0.0) for item in closed)
        self.state.realized_pnl += total
        self.state.active_trade = {}
        self.state_manager.save(self.state)
        return {'status': 'ok', 'closed': len(closed), 'pnl': total}

    async def on_market_tick(self, event: TickEvent) -> None:
        now = event.timestamp
        if not self._in_market_hours(now):
            self.state.system_status = 'MARKET_CLOSED'
            return

        self.candle_builder.add_tick({'timestamp': now, 'price': event.price, 'volume': event.volume})
        self.candle_builder.prune_ticks()
        candles = self.candle_builder.build_1min_candles()
        orb = self.candle_builder.opening_range(candles)

        chain = await self.option_chain_service.get_option_chain(settings.symbol, settings.expiry)
        bias = self.option_chain_service.get_option_chain_bias(chain)
        market_data = {
            'spot_price': event.price,
            'orb_high': orb.get('high', 0.0),
            'orb_low': orb.get('low', 0.0),
            'option_chain_bias': bias,
            'option_chain': chain,
            'candles': candles,
        }
        signal = self.strategy_engine.choose(market_data)
        self.state.latest_signal = signal
        self.state.strategy_state['spot'] = event.price
        self.state.orb_range = orb

        should_trade = str(signal.get('signal', '')).upper() in {'BUY', 'BUY CALL', 'BUY PUT'}
        if should_trade and not self.state.active_trade:
            option = self.option_chain_service.pick_option_contract(
                chain,
                event.price,
                signal.get('option_side', ''),
                settings.symbol,
                self.option_chain_service.get_live_expiry(settings.symbol, settings.expiry),
            )
            risk = self.risk_manager.pre_trade_check(
                self.state,
                capital=settings.paper_capital,
                entry=float(event.price),
                stop=float(signal.get('stop_loss') or 0.0),
            )
            if option and risk.allowed and float(option.get('premium') or 0.0) > 0:
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
                execution = await self.execution_engine.execute(payload, self.state.trading_mode)
                if str(execution.get('status', '')).lower() == 'success':
                    self.state.active_trade = {
                        'order_id': execution.get('order_id'),
                        'symbol': option['option_symbol'],
                        'entry_price': float(execution.get('fill_price') or option['premium']),
                        'quantity': risk.quantity,
                        'side': signal.get('option_side'),
                        'stop_loss': signal.get('stop_loss'),
                        'target': signal.get('target_price'),
                    }
                    self.state.trades_today += 1

        if self.state.active_trade:
            pos = self.state.active_trade
            option_quote = await self.market_data_service.get_option_ltp(str(pos.get('symbol') or ''))
            pnl = self.order_manager.update_pnl(pos.get('order_id', ''), option_quote)
            if self.risk_manager.should_exit_trade(pos, event.price, option_quote):
                trade = self.order_manager.close_position(pos.get('order_id', ''), option_quote)
                trade_pnl = float(trade.get('pnl') or pnl)
                self.state.realized_pnl += trade_pnl
                self.state.consecutive_losses = self.state.consecutive_losses + 1 if trade_pnl < 0 else 0
                self.trade_logger.log_trade(trade)
                self.state.active_trade = {}

        self.state.system_status = self.risk_manager.circuit_breaker(self.state, broker_ok=True, api_ok=True)
        self.state_manager.save(self.state)

    async def _run(self) -> None:
        await self.market_feed.start(settings.market_tick_interval_seconds)
        async for event in self.market_feed.tick_stream.subscribe():
            if not self.running:
                break
            try:
                await self.on_market_tick(event)
            except Exception:
                logger.exception('scheduler_tick_failed')

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        await self.market_feed.stop()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


scheduler = Scheduler()

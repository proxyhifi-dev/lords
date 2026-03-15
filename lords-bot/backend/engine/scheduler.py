from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

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
        self.strategy = OrbStrategy()
        self.risk_manager = RiskManager()
        self.order_manager = OrderManager()
        self.trade_logger = TradeLogger()

        self._last_option_chain_fetch: datetime | None = None
        self._latest_chain: list[dict[str, float]] = []
        self._last_entry_candle: str = ''

    @property
    def interval_seconds(self) -> int:
        return max(5, settings.scheduler_interval)

    def _in_market_hours(self, now: datetime) -> bool:
        return now.weekday() < 5 and time(9, 15) <= now.time() <= time(15, 30)

    async def flatten_active_trade(self) -> dict:
        async with self._lock:
            if not self.state.active_trade:
                return {'status': 'noop'}
            at = self.state.active_trade
            if self.state.trading_mode == 'REAL':
                payload = {
                    'exchange': 'NFO',
                    'symbolName': settings.symbol,
                    'expiryDate': settings.expiry,
                    'strikePrice': str(int(float(at.get('strike') or 0))),
                    'optionType': 'CE' if at.get('side') == 'CALL' else 'PE',
                    'transactionType': 'SELL',
                    'orderType': 'MKT',
                    'productType': 'MIS',
                    'quantity': int(at.get('quantity') or 0),
                }
                await self.order_manager.place_market_order(payload, self.state.trading_mode)
            self.state.active_trade = {}
            self.state_manager.save(self.state)
            return {'status': 'flattened'}

    async def tick(self) -> None:
        if self._lock.locked():
            logger.warning('tick skipped because previous tick is still running')
            return

        async with self._lock:
            try:
                now = datetime.now(IST)
                if not self._in_market_hours(now):
                    self.state.system_status = 'MARKET_CLOSED'
                    self.state_manager.save(self.state)
                    return

                if self.state.trade_day != now.date().isoformat():
                    self.state.trade_day = now.date().isoformat()
                    self.state.trades_today = 0
                    self.state.realized_pnl = 0.0
                    self.state.consecutive_losses = 0
                    self.state.active_trade = {}
                    self._last_entry_candle = ''
                    self.candle_builder.reset_for_session(now.date())

                spot = await self.market_data_service.get_nifty_spot()
                if spot <= 0:
                    self.state.last_error = 'invalid_spot'
                    self.state.system_status = 'API_UNSTABLE'
                    self.state_manager.save(self.state)
                    return

                self.market_data_service.add_tick(spot, timestamp=now)
                self.candle_builder.add_tick({'timestamp': now.isoformat(), 'price': spot, 'volume': 0})
                self.candle_builder.prune_ticks()
                candles = self.candle_builder.build_5min_candles()
                if not candles:
                    return

                orb = self.candle_builder.opening_range(candles)
                self.state.orb_range = orb
                if orb['high'] <= 0 or orb['low'] <= 0:
                    self.state.latest_signal = {'signal': 'HOLD', 'reason': 'WAITING_FOR_ORB'}
                    self.state_manager.save(self.state)
                    return

                should_refresh_chain = self._last_option_chain_fetch is None or (
                    now - self._last_option_chain_fetch
                ).total_seconds() >= max(10, settings.option_chain_ttl)

                api_ok = True
                broker_ok = True
                if should_refresh_chain:
                    self._latest_chain = await self.option_chain_service.get_option_chain(settings.symbol, settings.expiry)
                    self._last_option_chain_fetch = now
                chain = self._latest_chain
                if not chain:
                    api_ok = False

                bias = self.option_chain_service.get_option_chain_bias(chain)
                signal = self.strategy.generate(spot, orb['high'], orb['low'], bias, candles)
                self.state.latest_signal = signal.__dict__

                self.state.system_status = self.risk_manager.circuit_breaker(self.state, broker_ok=broker_ok, api_ok=api_ok)
                current_candle = candles[-1]['timestamp']

                if (
                    signal.signal == 'BUY'
                    and self.state.system_status == 'RUNNING'
                    and not self.state.active_trade
                    and self._last_entry_candle != current_candle
                ):
                    decision = self.risk_manager.pre_trade_check(
                        self.state,
                        capital=settings.paper_capital,
                        entry=signal.entry_price,
                        stop=signal.stop_loss,
                    )
                    if decision.allowed:
                        contract = self.option_chain_service.pick_option_contract(
                            chain, spot, signal.option_side, settings.symbol, settings.expiry
                        )
                        option_premium = float(contract['premium'] or 0.0)
                        if option_premium <= 0:
                            self.state.last_error = 'invalid_option_premium'
                        else:
                            order_payload = {
                                'exchange': 'NFO',
                                'symbolName': settings.symbol,
                                'expiryDate': contract['expiry'],
                                'strikePrice': str(int(contract['strike'])),
                                'optionType': contract['option_type'],
                                'transactionType': 'BUY',
                                'orderType': 'MKT',
                                'productType': 'MIS',
                                'quantity': decision.quantity,
                            }
                            placed = await self.order_manager.place_market_order(order_payload, self.state.trading_mode)
                            order_id = placed.get('order_id') or placed.get('nOrdNo') or ''
                            verification = (
                                {'order_status': 'COMPLETE'}
                                if self.state.trading_mode == 'PAPER'
                                else await self.order_manager.verify_order_status(order_id, self.state.trading_mode)
                            )
                            if order_id and self.order_manager.is_verified_success(verification):
                                position = TradePosition(
                                    symbol=contract['option_symbol'],
                                    side=signal.option_side,
                                    quantity=decision.quantity,
                                    entry_price=option_premium,
                                    stop_loss=signal.stop_loss,
                                    target_price=signal.target_price,
                                    order_id=order_id,
                                )
                                self.state.active_trade = position.to_dict()
                                self.state.active_trade['strike'] = contract['strike']
                                self.state.active_trade['entry_spot'] = spot
                                self.state.trades_today += 1
                                self._last_entry_candle = current_candle
                            else:
                                broker_ok = False
                                self.state.last_error = str(placed.get('statusMessage') or 'order_rejected')

                if self.state.active_trade:
                    at = self.state.active_trade
                    strike = float(at.get('strike') or 0.0)
                    option_ltp = float(at['entry_price'])
                    for row in chain:
                        if float(row.get('strike_price') or 0.0) == strike:
                            premium_key = 'call_ltp' if at['side'] == 'CALL' else 'put_ltp'
                            option_ltp = float(row.get(premium_key) or option_ltp)
                            break

                    rsi = compute_rsi([float(c['close']) for c in candles])
                    should_exit = False
                    if at['side'] == 'CALL':
                        should_exit = spot <= at['stop_loss'] or spot >= at['target_price'] or rsi >= 70
                    elif at['side'] == 'PUT':
                        should_exit = spot >= at['stop_loss'] or spot <= at['target_price'] or rsi <= 30
                    if now.time() >= time(15, 15):
                        should_exit = True

                    if should_exit:
                        if self.state.trading_mode == 'REAL':
                            exit_payload = {
                                'exchange': 'NFO',
                                'symbolName': settings.symbol,
                                'expiryDate': settings.expiry,
                                'strikePrice': str(int(strike)),
                                'optionType': 'CE' if at['side'] == 'CALL' else 'PE',
                                'transactionType': 'SELL',
                                'orderType': 'MKT',
                                'productType': 'MIS',
                                'quantity': int(at['quantity']),
                            }
                            await self.order_manager.place_market_order(exit_payload, self.state.trading_mode)

                        pnl = (option_ltp - float(at['entry_price'])) * int(at['quantity'])
                        self.state.realized_pnl += pnl
                        self.state.consecutive_losses = self.state.consecutive_losses + 1 if pnl < 0 else 0
                        self.trade_logger.log_trade(
                            {
                                'symbol': at['symbol'],
                                'entry_price': at['entry_price'],
                                'exit_price': option_ltp,
                                'entry_spot': at.get('entry_spot', 0.0),
                                'exit_spot': spot,
                                'timestamp': datetime.utcnow().isoformat(),
                                'pnl': round(pnl, 2),
                                'strategy': 'ORB',
                                'quantity': at['quantity'],
                            }
                        )
                        self.state.active_trade = {}

                self.state.strategy_state = {
                    'spot': spot,
                    'oi_bias': bias,
                    'rsi': compute_rsi([float(c['close']) for c in candles]),
                    'orb_high': orb.get('high', 0.0),
                    'orb_low': orb.get('low', 0.0),
                    'trade_active': bool(self.state.active_trade),
                }
                self.state.system_status = self.risk_manager.circuit_breaker(self.state, broker_ok=broker_ok, api_ok=api_ok)
                self.state_manager.save(self.state)
            except Exception as exc:
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


scheduler = Scheduler()

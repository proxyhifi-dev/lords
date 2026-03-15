from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time

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
        self._last_option_chain_fetch: datetime | None = None
        self._latest_chain: list[dict[str, float]] = []

    @property
    def interval_seconds(self) -> int:
        return max(5, settings.scheduler_interval)

    async def tick(self) -> None:

        async with self._lock:

            try:

                now = datetime.now()
                market_open = time(9, 15)
                market_close = time(15, 30)
                if now.weekday() >= 5 or not (market_open <= now.time() <= market_close):
                    self.state.system_status = 'MARKET_CLOSED'
                    self.state_manager.save(self.state)
                    return

                spot = await self.market_data_service.get_nifty_spot()
                self.market_data_service.add_tick(spot, timestamp=now)
                self.candle_builder.add_tick({'timestamp': now.isoformat(), 'price': spot, 'volume': 0})
                self.candle_builder.prune_ticks()

                candles = self.candle_builder.build_5min_candles()

                if not candles:
                    return

                orb = self.candle_builder.opening_range(candles)

                should_refresh_chain = (
                    self._last_option_chain_fetch is None
                    or (now - self._last_option_chain_fetch).total_seconds() >= max(10, settings.option_chain_ttl)
                )
                if should_refresh_chain:
                    self._latest_chain = await self.option_chain_service.get_option_chain(
                        settings.symbol,
                        settings.expiry,
                    )
                    self._last_option_chain_fetch = now

                chain = self._latest_chain

                bias = self.option_chain_service.get_option_chain_bias(chain)

                signal = self.strategy.generate(
                    spot,
                    orb["high"],
                    orb["low"],
                    bias,
                    candles,
                )

                self.state.latest_signal = signal.__dict__
                self.state.orb_range = orb
                logger.info('ORB high=%s low=%s spot=%s', orb.get('high'), orb.get('low'), spot)
                logger.info('Signal generated=%s reason=%s', signal.signal, signal.reason)

                # ---------------------------------------------
                # RISK CIRCUIT BREAKER
                # ---------------------------------------------

                system_status = self.risk_manager.circuit_breaker(
                    self.state,
                    broker_ok=True,
                    api_ok=True,
                )

                self.state.system_status = system_status

                # ---------------------------------------------
                # TRADE ENTRY
                # ---------------------------------------------

                if signal.signal == "BUY" and not self.state.active_trade:

                    decision = self.risk_manager.pre_trade_check(
                        self.state,
                        capital=100000.0,
                        entry=signal.entry_price,
                        stop=signal.stop_loss,
                    )

                    if decision.allowed:

                        contract = self.option_chain_service.pick_option_contract(
                            chain,
                            spot,
                            signal.option_side,
                            settings.symbol,
                            settings.expiry,
                        )
                        option_symbol = contract['option_symbol']
                        option_premium = float(contract['premium'] or signal.entry_price)

                        order_payload = {
                            "symbol": option_symbol,
                            "transactionType": "BUY",
                            "quantity": decision.quantity,
                            "orderType": "MKT",
                            "productType": "MIS",
                            "price": option_premium,
                        }

                        placed = await self.order_manager.place_market_order(
                            order_payload,
                            self.state.trading_mode,
                        )

                        verification = await self.order_manager.verify_order_status(
                            placed.get("order_id", ""),
                            self.state.trading_mode,
                        )

                        if verification.get("order_status") in {"COMPLETE", "FILLED"}:

                            position = TradePosition(
                                symbol=option_symbol,
                                side=signal.option_side,
                                quantity=decision.quantity,
                                entry_price=option_premium,
                                stop_loss=signal.stop_loss,
                                target_price=signal.target_price,
                                order_id=placed.get("order_id", ""),
                            )

                            self.state.active_trade = position.to_dict()
                            self.state.active_trade['strike'] = contract['strike']
                            self.state.active_trade['entry_spot'] = spot

                            self.state.trades_today += 1

                            logger.info("Trade opened %s", self.state.active_trade)
                        else:
                            logger.warning('Order not filled order=%s verification=%s', placed, verification)

                # ---------------------------------------------
                # TRADE MANAGEMENT
                # ---------------------------------------------

                if self.state.active_trade:

                    closes = [float(c["close"]) for c in candles]

                    rsi = compute_rsi(closes)

                    at = self.state.active_trade
                    strike = float(at.get('strike') or 0.0)

                    option_ltp = float(at['entry_price'])
                    for row in chain:
                        if float(row.get('strike_price') or 0.0) == strike:
                            premium_key = 'call_ltp' if at['side'] == 'CALL' else 'put_ltp'
                            option_ltp = float(row.get(premium_key) or option_ltp)
                            break

                    should_exit = False

                    if at["side"] == "CALL":

                        if (
                            spot <= at["stop_loss"]
                            or spot >= at["target_price"]
                            or rsi >= 70
                        ):
                            should_exit = True

                    if now.time() >= time(15, 15):
                        should_exit = True

                    if at["side"] == "PUT":

                        if (
                            spot >= at["stop_loss"]
                            or spot <= at["target_price"]
                            or rsi <= 30
                        ):
                            should_exit = True

                    if should_exit:

                        pnl = (option_ltp - float(at["entry_price"])) * int(at["quantity"])

                        self.state.realized_pnl += pnl

                        self.trade_logger.log_trade(
                            {
                                "symbol": at["symbol"],
                                "entry_price": at["entry_price"],
                                "exit_price": option_ltp,
                                "timestamp": datetime.utcnow().isoformat(),
                                "pnl": round(pnl, 2),
                                "strategy": "ORB",
                                "quantity": at["quantity"],
                            }
                        )

                        logger.info("Trade closed PnL=%s", pnl)

                        self.state.active_trade = {}

                self.state.strategy_state = {
                    "spot": spot,
                    "oi_bias": bias,
                    "rsi": compute_rsi([float(c["close"]) for c in candles]),
                    "orb_high": orb.get('high', 0.0),
                    "orb_low": orb.get('low', 0.0),
                    "trade_active": bool(self.state.active_trade),
                }

                self.state_manager.save(self.state)

            except Exception as exc:

                self.state.last_error = str(exc)

                logger.exception("scheduler tick failed")

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

        logger.info("scheduler started interval=%ss", self.interval_seconds)

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

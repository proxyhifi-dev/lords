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
from models import EngineState
from risk.risk_manager import RiskManager
from services.market_data_service import MarketDataService
from services.option_chain_service import OptionChainService
from services.trade_logger import TradeLogger
from strategies.orb_strategy import OrbStrategy
from strategies.pcr_strategy import PCRStrategy
from strategies.strategy_manager import StrategyManager

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


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

        self.strategy_manager = StrategyManager(
            [
                OrbStrategy(),
                PCRStrategy(),
            ]
        )

        self.risk_manager = RiskManager()
        self.order_manager = OrderManager()
        self.trade_logger = TradeLogger()

    @property
    def interval_seconds(self) -> int:
        return max(5, settings.scheduler_interval)

    def _in_market_hours(self, now: datetime) -> bool:
        return now.weekday() < 5 and time(9, 15) <= now.time() <= time(15, 30)

    async def tick(self) -> None:

        if self._lock.locked():
            return

        async with self._lock:

            try:

                now = datetime.now(IST)

                if not self._in_market_hours(now):
                    return

                spot = await self.market_data_service.get_nifty_spot()

                if spot <= 0:
                    return

                self.market_data_service.add_tick(spot, timestamp=now)

                self.candle_builder.add_tick(
                    {
                        "timestamp": now,
                        "price": spot,
                        "volume": 0,
                    }
                )

                self.candle_builder.prune_ticks()

                candles = self.candle_builder.build_1min_candles()

                if not candles:
                    return

                orb = self.candle_builder.opening_range(candles)

                self.state.orb_range = orb

                chain = await self.option_chain_service.get_option_chain(
                    settings.symbol,
                    settings.expiry,
                )

                bias = self.option_chain_service.get_option_chain_bias(chain)

                signal = self.strategy_manager.choose(
                    {
                        "spot_price": spot,
                        "orb_high": orb["high"],
                        "orb_low": orb["low"],
                        "option_chain_bias": bias,
                        "candles": candles,
                    }
                )

                self.state.latest_signal = signal

                # ------------------------------------------------
                # TRADE EXECUTION LOGIC
                # ------------------------------------------------

                if signal.get("signal") == "BUY" and not self.state.active_trade:

                    option = self.option_chain_service.pick_option_contract(
                        chain,
                        spot,
                        signal.get("option_side"),
                        settings.symbol,
                        settings.expiry,
                    )

                    payload = {
                        "exchange": "NFO",
                        "symbolName": option["option_symbol"],
                        "expiryDate": option["expiry"],
                        "strikePrice": option["strike"],
                        "optionType": option["option_type"],
                        "transactionType": "BUY",
                        "orderType": "MARKET",
                        "productType": "MIS",
                        "quantity": settings.quantity,
                        "price": option["premium"],
                    }

                    order = await self.order_manager.place_order(
                        payload,
                        self.state.trading_mode,
                    )

                    if order.get("status") == "Success":

                        order_id = order.get("order_id")

                        self.state.active_trade = {
                            "order_id": order_id,
                            "symbol": option["option_symbol"],
                            "entry_price": option["premium"],
                            "quantity": settings.quantity,
                            "side": signal.get("option_side"),
                            "stop_loss": signal.get("stop_loss"),
                            "target": signal.get("target_price"),
                        }

                        logger.info("Trade opened %s", option["option_symbol"])

                # ------------------------------------------------
                # EXIT LOGIC
                # ------------------------------------------------

                if self.state.active_trade:

                    pos = self.state.active_trade

                    pnl = self.order_manager.update_pnl(
                        pos["order_id"],
                        spot,
                    )

                    if (
                        spot <= pos["stop_loss"]
                        or spot >= pos["target"]
                    ):

                        trade = self.order_manager.close_position(
                            pos["order_id"],
                            spot,
                        )

                        self.trade_logger.log_trade(trade)

                        logger.info("Trade closed")

                        self.state.active_trade = {}

                self.state_manager.save(self.state)

            except Exception:

                logger.exception("scheduler tick failed")

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
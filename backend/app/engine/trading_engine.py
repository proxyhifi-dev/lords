from __future__ import annotations
import asyncio
from datetime import UTC, datetime

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.storage.trade_store import TradeStore
from backend.app.strategy.option_selector import OptionSelector
from backend.app.utils.logger import get_logger

settings = get_settings()


class TradingEngine:

    def __init__(
        self,
        event_bus: EventBus,
        state_manager,
        trade_store: TradeStore,
        broker: SamcoClient,
    ):
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.trade_store = trade_store
        self.broker = broker

        self.logger = get_logger("trading_engine")

        self._trade_lock = asyncio.Lock()
        self._broker_healthy = True
        self._symbol_cache: dict[str, str] = {}

    # --------------------------------------------------
    # MAIN RUN — gather all coroutines
    # --------------------------------------------------
    async def run(self):
        await asyncio.gather(
            self._signal_listener(),
            self._trade_executor(),
            self._monitor_trade_loop(),
            self._health_monitor_loop(),
        )

    # --------------------------------------------------
    # SIGNAL LISTENER
    # --------------------------------------------------
    async def _signal_listener(self):
        queue = self.event_bus.subscribe("SIGNAL")

        async for event in self.event_bus.iter_events(queue):

            payload = getattr(event, "payload", event)
            signal = payload.get("signal")

            if not signal:
                continue

            state = await self.state_manager.snapshot()

            # Ignore if already have a pending signal or active trade
            if state.signal is not None or state.active_trade:
                continue

            await self.state_manager.update(signal=signal)
            self.logger.info("Signal received → %s", signal)

    # --------------------------------------------------
    # RESOLVE OPTION SYMBOL  (with symbol cache)
    # --------------------------------------------------
    async def _resolve_option_symbol(self, strike: int, signal: str) -> str | None:

        cache_key = f"{strike}_{signal}"

        if cache_key in self._symbol_cache:
            return self._symbol_cache[cache_key]

        option_type = OptionSelector.get_option_type(signal)
        expiry = OptionSelector.get_expiry()

        self.logger.info(
            "Resolving option: strike=%s signal=%s expiry=%s type=%s",
            strike, signal, expiry, option_type,
        )

        chain = await self.broker.get_option_chain(
            search_symbol_name="NIFTY",
            exchange="NFO",
            expiry_date=expiry,
            strike_price=str(strike),
            option_type=option_type,
        )

        rows = chain.get("optionChainDetails") or chain.get("data") or []

        if not rows:
            self.logger.error(
                "Option chain empty — expiry=%s strike=%s type=%s | chain=%s",
                expiry, strike, option_type, chain,
            )
            return None

        best_symbol = None
        best_diff = 999_999.0

        for row in rows:
            row_strike = float(row.get("strikePrice", 0))
            diff = abs(row_strike - strike)
            if diff < best_diff:
                best_diff = diff
                best_symbol = row.get("tradingSymbol")

        if not best_symbol:
            self.logger.error(
                "Option symbol resolve failed strike=%s signal=%s", strike, signal
            )
            return None

        self._symbol_cache[cache_key] = best_symbol
        self.logger.info("Resolved option symbol: %s", best_symbol)
        return best_symbol

    # --------------------------------------------------
    # TRADE EXECUTOR  (polls every 1 s for a pending signal)
    # --------------------------------------------------
    async def _trade_executor(self):
        while True:
            await asyncio.sleep(1)

            async with self._trade_lock:

                state = await self.state_manager.snapshot()

                # Guard conditions
                if state.signal is None:
                    continue
                if state.active_trade:
                    continue
                if state.trade_count >= settings.max_trades:
                    await self.state_manager.update(signal=None)
                    self.logger.warning("Max trades reached — signal cleared")
                    continue
                if state.daily_pnl <= -abs(settings.max_daily_loss):
                    await self.state_manager.update(signal=None, trading_enabled=False)
                    self.logger.warning("Max daily loss hit — trading disabled")
                    continue
                if not state.trading_enabled:
                    await self.state_manager.update(signal=None)
                    continue
                if state.spot_price is None:
                    continue

                now = datetime.now().time()
                from datetime import time as dtime
                no_entry = dtime(*map(int, settings.no_entry_after.split(":")))
                if now >= no_entry:
                    await self.state_manager.update(signal=None)
                    self.logger.info("Past no-entry time — signal cleared")
                    continue

                signal = state.signal

                try:
                    strike = OptionSelector.get_otm_strike(state.spot_price, signal)
                    symbol = await self._resolve_option_symbol(strike, signal)

                    if not symbol:
                        await self.state_manager.update(signal=None)
                        continue

                    quote = await self.broker.get_quote(
                        symbol_name=symbol, exchange="NFO"
                    )
                    ltp = self.broker.parse_ltp(quote)

                    if not ltp:
                        self.logger.warning("LTP unavailable for %s — skipping", symbol)
                        await self.state_manager.update(signal=None)
                        continue

                    volume = (
                        quote.get("volume")
                        or quote.get("tradedVolume")
                        or 0
                    )
                    try:
                        volume = int(volume)
                    except (ValueError, TypeError):
                        volume = 0

                    if volume < settings.min_option_volume:
                        self.logger.warning(
                            "Low volume %s < %s for %s",
                            volume, settings.min_option_volume, symbol,
                        )
                        await self.state_manager.update(signal=None)
                        continue

                    qty = settings.order_qty

                    await self.broker.place_order(
                        symbol=symbol,
                        side="BUY",
                        quantity=qty,
                    )

                    trade = {
                        "symbol": symbol,
                        "qty": qty,
                        "entry_price": ltp,
                        "entry_time": datetime.now(UTC).isoformat(),
                        "status": "OPEN",
                        "signal": signal,
                        "max_price": ltp,
                    }

                    await self.state_manager.update(
                        active_trade=trade,
                        trade_count=state.trade_count + 1,
                        signal=None,
                    )

                    self.logger.info(
                        "Trade OPENED — symbol=%s entry=%.2f qty=%s",
                        symbol, ltp, qty,
                    )

                except Exception as exc:
                    self.logger.error("Trade execution failed: %s", exc, exc_info=True)
                    await self.state_manager.update(signal=None)

    # --------------------------------------------------
    # MONITOR TRADE LOOP  (SL / target / trailing / EOD)
    # --------------------------------------------------
    async def _monitor_trade_loop(self):
        while True:
            await asyncio.sleep(2)

            state = await self.state_manager.snapshot()
            trade = state.active_trade

            if not trade:
                continue

            try:
                quote = await self.broker.get_quote(
                    symbol_name=trade["symbol"], exchange="NFO"
                )
                ltp = self.broker.parse_ltp(quote)

                if not ltp:
                    continue

                entry = trade["entry_price"]
                qty = trade["qty"]
                pnl = (ltp - entry) * qty

                await self.state_manager.update(live_pnl=pnl)

                # Update trailing high
                trade["max_price"] = max(trade.get("max_price", ltp), ltp)

                trailing_sl = trade["max_price"] * (1 - settings.trailing_pct)

                now = datetime.now().time()
                from datetime import time as dtime
                sq_time = dtime(*map(int, settings.square_off.split(":")))

                if now >= sq_time:
                    await self._exit_trade(trade, "EOD", ltp)

                elif ltp <= entry * (1 - settings.stop_loss_pct):
                    await self._exit_trade(trade, "STOPLOSS", ltp)

                elif ltp >= entry * (1 + settings.target_pct):
                    await self._exit_trade(trade, "TARGET", ltp)

                elif ltp < trailing_sl and ltp < entry:
                    # Trailing SL only active after price moved in favour
                    await self._exit_trade(trade, "TRAILING_SL", ltp)

            except Exception as exc:
                self.logger.error("Monitor loop error: %s", exc, exc_info=True)

    # --------------------------------------------------
    # EXIT TRADE
    # --------------------------------------------------
    async def _exit_trade(self, trade: dict, reason: str, price: float):
        async with self._trade_lock:

            # Re-fetch state to avoid race
            state = await self.state_manager.snapshot()
            if not state.active_trade:
                return

            symbol = trade["symbol"]
            qty = trade["qty"]

            try:
                await self.broker.place_order(
                    symbol=symbol,
                    side="SELL",
                    quantity=qty,
                )
            except Exception as exc:
                self.logger.error("SELL order failed: %s", exc)

            pnl = (price - trade["entry_price"]) * qty

            closed = {
                **trade,
                "exit_price": price,
                "exit_time": datetime.now(UTC).isoformat(),
                "status": "CLOSED",
                "exit_reason": reason,
                "pnl": round(pnl, 2),
            }

            self.trade_store.append_trade(closed)

            await self.state_manager.update(
                active_trade=None,
                daily_pnl=round(state.daily_pnl + pnl, 2),
                live_pnl=0.0,
            )

            self.logger.info(
                "Trade CLOSED — symbol=%s reason=%s pnl=%.2f",
                symbol, reason, pnl,
            )

    # --------------------------------------------------
    # BROKER HEALTH MONITOR
    # --------------------------------------------------
    async def _health_monitor_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                healthy = await self.broker.healthcheck()
                self._broker_healthy = healthy
                if not healthy:
                    self.logger.warning("Broker healthcheck FAILED — attempting re-login")
                    await self.broker.login()
            except Exception as exc:
                self.logger.error("Health monitor error: %s", exc)

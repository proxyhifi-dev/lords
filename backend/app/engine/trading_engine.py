# backend/app/engine/trading_engine.py
from __future__ import annotations

import asyncio
import os
from collections import defaultdict, deque
from datetime import datetime, timezone, time as dtime
from zoneinfo import ZoneInfo

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.core.logging_config import setup_file_logging
from backend.app.engine.execution_manager import ExecutionManager, OrderState
from backend.app.engine.order_execution import (
    OrderExecutionSequence,
    ExpiryDaySafetyProtocol,
    WebSocketResilience,
    MarginUtilizationMonitor,
)
from backend.app.storage.trade_store import TradeStore
from backend.app.strategy.iron_condor_strategy import IronCondorStrategy
from backend.app.strategy.option_selector import OptionSelector

settings = get_settings()
logger = setup_file_logging("trading_engine")
IST = ZoneInfo("Asia/Kolkata")

_SELL_MAX_RETRIES = 3
_SELL_RETRY_DELAY = 1.5
_FILL_CONFIRM_ATTEMPTS = 8
_FILL_CONFIRM_DELAY = 0.75
_EXIT_VERIFY_ATTEMPTS = 4
_EXIT_VERIFY_DELAY = 1.0
_ENTRY_MAX_RETRIES = 3
_ENTRY_RETRY_DELAY = 1.0


def _parse_volume(quote: dict) -> int:
    def _int(val) -> int:
        try:
            return int(float(str(val).replace(",", "").strip()))
        except (TypeError, ValueError):
            return 0

    for key in ("tradedVolume", "volume", "traded_volume", "totalTradedVolume"):
        value = _int(quote.get(key))
        if value > 0:
            return value

    inner = quote.get("quoteDetails")
    if isinstance(inner, list) and inner:
        inner = inner[0]
    if isinstance(inner, dict):
        for key in ("tradedVolume", "volume", "totalTradedVolume"):
            value = _int(inner.get(key))
            if value > 0:
                return value

    data = quote.get("data")
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        for key in ("tradedVolume", "volume", "totalTradedVolume"):
            value = _int(data.get(key))
            if value > 0:
                return value

    return 0


def _parse_filled_qty(order_status: dict, requested_qty: int) -> int:
    def _int(val) -> int:
        try:
            return int(float(str(val).replace(",", "").strip()))
        except (TypeError, ValueError):
            return 0

    data = order_status.get("orderDetails") or order_status.get("data") or order_status
    if isinstance(data, list):
        data = data[0] if data else {}

    for key in (
        "filledShares",
        "tradedQty",
        "filledQty",
        "executedQty",
        "filled_quantity",
        "tradedQuantity",
    ):
        value = _int(data.get(key))
        if value > 0:
            return value

    status = str(data.get("orderStatus") or data.get("status") or "").upper()
    if status in ("COMPLETE", "FILLED", "TRADED"):
        return requested_qty

    return 0


class TradingEngine:
    def __init__(
        self,
        event_bus: EventBus,
        state_manager,
        trade_store: TradeStore,
        broker: SamcoClient | None,
        strategy=None,
    ):
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.trade_store = trade_store
        self.broker = broker
        self.strategy = strategy
        self._trade_lock = asyncio.Lock()
        self._symbol_cache: dict[str, str] = {}
        self._fatal_lock = asyncio.Lock()
        self._ltp_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=20))

        self.iron_condor_strategy: IronCondorStrategy | None = None
        if settings.strategy_type == "iron_condor":
            self.iron_condor_strategy = IronCondorStrategy()
            logger.info("Iron Condor strategy enabled in TradingEngine")

        self.reconciliation = None

        from backend.app.risk.risk_manager import RiskManager as _RiskManager

        self.risk_manager = _RiskManager(
            event_bus=event_bus,
            state_manager=state_manager,
            broker=broker,
        )
        self.execution_manager = ExecutionManager(
            broker=self.broker,
            state_manager=self.state_manager,
            event_bus=self.event_bus,
        )

        self.order_executor = (
            OrderExecutionSequence(
                broker_client=self.broker,
                settings=settings,
                logger=logger,
            )
            if self.broker is not None
            else None
        )

        self.expiry_safety = ExpiryDaySafetyProtocol(
            settings=settings,
            logger=logger,
        )

        self.margin_monitor = MarginUtilizationMonitor(
            total_capital=settings.capital,
            safety_buffer=5000,
            logger=logger,
        )

        self.ws_resilience = None

        logger.info(
            "TradingEngine initialized mode=%s broker_connected=%s paper_mode_use_broker=%s",
            str(getattr(settings, "mode", "paper")).upper(),
            self.broker is not None,
            bool(getattr(settings, "paper_mode_use_broker", False)),
        )

    def _is_paper_mode(self) -> bool:
        return str(getattr(settings, "mode", "paper")).strip().lower() == "paper"

    def _paper_mode_use_broker(self) -> bool:
        return bool(getattr(settings, "paper_mode_use_broker", False))

    def _broker_available(self) -> bool:
        return self.broker is not None

    def _paper_safe_order_id(self, prefix: str, symbol: str) -> str:
        ts = int(datetime.now(timezone.utc).timestamp())
        return f"PAPER-{prefix}-{symbol}-{ts}"

    async def _paper_quote_price(self, symbol: str, fallback: float = 0.0) -> float:
        if not self._broker_available():
            return fallback
        try:
            quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
            ltp = self.broker.parse_ltp(quote)
            return float(ltp or fallback)
        except Exception as exc:
            logger.warning("Paper quote fetch failed symbol=%s err=%s", symbol, exc)
            return fallback

    def _map_signal(self, raw_signal: str) -> str:
        if not raw_signal:
            raise ValueError("Invalid signal: empty or missing")

        signal = str(raw_signal).strip().upper()
        if signal in ("LONG", "CALL"):
            return "CALL"
        if signal in ("SHORT", "PUT"):
            return "PUT"
        if signal in ("IRON_CONDOR", "IRONCONDOR", "IC"):
            return "IRON_CONDOR"
        raise ValueError(
            f"Invalid signal: {raw_signal}. Expected LONG, SHORT, CALL, PUT or IRON_CONDOR."
        )

    async def _resolve_option_symbol(self, strike: int, option_type: str) -> str | None:
        if not self._broker_available():
            logger.error("Cannot resolve IC option symbol: broker unavailable")
            return None

        key = f"{strike}_{option_type}"
        if key in self._symbol_cache:
            return self._symbol_cache[key]

        expiry = OptionSelector.get_expiry_api()
        logger.info(
            "IC SYMBOL LOOKUP: strike=%s type=%s expiry=%s",
            strike,
            option_type,
            expiry,
        )

        chain = await self.broker.get_option_chain(
            search_symbol_name=settings.nifty_symbol,
            exchange="NFO",
            expiry_date=expiry,
            strike_price=str(strike),
            option_type=option_type,
        )
        if isinstance(chain, dict) and chain.get("validationErrors"):
            logger.warning(
                "SAMCO validation error on IC symbol lookup: %s — retrying full chain",
                chain.get("validationErrors"),
            )
            chain = None

        rows = self._extract_chain_rows(chain) if chain else []

        if not rows:
            chain = await self.broker.get_option_chain(
                search_symbol_name=settings.nifty_symbol,
                exchange="NFO",
                expiry_date=expiry,
                strike_price="0",
                option_type=option_type,
            )
            if isinstance(chain, dict) and chain.get("validationErrors"):
                logger.error(
                    "IC symbol full-chain also failed: %s",
                    chain.get("validationErrors"),
                )
                return None
            rows = self._extract_chain_rows(chain)

        if not rows:
            logger.error("IC option chain empty: strike=%s type=%s", strike, option_type)
            return None

        best_sym = None
        best_diff = float("inf")
        for row in rows:
            try:
                diff = abs(float(row.get("strikePrice", 0)) - strike)
                if diff < best_diff:
                    best_diff = diff
                    best_sym = row.get("tradingSymbol")
            except (TypeError, ValueError):
                continue

        if best_sym:
            self._symbol_cache[key] = best_sym
            logger.info("IC SYMBOL RESOLVED: %s (snap_diff=%s)", best_sym, best_diff)
        else:
            logger.error("No valid IC symbol near strike=%s type=%s", strike, option_type)

        return best_sym

    async def _get_leg_quote_snapshot(self, symbol: str) -> tuple[dict, float, float, float]:
        if not self._broker_available():
            raise RuntimeError("Broker unavailable for quote snapshot")

        quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
        ltp = float(self.broker.parse_ltp(quote) or 0.0)
        bid, ask = self.broker.parse_bid_ask(quote)
        bid = float(bid or 0.0)
        ask = float(ask or 0.0)
        return quote, bid, ask, ltp

    def _quote_price_for_side(self, side: str, bid: float, ask: float, ltp: float) -> float:
        if side == "SELL":
            return bid or ltp or ask or 0.0
        return ask or ltp or bid or 0.0

    def _close_price_for_leg(self, leg_side: str, bid: float, ask: float, ltp: float) -> float:
        side = str(leg_side).upper()
        if side == "SELL":
            return ask or ltp or bid or 0.0
        return bid or ltp or ask or 0.0

    async def _get_live_iron_condor_close_snapshot(
        self,
        trade: dict[str, any],
    ) -> tuple[float, list[dict[str, any]]]:
        if not self._broker_available():
            raise RuntimeError("Broker unavailable for IC live snapshot")

        legs = trade.get("legs") or []
        if not legs:
            raise RuntimeError("Active IC trade has no legs")

        current_premium = 0.0
        current_legs: list[dict[str, any]] = []

        for leg in legs:
            symbol = leg.get("symbol")
            side = str(leg.get("side", "")).upper()
            if not symbol or side not in {"BUY", "SELL"}:
                raise RuntimeError(f"Invalid IC leg: {leg}")

            quote, bid, ask, ltp = await self._get_leg_quote_snapshot(symbol)
            close_price = self._close_price_for_leg(side, bid, ask, ltp)
            if close_price <= 0:
                raise RuntimeError(
                    f"Invalid IC close quote for {symbol}: bid={bid} ask={ask} ltp={ltp}"
                )

            if side == "SELL":
                current_premium += close_price
            else:
                current_premium -= close_price

            current_legs.append(
                {
                    "name": leg.get("name"),
                    "symbol": symbol,
                    "display_symbol": leg.get("display_symbol") or symbol,
                    "side": side,
                    "entry_price": float(leg.get("entry_price") or leg.get("fill_price") or 0.0),
                    "entry_bid": float(leg.get("entry_bid") or 0.0),
                    "entry_ask": float(leg.get("entry_ask") or 0.0),
                    "entry_ltp": float(leg.get("entry_ltp") or 0.0),
                    "current_bid": float(bid or 0.0),
                    "current_ask": float(ask or 0.0),
                    "current_ltp": float(ltp or 0.0),
                    "current_close_price": float(close_price or 0.0),
                    "current_quote": quote,
                    "price_source": "broker_quote_snapshot",
                }
            )

        return round(current_premium, 2), current_legs

    async def _build_iron_condor_snapshot_legs(
        self,
        strikes: dict[str, int],
        expiry: str,
    ) -> tuple[list[dict[str, any]], dict[str, float], float] | tuple[None, None, None]:
        if not self._broker_available():
            logger.error("Cannot build IC snapshot legs: broker unavailable")
            return None, None, None

        leg_specs = [
            ("long_put", "BUY", "PE", strikes["long_put"]),
            ("short_put", "SELL", "PE", strikes["short_put"]),
            ("short_call", "SELL", "CE", strikes["short_call"]),
            ("long_call", "BUY", "CE", strikes["long_call"]),
        ]

        legs: list[dict[str, any]] = []
        premiums: dict[str, float] = {}

        for name, side, option_type, strike in leg_specs:
            symbol = await self._resolve_option_symbol(strike, option_type)
            if not symbol:
                logger.error("Failed to resolve IC leg symbol for %s strike=%s type=%s", name, strike, option_type)
                return None, None, None

            quote, bid, ask, ltp = await self._get_leg_quote_snapshot(symbol)
            entry_price = self._quote_price_for_side(side, bid, ask, ltp)
            if entry_price <= 0:
                logger.error(
                    "Invalid IC quote snapshot for %s symbol=%s bid=%.2f ask=%.2f ltp=%.2f",
                    name,
                    symbol,
                    bid,
                    ask,
                    ltp,
                )
                return None, None, None

            premiums[name] = entry_price
            legs.append(
                {
                    "name": name,
                    "symbol": symbol,
                    "display_symbol": f"{settings.nifty_symbol} {expiry} {strike} {option_type}",
                    "side": side,
                    "option_type": option_type,
                    "strike": strike,
                    "qty": settings.order_qty,
                    "price": entry_price,
                    "entry_price": entry_price,
                    "entry_bid": bid,
                    "entry_ask": ask,
                    "entry_ltp": ltp,
                    "entry_quote": quote,
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "price_source": "broker_quote_snapshot",
                }
            )

        net_premium = premiums["short_call"] + premiums["short_put"] - premiums["long_call"] - premiums["long_put"]
        return legs, premiums, net_premium

    async def _place_iron_condor_leg(self, leg: dict[str, any]) -> dict[str, any] | None:
        if self._is_paper_mode():
            fill_price = float(leg.get("price") or leg.get("entry_price") or 0.0)
            if fill_price <= 0 and self._broker_available():
                fill_price = await self._paper_quote_price(leg["symbol"], fallback=0.0)

            logger.info(
                "PAPER MODE: simulated IC leg side=%s symbol=%s qty=%s price=%.2f",
                leg["side"],
                leg["symbol"],
                leg["qty"],
                fill_price,
            )
            return {
                **leg,
                "order_id": self._paper_safe_order_id(leg["side"], leg["symbol"]),
                "fill_price": fill_price,
                "filled_qty": leg["qty"],
                "price_source": "broker_quote_snapshot",
            }

        request = {
            "signal": "IRON_CONDOR",
            "symbol": leg["symbol"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "quantity": leg["qty"],
            "side": leg["side"],
        }
        exec_result = await self.execution_manager.execute_order(request)
        if exec_result.is_uncertain:
            return None
        if exec_result.state != OrderState.FILLED:
            return None

        filled_qty = exec_result.filled_qty
        if exec_result.order_id and exec_result.order_id.startswith("PAPER-"):
            filled_qty = leg["qty"]

        actual_fill = float(exec_result.avg_price or 0.0)
        if actual_fill <= 0:
            actual_fill = float(leg.get("entry_price") or leg.get("price") or 0.0)

        return {
            **leg,
            "order_id": exec_result.order_id,
            "fill_price": actual_fill,
            "entry_price": actual_fill,
            "filled_qty": filled_qty,
            "price_source": "broker_fill",
        }

    async def _rollback_iron_condor(self, legs: list[dict[str, any]]) -> None:
        if self._is_paper_mode():
            logger.warning("PAPER MODE: simulated IC rollback for %d legs", len(legs))
            return

        for leg in reversed(legs):
            side = "BUY" if leg["side"] == "SELL" else "SELL"
            try:
                await self.execution_manager.execute_order(
                    {
                        "signal": "IRON_CONDOR_ROLLBACK",
                        "symbol": leg["symbol"],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "quantity": leg["filled_qty"],
                        "side": side,
                    }
                )
            except Exception as exc:
                logger.warning(
                    "Rollback leg failed: %s %s qty=%d err=%s",
                    side,
                    leg["symbol"],
                    leg["filled_qty"],
                    exc,
                )

    async def _enter_iron_condor_trade(self, payload: dict[str, any], state) -> None:
        if not self.iron_condor_strategy:
            logger.error("Iron Condor strategy helper is unavailable")
            return

        if not self._broker_available():
            logger.error("Cannot enter Iron Condor: broker unavailable")
            return

        spot = state.spot_price
        if spot is None:
            logger.warning("Cannot enter Iron Condor: spot_price missing")
            return

        current_time = datetime.now(IST)
        logger.info("IC entry check started spot=%.2f time=%s", spot, current_time.isoformat())

        allowed = self.iron_condor_strategy.can_enter_cycle(current_time, state)
        logger.info("IC can_enter_cycle=%s", allowed)
        if not allowed:
            logger.warning("IC entry blocked by can_enter_cycle()")
            return

        strikes = self.iron_condor_strategy.calculate_strikes(spot)
        logger.info("IC strikes=%s", strikes)
        if not strikes:
            logger.error("Invalid strikes calculated - skipping entry")
            return

        expiry = OptionSelector.get_expiry_api()
        snapshot_legs, snapshot_premiums, snapshot_net_premium = await self._build_iron_condor_snapshot_legs(
            strikes,
            expiry,
        )
        if not snapshot_legs or snapshot_premiums is None or snapshot_net_premium is None:
            logger.error("Failed to build IC quote snapshot legs - skipping entry")
            return

        logger.info(
            "IC quote snapshot premium net_premium=%.2f min_required=%.2f",
            snapshot_net_premium,
            self.iron_condor_strategy.min_premium,
        )
        if snapshot_net_premium < self.iron_condor_strategy.min_premium:
            logger.warning(
                "Iron Condor premium too low: ₹%.2f < ₹%.2f",
                snapshot_net_premium,
                self.iron_condor_strategy.min_premium,
            )
            return

        is_valid = await self.risk_manager.validate_iron_condor_position(snapshot_net_premium, state)
        logger.info("IC risk validation passed=%s", is_valid)
        if not is_valid:
            logger.error("IC position validation failed - skipping entry")
            return

        logger.info("Starting Iron Condor entry sequence")

        margin_status = self.margin_monitor.check_margin(settings.ic_margin_required)
        if margin_status["status"] == "CRITICAL":
            logger.error("Rejecting entry: Critical margin utilization")
            return

        should_exit, reason = self.expiry_safety.should_force_exit(current_time, current_time)
        if should_exit:
            logger.critical("Rejecting entry: %s", reason)
            return

        execution_order_names = ("long_put", "long_call", "short_put", "short_call")
        execution_order = [next(leg for leg in snapshot_legs if leg["name"] == name) for name in execution_order_names]

        placed_legs: list[dict[str, any]] = []
        for leg in execution_order:
            placed = await self._place_iron_condor_leg(leg)
            if not placed or int(placed.get("filled_qty", 0)) <= 0:
                logger.error("IC leg placement failed: %s", leg["display_symbol"])
                if placed_legs:
                    await self._rollback_iron_condor(placed_legs)
                return
            placed_legs.append(placed)

        if len(placed_legs) != 4:
            logger.error("Only %d of 4 IC legs filled — rolling back", len(placed_legs))
            if placed_legs:
                await self._rollback_iron_condor(placed_legs)
            return

        filled_map = {leg["name"]: leg for leg in placed_legs}
        actual_premiums = {
            "short_call": float(filled_map["short_call"].get("fill_price") or filled_map["short_call"].get("entry_price") or 0.0),
            "long_call": float(filled_map["long_call"].get("fill_price") or filled_map["long_call"].get("entry_price") or 0.0),
            "short_put": float(filled_map["short_put"].get("fill_price") or filled_map["short_put"].get("entry_price") or 0.0),
            "long_put": float(filled_map["long_put"].get("fill_price") or filled_map["long_put"].get("entry_price") or 0.0),
        }
        actual_net_premium = (
            actual_premiums["short_call"]
            + actual_premiums["short_put"]
            - actual_premiums["long_call"]
            - actual_premiums["long_put"]
        )

        logger.info("All 4 IC legs successfully filled")
        logger.info("Spot: ₹%.0f", state.spot_price)
        logger.info("Shorts collected: ₹%.2f", actual_premiums["short_call"] + actual_premiums["short_put"])
        logger.info("Longs paid: ₹%.2f", actual_premiums["long_call"] + actual_premiums["long_put"])
        logger.info("Net credit: ₹%.2f", actual_net_premium)

        order_ids = [leg.get("order_id") for leg in placed_legs]
        logger.info("Position opened with order IDs: %s", order_ids)

        trade = {
            "strategy": "IRON_CONDOR",
            "signal": "IRON_CONDOR",
            "symbol": "NIFTY",
            "underlying": settings.nifty_symbol,
            "expiry": expiry,
            "strike": f"{strikes['short_call']}/{strikes['short_put']}",
            "qty": settings.order_qty,
            "entry_price": actual_net_premium,
            "entry_ltp": actual_net_premium,
            "quote_net_premium": snapshot_net_premium,
            "entry_time": current_time.isoformat(),
            "status": "OPEN",
            "size_label": payload.get("size_label", "FULL"),
            "order_ids": order_ids,
            "premiums": actual_premiums,
            "quote_premiums": snapshot_premiums,
            "strikes": strikes,
            "legs": placed_legs,
            "pricing_source": "broker_quote_snapshot" if self._is_paper_mode() else "broker_fill",
            "exit_reason": None,
            "exit_premium": None,
            "current_premium": actual_net_premium,
        }

        await self.state_manager.update(
            active_trade=trade,
            trade_count=state.trade_count + 1,
            live_pnl=0.0,
        )

        logger.info(
            "IRON_CONDOR position opened with premium=₹%.2f qty=%d source=%s",
            actual_net_premium,
            trade["qty"],
            trade["pricing_source"],
        )
        await self.event_bus.publish("TRADE_OPENED", {"trade": trade})

    async def _exit_iron_condor_trade(
        self,
        trade: dict[str, any],
        reason: str,
        current_premium: float,
    ) -> None:
        if trade.get("status") == "CLOSED":
            return

        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade:
                logger.warning("IRON_CONDOR exit skipped: no active trade")
                return

            logger.info("IRON_CONDOR exit triggered reason=%s premium=₹%.2f", reason, current_premium)

            for leg in trade.get("legs", []):
                exit_side = "BUY" if leg["side"] == "SELL" else "SELL"
                result = await self.execution_manager.execute_order(
                    {
                        "signal": reason,
                        "symbol": leg["symbol"],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "quantity": leg["filled_qty"],
                        "side": exit_side,
                    }
                )
                if result.is_uncertain or result.state != OrderState.FILLED:
                    logger.error("Iron Condor exit leg failed: %s", leg)
                    await self._handle_fatal_exception(
                        "IRON_CONDOR_EXIT_FAILED",
                        RuntimeError("Iron Condor exit failed"),
                    )
                    return

            pnl = self.iron_condor_strategy.compute_pnl(
                trade["entry_price"],
                current_premium,
                trade["qty"],
            )
            new_daily = round(state.daily_pnl + pnl["net_pnl"], 2)
            closed = {
                **trade,
                "status": "CLOSED",
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "exit_reason": reason,
                "exit_premium": current_premium,
                "gross_pnl": pnl["gross_pnl"],
                "charges": pnl["total_charges"],
                "stt": pnl["stt"],
                "net_pnl": pnl["net_pnl"],
                "trade_type": "IRON_CONDOR",
            }

            self.trade_store.append_trade(closed, new_daily)

            await self.state_manager.update(
                active_trade=None,
                daily_pnl=new_daily,
                live_pnl=0.0,
                consecutive_losses=self._next_consecutive_losses(
                    state.consecutive_losses,
                    pnl["net_pnl"],
                ),
                last_iron_condor_month=datetime.now(IST).month,
            )

            await self._enforce_global_risk_stop(new_daily, pnl["net_pnl"], state)
            logger.info("IRON_CONDOR CLOSED reason=%s net_pnl=₹%.2f", reason, pnl["net_pnl"])
            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})

    async def run(self):
        logger.info("TradingEngine started")
        try:
            await asyncio.gather(
                self._entry_listener(),
                self._monitor_loop(),
                self._health_loop(),
            )
        except asyncio.CancelledError:
            logger.info("TradingEngine cancelled (normal shutdown)")
            raise
        except Exception as exc:
            logger.critical("TradingEngine run loop failure: %s", exc, exc_info=True)
            try:
                await self.emergency_exit_active_trade(reason="SYSTEM_FAILURE")
            except Exception as exit_exc:
                logger.critical("SYSTEM_FAILURE emergency exit failed: %s", exit_exc, exc_info=True)
            try:
                await self.state_manager.update(
                    trading_enabled=False,
                    last_order_failed=True,
                    last_risk_breach="system_failure",
                )
            except Exception as state_exc:
                logger.critical("Failed to disable trading after system failure: %s", state_exc)
            raise

    async def _entry_listener(self):
        logger.info("TradingEngine listening for RISK_APPROVED events")
        queue = self.event_bus.subscribe("RISK_APPROVED")
        async for event in self.event_bus.iter_events(queue):
            logger.info("TradingEngine received event: %s", event.payload)
            await self._enter_trade(event.payload)

    async def _enter_trade(self, payload: dict):
        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            await self.state_manager.update(signal=None, signal_meta=None)

            if os.getenv("TRADING_KILL_SWITCH", "0") == "1":
                logger.critical("Kill switch enabled; rejecting entry")
                return

            if state.active_trade or state.spot_price is None or not state.trading_enabled:
                return

            if getattr(settings, "no_entry_after", None):
                no_h, no_m = map(int, str(settings.no_entry_after).split(":"))
                if datetime.now(IST).time() >= dtime(no_h, no_m):
                    logger.info("Past no-entry time — skipping")
                    return

            raw_signal = payload.get("signal")
            size_label = payload.get("size_label", "FULL")
            logger.info(
                "SIGNAL RECEIVED raw=%s size=%s spot=₹%.2f",
                raw_signal,
                size_label,
                state.spot_price or 0,
            )

            try:
                signal = self._map_signal(raw_signal)
                logger.info("SIGNAL MAPPED %s -> %s", raw_signal, signal)

                if signal == "IRON_CONDOR":
                    await self._enter_iron_condor_trade(payload, state)
                    return

                if not self._broker_available():
                    logger.error("Cannot enter directional trade: broker unavailable")
                    return

                expiry = OptionSelector.get_expiry_api()
                dte = self._days_to_expiry(expiry)
                if dte is None:
                    logger.warning("Unable to parse expiry for DTE validation: %s", expiry)
                    return
                if dte < settings.min_dte or dte > settings.max_dte:
                    logger.warning(
                        "Expiry DTE %d outside allowed range [%d,%d] for expiry=%s",
                        dte,
                        settings.min_dte,
                        settings.max_dte,
                        expiry,
                    )
                    return

                strike = OptionSelector.get_otm_strike(
                    state.spot_price,
                    signal,
                    distance=settings.otm_distance,
                )
                logger.info(
                    "STRIKE CALCULATED spot=₹%.2f signal=%s -> strike=%d",
                    state.spot_price,
                    signal,
                    strike,
                )

                symbol = await self._resolve_symbol(strike, signal)
                if not symbol:
                    logger.error("SYMBOL RESOLUTION FAILED strike=%d signal=%s", strike, signal)
                    return
                logger.info("SYMBOL RESOLVED: %s", symbol)

                quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
                ltp = self.broker.parse_ltp(quote)
                bid, ask = self.broker.parse_bid_ask(quote)
                if not ltp:
                    logger.warning("LTP UNAVAILABLE: %s", symbol)
                    return
                logger.info("LTP FETCHED %s = ₹%.2f", symbol, ltp)

                dynamic_spread_limit = self._compute_dynamic_spread_limit(symbol, ltp)
                if bid and ask and ask > bid:
                    spread = ask - bid
                    spread_pct = spread / ask
                    if spread_pct > dynamic_spread_limit:
                        logger.warning(
                            "SPREAD TOO WIDE %.2f%% > %.2f%% symbol=%s bid=%.2f ask=%.2f ltp=%.2f",
                            spread_pct * 100,
                            dynamic_spread_limit * 100,
                            symbol,
                            bid,
                            ask,
                            ltp,
                        )
                        return

                if not self._validate_trade_setup(state, raw_signal, ltp, bid, ask):
                    logger.warning("TRADE VALIDATION FAILED symbol=%s signal=%s", symbol, raw_signal)
                    return

                if ltp < settings.min_entry_premium:
                    logger.warning(
                        "PREMIUM TOO LOW ₹%.1f < min ₹%.1f — skip %s",
                        ltp,
                        settings.min_entry_premium,
                        symbol,
                    )
                    return

                if settings.min_option_volume > 0:
                    volume = _parse_volume(quote)
                    if volume < settings.min_option_volume:
                        logger.warning(
                            "LOW VOLUME %d < %d — skip %s",
                            volume,
                            settings.min_option_volume,
                            symbol,
                        )
                        return
                    logger.info("VOLUME OK %s = %d", symbol, volume)

                requested_qty = self._get_qty(size_label)
                logger.info("QTY CALCULATED size=%s -> qty=%d", size_label, requested_qty)

                logger.info("PLACING BUY ORDER: %s qty=%d", symbol, requested_qty)
                exec_result = await self.execution_manager.execute_order(
                    {
                        "signal": raw_signal,
                        "symbol": symbol,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "quantity": requested_qty,
                        "side": "BUY",
                    }
                )
                order_id = exec_result.order_id
                fill_price = exec_result.avg_price
                filled_qty = exec_result.filled_qty

                if exec_result.is_uncertain:
                    await self._handle_fatal_exception(
                        "ENTRY_EXECUTION_UNCERTAIN",
                        RuntimeError("Entry execution uncertain"),
                    )
                    return

                if exec_result.state != OrderState.FILLED:
                    logger.error("BUY ORDER FAILED: state=%s order=%s", exec_result.state, order_id or "NONE")
                    return

                is_paper = bool(order_id and order_id.startswith("PAPER-"))
                if is_paper and exec_result.state == OrderState.FILLED:
                    filled_qty = requested_qty

                if filled_qty <= 0 and not is_paper:
                    logger.error("BUY ORDER FAILED: no executable fill qty=%d", filled_qty)
                    return

                conservative_ltp = ask if ask else ltp
                if fill_price is None:
                    if is_paper:
                        logger.warning(
                            "PAPER MODE: using LTP as fill price for order=%s qty=%d",
                            order_id,
                            filled_qty,
                        )
                        entry_price = conservative_ltp
                    else:
                        logger.error(
                            "LIVE MODE: missing fill price despite FILLED state order=%s",
                            order_id,
                        )
                        return
                else:
                    entry_price = fill_price

                logger.info(
                    "BUY FILL CONFIRMED order=%s fill=₹%.2f ltp=₹%.2f mode=%s",
                    order_id,
                    entry_price,
                    ltp,
                    settings.mode.upper(),
                )

                logger.info(
                    "ENTRY SUMMARY fill=₹%.2f ltp=₹%.2f qty=%d/%d order=%s mode=%s",
                    entry_price,
                    ltp,
                    filled_qty,
                    requested_qty,
                    order_id,
                    settings.mode.upper(),
                )

                t1_qty = filled_qty // 2
                t2_qty = filled_qty - t1_qty

                trade = {
                    "symbol": symbol,
                    "strike": strike,
                    "qty": filled_qty,
                    "requested_qty": requested_qty,
                    "entry_price": entry_price,
                    "entry_ltp": ltp,
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "status": "OPEN",
                    "signal": signal,
                    "max_price": entry_price,
                    "order_id": order_id,
                    "size_label": size_label,
                    "sl_price": round(entry_price * (1 - settings.stop_loss_pct), 2),
                    "t1_price": round(entry_price * (1 + settings.t1_pct), 2),
                    "t2_price": round(entry_price * (1 + settings.t2_pct), 2),
                    "t1_hit": False,
                    "t1_booked": False,
                    "t1_qty": t1_qty,
                    "t2_qty": t2_qty,
                    "t1_pnl": 0.0,
                    "partial_fill": filled_qty < requested_qty,
                    "sl_order_id": None,
                }

                if self._is_paper_mode():
                    trade["sl_order_id"] = f"PAPER-SL-{symbol}"
                    logger.info(
                        "PAPER MODE: simulated stop-loss order symbol=%s qty=%d trigger=₹%.2f",
                        symbol,
                        filled_qty,
                        trade["sl_price"],
                    )
                else:
                    sl_resp = await self.broker.place_stop_loss_order(
                        symbol=symbol,
                        quantity=filled_qty,
                        trigger_price=trade["sl_price"],
                        side="SELL",
                    )
                    sl_order_id = sl_resp.get("orderNumber") or sl_resp.get("orderId")
                    if sl_resp.get("status") != "Success" or not sl_order_id:
                        logger.critical(
                            "SL placement failed after entry fill; forcing exit and halting. resp=%s",
                            sl_resp,
                        )
                        await self._force_exit_and_halt(
                            symbol=symbol,
                            qty=filled_qty,
                            reason="STOP_LOSS_PLACEMENT_FAILED",
                        )
                        return
                    trade["sl_order_id"] = sl_order_id

                await self.state_manager.update(
                    active_trade=trade,
                    trade_count=state.trade_count + 1,
                )

                if not is_paper:
                    await self._validate_post_order_position(symbol, filled_qty, "ENTRY")
                else:
                    logger.info(
                        "PAPER MODE: skipping broker position validation symbol=%s qty=%d",
                        symbol,
                        filled_qty,
                    )

                logger.info(
                    "TRADE OPENED %s qty=%d entry=₹%.2f SL=₹%.2f T1=₹%.2f T2=₹%.2f%s",
                    symbol,
                    filled_qty,
                    entry_price,
                    trade["sl_price"],
                    trade["t1_price"],
                    trade["t2_price"],
                    " [PARTIAL FILL]" if trade["partial_fill"] else "",
                )
                logger.info(
                    "TRADE DETAILS strike=%d signal=%s t1_qty=%d t2_qty=%d order=%s",
                    strike,
                    signal,
                    t1_qty,
                    t2_qty,
                    order_id,
                )

                if self.strategy:
                    self.strategy.set_already_traded_today()
                    logger.info("Daily trade limit locked (after successful entry)")

                await self.event_bus.publish("TRADE_OPENED", {"trade": trade})

            except Exception as exc:
                logger.error("ENTRY FAILED: %s", exc, exc_info=True)
                await self._handle_fatal_exception("entry", exc)

    async def _monitor_loop(self):
        while True:
            await asyncio.sleep(2)

            state = await self.state_manager.snapshot()
            trade = state.active_trade

            if not trade or trade.get("status") == "CLOSED":
                continue

            if trade.get("strategy") == "IRON_CONDOR":
                if not self.iron_condor_strategy:
                    continue

                current_time = datetime.now(IST)
                entry_time = datetime.fromisoformat(trade["entry_time"])

                try:
                    current_premium, current_legs = await self._get_live_iron_condor_close_snapshot(trade)
                    pricing_source = "broker_quote_snapshot"
                except Exception as exc:
                    logger.warning("IC live snapshot failed; using model fallback: %s", exc)
                    current_premium = self.iron_condor_strategy.estimate_current_premium(
                        trade["entry_price"],
                        entry_time,
                        current_time,
                    )
                    current_legs = []
                    pricing_source = "model_fallback"

                live_pnl = round((float(trade["entry_price"]) - float(current_premium)) * float(trade.get("qty", 0)), 2)

                updated_trade = {
                    **trade,
                    "current_premium": round(float(current_premium), 2),
                    "current_legs": current_legs,
                    "current_pricing_source": pricing_source,
                }
                await self.state_manager.update(
                    active_trade=updated_trade,
                    live_pnl=live_pnl,
                )

                should_force_exit, reason = self.expiry_safety.should_force_exit(
                    current_time,
                    entry_time,
                )
                if should_force_exit:
                    logger.critical("FORCE CLOSING: %s", reason)
                    await self._exit_iron_condor_trade(
                        updated_trade,
                        "EXPIRY_SAFETY_FORCED_EXIT",
                        current_premium,
                    )
                    continue

                reason = self.iron_condor_strategy.get_exit_reason(
                    entry_time,
                    current_time,
                    updated_trade["entry_price"],
                    current_premium,
                )
                if reason:
                    await self._exit_iron_condor_trade(updated_trade, reason, current_premium)
                continue

            try:
                if not self._broker_available():
                    continue

                quote = await self.broker.get_quote(symbol_name=trade["symbol"], exchange="NFO")
                ltp = self.broker.parse_ltp(quote)
                if not ltp or ltp <= 0:
                    continue

                entry = trade["entry_price"]
                t1_booked = trade.get("t1_booked", False)

                sl_price = trade.get("sl_price", round(entry * (1 - settings.stop_loss_pct), 2))
                t1_price = trade.get("t1_price", round(entry * (1 + settings.t1_pct), 2))
                t2_price = trade.get("t2_price", round(entry * (1 + settings.t2_pct), 2))

                remaining_qty = trade.get("t2_qty", trade["qty"] // 2) if t1_booked else trade["qty"]
                live_pnl = (ltp - entry) * remaining_qty if ltp > 0 else 0.0
                await self.state_manager.update(live_pnl=round(live_pnl, 2))

                if ltp > trade.get("max_price", entry):
                    trade["max_price"] = ltp
                    await self.state_manager.update(active_trade=trade)

                breakeven_trigger = getattr(settings, "breakeven_at_pct", 0.20)
                if not t1_booked and ltp >= entry * (1 + breakeven_trigger):
                    new_sl = max(sl_price, entry * 1.001)
                    if new_sl > sl_price:
                        sl_price = round(new_sl, 2)
                        trade["sl_price"] = sl_price
                        await self.state_manager.update(active_trade=trade)
                        logger.info("SL upgraded to breakeven: ₹%.2f (ltp ₹%.2f)", sl_price, ltp)

                trail_sl = round(trade["max_price"] * (1 - settings.trailing_pct), 2)

                sq_h, sq_m = map(int, settings.square_off.split(":"))
                now = datetime.now(IST)

                if ltp <= sl_price:
                    await self._exit_trade(trade, "STOPLOSS", ltp)
                    continue

                if not t1_booked and ltp >= t1_price:
                    await self._book_partial(trade, ltp)
                    continue

                if t1_booked:
                    if ltp >= t2_price:
                        await self._exit_remaining(trade, "TARGET_2", ltp)
                        continue

                    if ltp <= trail_sl:
                        await self._exit_remaining(trade, "TRAIL_STOP", ltp)
                        continue

                if now.time() >= dtime(sq_h, sq_m):
                    await self._exit_trade(trade, "EOD_SQUAREOFF", ltp)
                    continue

            except Exception as exc:
                logger.error("Monitor loop: %s", exc, exc_info=True)
                await self._handle_fatal_exception("monitor_loop", exc)

    async def _book_partial(self, trade: dict, ltp: float):
        if trade.get("status") == "CLOSED":
            logger.info("BOOK PARTIAL SKIPPED: trade already closed")
            return

        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade:
                logger.warning("BOOK PARTIAL SKIPPED: no active trade in state")
                return

            symbol = trade["symbol"]
            t1_qty = trade.get("t1_qty", trade["qty"] // 2)
            logger.info("BOOKING PARTIAL: %s qty=%d ltp=₹%.2f", symbol, t1_qty, ltp)

            sell_id, fill_price = await self._sell_with_retry(symbol, t1_qty, "TARGET_1")
            if not sell_id:
                logger.critical("T1 SELL FAILED — position may be open! %s qty=%d", symbol, t1_qty)
                await self.event_bus.publish(
                    "SELL_FAILED_CRITICAL",
                    {"symbol": symbol, "qty": t1_qty, "reason": "TARGET_1"},
                )
                return

            quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
            bid, _ = self.broker.parse_bid_ask(quote)
            conservative_ltp = bid if bid else ltp
            exit_price = fill_price if fill_price else conservative_ltp
            t1_pnl = round((exit_price - trade["entry_price"]) * t1_qty, 2)

            logger.info(
                "T1 PARTIAL FILL: order=%s fill=₹%.2f pnl=₹%.2f",
                sell_id,
                exit_price,
                t1_pnl,
            )

            trade["t1_booked"] = True
            trade["t1_pnl"] = t1_pnl
            trade["t1_exit_price"] = exit_price

            await self.state_manager.update(active_trade=trade)

            logger.info(
                "PARTIAL BOOKED: %s t1_pnl=₹%.2f remaining_qty=%d",
                symbol,
                t1_pnl,
                trade.get("t2_qty", trade["qty"] // 2),
            )

            await self.event_bus.publish("PARTIAL_BOOKED", {"trade": trade})

    async def _exit_remaining(self, trade: dict, reason: str, ltp: float):
        if trade.get("status") == "CLOSED":
            logger.info("EXIT REMAINING SKIPPED: trade already closed")
            return

        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade:
                logger.warning("EXIT REMAINING SKIPPED: no active trade in state")
                return

            symbol = trade["symbol"]
            t2_qty = trade.get("t2_qty", trade["qty"] // 2)

            logger.info("EXITING REMAINING: %s qty=%d reason=%s ltp=₹%.2f", symbol, t2_qty, reason, ltp)

            sell_id, fill_price = await self._sell_with_retry(symbol, t2_qty, reason)
            if not sell_id:
                logger.critical(
                    "T2 SELL FAILED — position may be open! %s qty=%d reason=%s",
                    symbol,
                    t2_qty,
                    reason,
                )
                await self.event_bus.publish(
                    "SELL_FAILED_CRITICAL",
                    {"symbol": symbol, "qty": t2_qty, "reason": reason},
                )
                await self._handle_fatal_exception(
                    f"exit_remaining_sell_failed:{reason}",
                    RuntimeError("Remaining position sell failed"),
                )
                return

            quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
            bid, _ = self.broker.parse_bid_ask(quote)
            conservative_ltp = bid if bid else ltp
            exit_price = fill_price if fill_price else conservative_ltp
            t2_pnl = round((exit_price - trade["entry_price"]) * t2_qty, 2)
            total_pnl = round(trade.get("t1_pnl", 0) + t2_pnl, 2)
            new_daily = round(state.daily_pnl + t2_pnl, 2)

            logger.info(
                "T2 EXIT FILL: order=%s fill=₹%.2f pnl=₹%.2f total_pnl=₹%.2f",
                sell_id,
                exit_price,
                t2_pnl,
                total_pnl,
            )

            position_closed = await self._ensure_position_closed(symbol, reason, fallback_qty=t2_qty)
            if not position_closed:
                await self._handle_fatal_exception(
                    f"exit_remaining_verify_failed:{reason}",
                    RuntimeError("Broker position still open after exit"),
                )
                return

            trade["status"] = "CLOSED"

            closed = {
                **trade,
                "exit_price": exit_price,
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "status": "CLOSED",
                "exit_reason": reason,
                "pnl": total_pnl,
                "sell_order_id": sell_id,
            }

            self.trade_store.append_trade(closed, new_daily)

            await self.state_manager.update(
                active_trade=None,
                daily_pnl=new_daily,
                live_pnl=0.0,
                consecutive_losses=self._next_consecutive_losses(state.consecutive_losses, total_pnl),
            )
            await self._enforce_global_risk_stop(new_daily, total_pnl, state)

            logger.info(
                "TRADE CLOSED (REMAINING): %s reason=%s exit=₹%.2f pnl=₹%.2f daily_pnl=₹%.2f",
                symbol,
                reason,
                exit_price,
                total_pnl,
                new_daily,
            )

            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})

    async def _exit_trade(self, trade: dict, reason: str, ltp: float):
        if trade.get("status") == "CLOSED":
            logger.info("EXIT SKIPPED: trade already closed")
            return

        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade:
                logger.warning("EXIT SKIPPED: no active trade in state")
                return

            symbol = trade["symbol"]
            qty = trade.get("t2_qty", trade["qty"] // 2) if trade.get("t1_booked") else trade["qty"]

            logger.info("EXITING TRADE: %s qty=%d reason=%s ltp=₹%.2f", symbol, qty, reason, ltp)

            sell_exec = await self.execution_manager.execute_order(
                {
                    "signal": reason,
                    "symbol": symbol,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "quantity": qty,
                    "side": "SELL",
                }
            )
            sell_id = sell_exec.order_id
            fill_price = sell_exec.avg_price

            if sell_exec.is_uncertain:
                await self._handle_fatal_exception(
                    f"SELL_EXECUTION_UNCERTAIN:{reason}",
                    RuntimeError("Sell execution result uncertain"),
                )
                return

            if not sell_id:
                logger.critical("SELL FAILED — position may be open! %s qty=%d reason=%s", symbol, qty, reason)
                await self.event_bus.publish(
                    "SELL_FAILED_CRITICAL",
                    {"symbol": symbol, "qty": qty, "reason": reason},
                )
                await self._handle_fatal_exception(
                    f"exit_trade_sell_failed:{reason}",
                    RuntimeError("Full exit sell failed"),
                )
                return

            quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
            bid, _ = self.broker.parse_bid_ask(quote)
            conservative_ltp = bid if bid else ltp
            exit_price = fill_price if fill_price else conservative_ltp
            exit_pnl = round((exit_price - trade["entry_price"]) * qty, 2)
            total_pnl = round(trade.get("t1_pnl", 0) + exit_pnl, 2)
            new_daily = round(state.daily_pnl + exit_pnl, 2)

            logger.info(
                "EXIT FILL: order=%s fill=₹%.2f pnl=₹%.2f total_pnl=₹%.2f",
                sell_id,
                exit_price,
                exit_pnl,
                total_pnl,
            )

            position_closed = await self._ensure_position_closed(symbol, reason, fallback_qty=qty)
            if not position_closed:
                await self._handle_fatal_exception(
                    f"exit_trade_verify_failed:{reason}",
                    RuntimeError("Broker position still open after full exit"),
                )
                return

            trade["status"] = "CLOSED"

            closed = {
                **trade,
                "exit_price": exit_price,
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "status": "CLOSED",
                "exit_reason": reason,
                "pnl": total_pnl,
                "sell_order_id": sell_id,
            }

            self.trade_store.append_trade(closed, new_daily)

            await self.state_manager.update(
                active_trade=None,
                daily_pnl=new_daily,
                live_pnl=0.0,
                consecutive_losses=self._next_consecutive_losses(state.consecutive_losses, total_pnl),
            )
            await self._enforce_global_risk_stop(new_daily, total_pnl, state)

            logger.info(
                "TRADE CLOSED: %s reason=%s exit=₹%.2f pnl=₹%.2f daily_pnl=₹%.2f",
                symbol,
                reason,
                exit_price,
                total_pnl,
                new_daily,
            )

            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})

    async def _sell_with_retry(self, symbol: str, qty: int, reason: str) -> tuple[str | None, float | None]:
        if self._is_paper_mode():
            ltp = await self._paper_quote_price(symbol, fallback=0.0)
            sell_id = self._paper_safe_order_id("SELL", symbol)
            logger.info(
                "PAPER MODE: simulated SELL symbol=%s qty=%d reason=%s fill=%.2f",
                symbol,
                qty,
                reason,
                ltp,
            )
            return sell_id, ltp

        for attempt in range(1, _SELL_MAX_RETRIES + 1):
            try:
                sell_id, fill_price = await self.broker.place_order_and_wait_fill(
                    symbol=symbol,
                    side="SELL",
                    quantity=qty,
                )
                if sell_id:
                    logger.info(
                        "SELL OK attempt=%d reason=%s order=%s fill=%s",
                        attempt,
                        reason,
                        sell_id,
                        f"₹{fill_price:.2f}" if fill_price else "N/A",
                    )
                    return sell_id, fill_price
                logger.warning(
                    "SELL attempt %d/%d no order_id reason=%s",
                    attempt,
                    _SELL_MAX_RETRIES,
                    reason,
                )
            except Exception as exc:
                logger.error(
                    "SELL attempt %d/%d exception reason=%s: %s",
                    attempt,
                    _SELL_MAX_RETRIES,
                    reason,
                    exc,
                )

            if attempt < _SELL_MAX_RETRIES:
                await asyncio.sleep(_SELL_RETRY_DELAY)

        logger.critical(
            "EMERGENCY SELL: all %d retries failed — placing emergency order %s qty=%d",
            _SELL_MAX_RETRIES,
            symbol,
            qty,
        )
        try:
            resp = await self.broker.place_order(symbol=symbol, side="SELL", quantity=qty)
            eid = resp.get("orderNumber") or resp.get("orderId")
            if eid:
                await asyncio.sleep(2)
                fp = await self.broker.get_actual_fill_price(eid)
                logger.critical("EMERGENCY SELL placed order=%s fill=%s", eid, fp)
                return eid, fp
        except Exception as exc:
            logger.critical("EMERGENCY SELL also failed: %s", exc)

        return None, None

    async def _buy_with_retry(self, symbol: str, requested_qty: int) -> tuple[str | None, float | None, int]:
        if self._is_paper_mode():
            ltp = await self._paper_quote_price(symbol, fallback=0.0)
            order_id = self._paper_safe_order_id("BUY", symbol)
            logger.info(
                "PAPER MODE: simulated BUY symbol=%s qty=%d fill=%.2f",
                symbol,
                requested_qty,
                ltp,
            )
            return order_id, ltp, requested_qty

        for attempt in range(1, _ENTRY_MAX_RETRIES + 1):
            try:
                order_id, fill_price = await self.broker.place_order_and_wait_fill(
                    symbol=symbol,
                    side="BUY",
                    quantity=requested_qty,
                )
                if not order_id:
                    logger.warning("BUY attempt=%d/%d failed: no order_id", attempt, _ENTRY_MAX_RETRIES)
                    continue

                if order_id.startswith("PAPER-"):
                    return order_id, fill_price, requested_qty

                status, filled_qty, broker_avg = await self._await_fill_confirmation(
                    order_id=order_id,
                    requested_qty=requested_qty,
                    side="BUY",
                )
                if broker_avg and not fill_price:
                    fill_price = broker_avg

                data = status.get("orderDetails") or status.get("data") or status
                if isinstance(data, list):
                    data = data[0] if data else {}
                broker_state = str(data.get("orderStatus") or data.get("status") or "").upper()

                if broker_state in ("REJECTED", "CANCELLED", "CANCELED"):
                    logger.warning(
                        "BUY rejected/cancelled attempt=%d/%d order=%s state=%s",
                        attempt,
                        _ENTRY_MAX_RETRIES,
                        order_id,
                        broker_state,
                    )
                    continue

                if filled_qty > 0:
                    if filled_qty < requested_qty:
                        await self._safe_cancel_order(order_id)
                    return order_id, fill_price, filled_qty

                logger.warning(
                    "BUY no-fill attempt=%d/%d order=%s state=%s",
                    attempt,
                    _ENTRY_MAX_RETRIES,
                    order_id,
                    broker_state or "UNKNOWN",
                )
            except Exception as exc:
                logger.error("BUY attempt=%d/%d exception: %s", attempt, _ENTRY_MAX_RETRIES, exc)
                if attempt == _ENTRY_MAX_RETRIES:
                    raise

            if attempt < _ENTRY_MAX_RETRIES:
                await asyncio.sleep(_ENTRY_RETRY_DELAY * attempt)

        return None, None, 0

    async def emergency_exit_active_trade(self, reason: str = "EMERGENCY") -> bool:
        state = await self.state_manager.snapshot()
        trade = state.active_trade
        if not trade:
            return True

        symbol = trade.get("symbol")
        qty = trade.get("t2_qty", trade.get("qty", 0) // 2) if trade.get("t1_booked") else trade.get("qty", 0)

        if not symbol or qty <= 0:
            await self.state_manager.update(active_trade=None, live_pnl=0.0)
            return False

        sell_id, _ = await self._sell_with_retry(symbol, qty, reason)
        if not sell_id:
            return False

        closed = await self._ensure_position_closed(symbol, reason, fallback_qty=qty)
        if closed:
            await self.state_manager.update(active_trade=None, live_pnl=0.0)
        return closed

    async def _health_loop(self):
        while True:
            await asyncio.sleep(60)

            if not self._broker_available():
                continue

            try:
                if not await self.broker.healthcheck():
                    logger.warning("Healthcheck failed — re-login")
                    await self.broker.login()
                    state = await self.state_manager.snapshot()
                    if state.active_trade and not self._is_paper_mode():
                        raise RuntimeError("Broker healthcheck failed during active trade")
            except Exception as exc:
                logger.error("Health loop: %s", exc)
                await self._handle_fatal_exception("health_loop", exc)

    def _get_qty(self, size_label: str) -> int:
        base = settings.order_qty
        if size_label == "FULL":
            return max(base, 1)
        if size_label == "MEDIUM":
            return max(int(round(base * 0.75)), 1)
        if size_label == "HALF":
            return max(int(round(base * 0.50)), 1)
        return base

    async def _resolve_symbol(self, strike: int, signal: str) -> str | None:
        if not self._broker_available():
            logger.error("Cannot resolve symbol: broker unavailable")
            return None

        key = f"{strike}_{signal}"
        if key in self._symbol_cache:
            return self._symbol_cache[key]

        opt_type = OptionSelector.get_option_type(signal)
        expiry = OptionSelector.get_expiry_api()

        logger.info(
            "SYMBOL LOOKUP requested_strike=%s type=%s expiry=%s",
            strike,
            opt_type,
            expiry,
        )

        chain = await self.broker.get_option_chain(
            search_symbol_name=settings.nifty_symbol,
            exchange="NFO",
            expiry_date=expiry,
            strike_price=str(strike),
            option_type=opt_type,
        )

        if isinstance(chain, dict) and chain.get("validationErrors"):
            logger.warning(
                "SAMCO validation error on specific-strike call: errors=%s (falling back to full chain)",
                chain.get("validationErrors"),
            )
            chain = None

        rows = self._extract_chain_rows(chain) if chain else []

        if not rows:
            if chain is not None:
                logger.warning(
                    "Specific-strike chain empty: expiry=%s strike=%s type=%s response_keys=%s response_type=%s",
                    expiry,
                    strike,
                    opt_type,
                    list(chain.keys()) if isinstance(chain, dict) else [],
                    type(chain).__name__,
                )
            logger.info("Retrying with full chain (strike_price='0')")

            chain = await self.broker.get_option_chain(
                search_symbol_name=settings.nifty_symbol,
                exchange="NFO",
                expiry_date=expiry,
                strike_price="0",
                option_type=opt_type,
            )

            if isinstance(chain, dict) and chain.get("validationErrors"):
                logger.error(
                    "SAMCO validation error on full chain too: errors=%s expiry=%s type=%s",
                    chain.get("validationErrors"),
                    expiry,
                    opt_type,
                )
                return None

            rows = self._extract_chain_rows(chain)

        if not rows:
            resp_keys = list(chain.keys()) if isinstance(chain, dict) else []
            logger.error(
                "Option chain empty after both attempts: expiry=%s strike=%s type=%s response_keys=%s",
                expiry,
                strike,
                opt_type,
                resp_keys,
            )
            return None

        best_sym = None
        best_strike = None
        best_diff = float("inf")
        available_strikes: list[float] = []

        for row in rows:
            try:
                row_strike = float(row.get("strikePrice", 0))
                available_strikes.append(row_strike)
                diff = abs(row_strike - strike)
                if diff < best_diff:
                    best_diff = diff
                    best_sym = row.get("tradingSymbol")
                    best_strike = row_strike
            except (TypeError, ValueError):
                continue

        if not best_sym:
            logger.error("No valid strike in chain near %s. Available strikes: %s", strike, sorted(available_strikes)[:20])
            return None

        if best_diff > 0:
            logger.warning(
                "Strike snap: requested=%s -> got nearest=%s (diff=%s)",
                strike,
                best_strike,
                best_diff,
            )

        self._symbol_cache[key] = best_sym
        logger.info(
            "SYMBOL RESOLVED: requested_strike=%s type=%s -> %s (snap_diff=%s)",
            strike,
            opt_type,
            best_sym,
            best_diff,
        )
        return best_sym

    @staticmethod
    def _extract_chain_rows(chain: dict | list | None) -> list[dict]:
        if not chain:
            return []

        if isinstance(chain, list):
            return chain

        rows = (
            chain.get("optionChainDetails")
            or chain.get("data")
            or chain.get("rows")
            or chain.get("result")
            or []
        )

        if isinstance(rows, dict):
            return [rows]
        if isinstance(rows, list):
            return rows
        return []

    def clear_cache(self):
        self._symbol_cache.clear()

    async def _await_fill_confirmation(
        self,
        order_id: str,
        requested_qty: int,
        side: str,
    ) -> tuple[dict, int, float | None]:
        latest_status: dict = {}
        latest_filled = 0
        latest_avg: float | None = None

        for attempt in range(1, _FILL_CONFIRM_ATTEMPTS + 1):
            status = await self.broker.get_order_status(order_id)
            latest_status = status or {}
            latest_filled = _parse_filled_qty(latest_status, requested_qty)
            latest_avg = await self.broker.get_actual_fill_price(order_id)

            data = latest_status.get("orderDetails") or latest_status.get("data") or latest_status
            if isinstance(data, list):
                data = data[0] if data else {}
            broker_state = str(data.get("orderStatus") or data.get("status") or "").upper()

            if latest_filled >= requested_qty or broker_state in ("COMPLETE", "FILLED", "TRADED"):
                return latest_status, min(latest_filled, requested_qty), latest_avg

            if broker_state in ("REJECTED", "CANCELLED", "CANCELED"):
                logger.error("%s order terminal state=%s order=%s", side, broker_state, order_id)
                return latest_status, latest_filled, latest_avg

            logger.warning(
                "%s delayed fill response attempt=%d/%d order=%s filled=%d/%d status=%s",
                side,
                attempt,
                _FILL_CONFIRM_ATTEMPTS,
                order_id,
                latest_filled,
                requested_qty,
                broker_state or "UNKNOWN",
            )
            await asyncio.sleep(_FILL_CONFIRM_DELAY)

        return latest_status, latest_filled, latest_avg

    async def _safe_cancel_order(self, order_id: str) -> None:
        if self._is_paper_mode():
            logger.info("PAPER MODE: skipping cancel order=%s", order_id)
            return
        try:
            await self.broker.cancel_order(order_id)
        except Exception as exc:
            logger.warning("Cancel failed order=%s err=%s", order_id, exc)

    async def _ensure_position_closed(self, symbol: str, reason: str, fallback_qty: int) -> bool:
        if self._is_paper_mode():
            logger.info(
                "PAPER MODE: treating position as closed symbol=%s reason=%s fallback_qty=%d",
                symbol,
                reason,
                fallback_qty,
            )
            return True

        for attempt in range(1, _EXIT_VERIFY_ATTEMPTS + 1):
            open_qty = await self._get_open_position_qty(symbol)
            if open_qty < 0:
                logger.critical(
                    "EXIT VALIDATION INCONCLUSIVE: position API unavailable symbol=%s reason=%s",
                    symbol,
                    reason,
                )
                return False
            if open_qty <= 0:
                return True

            logger.warning(
                "EXIT VALIDATION FAILED attempt=%d/%d symbol=%s open_qty=%d reason=%s",
                attempt,
                _EXIT_VERIFY_ATTEMPTS,
                symbol,
                open_qty,
                reason,
            )
            if attempt < _EXIT_VERIFY_ATTEMPTS:
                retry_qty = open_qty if open_qty > 0 else fallback_qty
                await self._sell_with_retry(symbol, retry_qty, f"{reason}_RETRY_{attempt}")
                await asyncio.sleep(_EXIT_VERIFY_DELAY)

        return False

    async def _get_open_position_qty(self, symbol: str) -> int:
        if self._is_paper_mode():
            return 0

        try:
            positions = await self.broker.get_positions()
        except Exception as exc:
            logger.warning("Position fetch failed for exit validation: %s", exc)
            return -1

        total = 0
        symbol_upper = str(symbol).upper()

        for pos in positions or []:
            ts = str(pos.get("tradingSymbol") or pos.get("symbolName") or "").upper()
            if ts != symbol_upper:
                continue
            for key in ("netQty", "netQuantity", "quantity", "netPosition"):
                try:
                    total = int(float(str(pos.get(key, 0)).replace(",", "").strip()))
                    break
                except (TypeError, ValueError):
                    continue

        return max(total, 0)

    async def _validate_post_order_position(self, symbol: str, expected_qty: int, context: str) -> None:
        if self._is_paper_mode():
            logger.info("PAPER MODE: skipping post-order position validation symbol=%s", symbol)
            return

        if expected_qty <= 0:
            raise RuntimeError(f"{context} invalid expected qty={expected_qty}")

        observed_qty = await self._get_open_position_qty(symbol)
        if observed_qty < 0:
            raise RuntimeError(f"{context} position check failed: broker positions unavailable")
        if observed_qty < expected_qty:
            raise RuntimeError(
                f"{context} position mismatch expected>={expected_qty} observed={observed_qty}"
            )

    def _compute_dynamic_spread_limit(self, symbol: str, ltp: float) -> float:
        history = self._ltp_history[symbol]
        history.append(float(ltp))
        base_limit = float(getattr(settings, "max_spread_pct", 0.05))
        hard_cap = float(getattr(settings, "dynamic_spread_max_pct", 0.12))
        vol_multiplier = float(getattr(settings, "dynamic_spread_vol_multiplier", 2.0))

        if ltp <= 50:
            liquidity_floor = 0.08
        elif ltp <= 100:
            liquidity_floor = 0.06
        else:
            liquidity_floor = 0.04

        if len(history) < 3:
            return min(max(base_limit, liquidity_floor), hard_cap)

        max_ltp = max(history)
        min_ltp = min(history)
        realized_vol = ((max_ltp - min_ltp) / ltp) if ltp > 0 else 0.0
        dynamic = max(base_limit, liquidity_floor, realized_vol * vol_multiplier)
        return min(dynamic, hard_cap)

    async def _handle_fatal_exception(self, context: str, exc: Exception) -> None:
        async with self._fatal_lock:
            state = await self.state_manager.snapshot()

            if state.trading_enabled:
                await self.state_manager.update(
                    trading_enabled=False,
                    last_order_failed=True,
                    last_risk_breach=f"fatal_exception:{context}",
                )
                logger.critical("HARD FAIL SAFE ACTIVATED context=%s err=%s", context, exc)

            if state.active_trade:
                closed = await self.emergency_exit_active_trade(reason=f"HARD_FAIL_{context}")
                if not closed:
                    logger.critical("HARD FAIL EXIT UNSUCCESSFUL context=%s", context)

    async def _force_exit_and_halt(self, symbol: str, qty: int, reason: str) -> None:
        await self.state_manager.update(
            trading_enabled=False,
            last_order_failed=True,
            last_risk_breach=reason,
        )

        if self._is_paper_mode():
            logger.critical(
                "PAPER MODE: force exit + halt symbol=%s qty=%s reason=%s",
                symbol,
                qty,
                reason,
            )
            await self.event_bus.publish(
                "ORDER_UNCERTAIN",
                {"reason": reason, "symbol": symbol, "qty": qty},
            )
            return

        try:
            await self.broker.place_order(symbol=symbol, side="SELL", quantity=qty)
        except Exception as exc:
            logger.critical(
                "Forced exit failed symbol=%s qty=%s err=%s",
                symbol,
                qty,
                exc,
                exc_info=True,
            )

        try:
            await self.broker.cancel_all_open_orders()
        except Exception as exc:
            logger.warning("cancel_all_open_orders failed: %s", exc)

        await self.event_bus.publish(
            "ORDER_UNCERTAIN",
            {"reason": reason, "symbol": symbol, "qty": qty},
        )

    def _validate_trade_setup(
        self,
        state,
        raw_signal: str,
        ltp: float,
        bid: float | None,
        ask: float | None,
    ) -> bool:
        try:
            signal = self._map_signal(raw_signal)
        except ValueError:
            logger.warning("Invalid signal for validation: %s", raw_signal)
            return False

        if ask and ltp > 0:
            spike_pct = abs((ask - ltp) / ltp)
            if spike_pct > settings.max_option_spike_pct:
                logger.warning("Spike filter blocked: spike_pct=%.2f%%", spike_pct * 100)
                return False

        if signal == "CALL" and state.orb_high:
            min_break = state.orb_high + settings.breakout_buffer
            max_break = state.orb_high * (1 + settings.max_breakout_extension_pct)
            if state.spot_price < min_break or state.spot_price > max_break:
                logger.warning(
                    "Fake breakout/spike block CALL: spot=%.2f range=[%.2f, %.2f]",
                    state.spot_price,
                    min_break,
                    max_break,
                )
                return False

        if signal == "PUT" and state.orb_low:
            max_break = state.orb_low - settings.breakout_buffer
            min_break = state.orb_low * (1 - settings.max_breakout_extension_pct)
            if state.spot_price > max_break or state.spot_price < min_break:
                logger.warning(
                    "Fake breakout/spike block PUT: spot=%.2f range=[%.2f, %.2f]",
                    state.spot_price,
                    min_break,
                    max_break,
                )
                return False

        return True

    def _days_to_expiry(self, expiry: str) -> int | None:
        try:
            expiry_date = datetime.fromisoformat(expiry).date()
        except ValueError:
            try:
                expiry_date = datetime.strptime(expiry, "%d-%b-%Y").date()
            except ValueError:
                return None
        return max((expiry_date - datetime.now(IST).date()).days, 0)

    @staticmethod
    def _next_consecutive_losses(current: int, trade_pnl: float) -> int:
        return (current + 1) if trade_pnl < 0 else 0

    async def _enforce_global_risk_stop(self, new_daily: float, trade_pnl: float, state) -> None:
        new_losses = self._next_consecutive_losses(state.consecutive_losses, trade_pnl)
        hit_loss_streak = new_losses >= settings.max_consecutive_losses
        peak_equity = state.peak_equity or settings.capital
        equity_now = settings.capital + new_daily
        drawdown = ((peak_equity - equity_now) / peak_equity) if peak_equity > 0 else 0.0

        if hit_loss_streak or drawdown >= settings.max_drawdown_pct:
            await self.state_manager.update(
                trading_enabled=False,
                last_risk_breach=(
                    f"consecutive_losses_{new_losses}"
                    if hit_loss_streak
                    else f"drawdown_{drawdown:.2%}"
                ),
            )
            logger.critical(
                "GLOBAL RISK STOP: streak=%d drawdown=%.2f%% trading disabled",
                new_losses,
                drawdown * 100,
            )
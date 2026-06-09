# backend/app/engine/trading_engine.py
from __future__ import annotations

import asyncio
import os
from collections import defaultdict, deque
from datetime import datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.core.logging_config import setup_file_logging
from backend.app.engine.execution_manager import ExecutionManager, OrderState
from backend.app.engine.order_execution import (
    ExpiryDaySafetyProtocol,
    MarginUtilizationMonitor,
    OrderExecutionSequence,
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

_QUOTE_DEGRADED_WARN_TICKS = 15
_QUOTE_DEGRADED_CRITICAL_TICKS = 150

_PARTIAL_FILL_RETRY_DELAY = 1.5


def _parse_volume(quote: dict) -> int:
    def _int(value: Any) -> int:
        try:
            return int(float(str(value).replace(",", "").strip()))
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
    def _int(value: Any) -> int:
        try:
            return int(float(str(value).replace(",", "").strip()))
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


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


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
        self._ic_quote_cache: dict[str, dict[str, Any]] = {}
        self._throttled_log_times: dict[str, datetime] = {}

        # Rolling market-tick history for trend and IV-rank computation.
        self._spot_history: deque[float] = deque(maxlen=60)   # ~60 s at 1-s poll
        self._engine_iv_history: deque[float] = deque(maxlen=21600)  # full 6-hr session at 1-s poll
        self._session_open_spot: float | None = None
        self._session_open_date: object | None = None  # datetime.date

        self._ic_fallback_streak = 0
        self._ic_fallback_alert_level = 0
        self._directional_bad_quote_streak = 0
        self._directional_bad_quote_alert_level = 0
        self._paper_order_seq = 0  # Issue #39: monotonic counter for unique paper order IDs

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

        self.expiry_safety = ExpiryDaySafetyProtocol(settings=settings, logger=logger)

        self.margin_monitor = MarginUtilizationMonitor(
            total_capital=settings.capital,
            safety_buffer=float(getattr(settings, "margin_safety_buffer", 5000)),
            logger=logger,
        )

        self.ws_resilience = None

        try:
            sq_h, sq_m = map(int, str(settings.square_off).strip().split(":")[:2])
            self._square_off_time = dtime(sq_h, sq_m)
        except (TypeError, ValueError, AttributeError) as exc:
            logger.error(
                "Invalid SQUARE_OFF=%r in settings — falling back to 15:25 (%s)",
                getattr(settings, "square_off", None),
                exc,
            )
            self._square_off_time = dtime(15, 25)

        no_entry_raw = getattr(settings, "no_entry_after", None)
        self._no_entry_after_time: dtime | None = None
        if no_entry_raw:
            try:
                ne_h, ne_m = map(int, str(no_entry_raw).strip().split(":")[:2])
                self._no_entry_after_time = dtime(ne_h, ne_m)
            except (TypeError, ValueError, AttributeError) as exc:
                logger.error(
                    "Invalid NO_ENTRY_AFTER=%r in settings — disabling no-entry cutoff (%s)",
                    no_entry_raw,
                    exc,
                )

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

    def _broker_quote_timeout_seconds(self) -> float:
        return float(getattr(settings, "broker_quote_timeout_seconds", 5) or 5)

    async def _fetch_nfo_quote(self, symbol: str) -> dict:
        if not self._broker_available():
            raise RuntimeError("Broker unavailable for NFO quote")

        return await asyncio.wait_for(
            self.broker.get_quote(symbol_name=symbol, exchange="NFO"),
            timeout=self._broker_quote_timeout_seconds(),
        )

    def _broker_position_checks_enabled(self) -> bool:
        """
        Broker position validation must run in live mode and in paper mode when
        PAPER_MODE_USE_BROKER=true. Only pure offline paper mode skips broker
        position APIs.
        """
        return self._broker_available() and (
            not self._is_paper_mode() or self._paper_mode_use_broker()
        )

    def _ic_quote_cache_ttl_seconds(self) -> int:
        # IC exits may use cached quotes only briefly. Keep default tight for safety.
        return int(getattr(settings, "ic_quote_cache_ttl_seconds", 3) or 3)

    def _is_valid_quote_prices(self, bid: float, ask: float, ltp: float) -> bool:
        return bid > 0 or ask > 0 or ltp > 0

    def _log_throttled(
        self,
        key: str,
        seconds: float,
        level: str,
        message: str,
        *args: Any,
    ) -> None:
        now = datetime.now(timezone.utc)
        last = self._throttled_log_times.get(key)
        if last and (now - last).total_seconds() < seconds:
            return

        self._throttled_log_times[key] = now
        getattr(logger, level)(message, *args)

    def _remember_ic_quote(
        self,
        symbol: str,
        quote: dict,
        bid: float,
        ask: float,
        ltp: float,
    ) -> None:
        if not self._is_valid_quote_prices(bid, ask, ltp):
            return

        self._ic_quote_cache[symbol] = {
            "quote": quote,
            "bid": float(bid or 0.0),
            "ask": float(ask or 0.0),
            "ltp": float(ltp or 0.0),
            "timestamp": datetime.now(timezone.utc),
        }

    def _get_cached_ic_quote(self, symbol: str) -> tuple[dict, float, float, float, float] | None:
        cached = self._ic_quote_cache.get(symbol)
        if not cached:
            return None

        timestamp = cached.get("timestamp")
        if not isinstance(timestamp, datetime):
            return None

        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        ttl = self._ic_quote_cache_ttl_seconds()
        if age > ttl:
            self._log_throttled(
                f"ic_cached_quote_stale:{symbol}",
                30,
                "warning",
                "IC cached quote stale symbol=%s age=%.2fs ttl=%ss — not reusing",
                symbol,
                age,
                ttl,
            )
            return None

        return (
            cached.get("quote") or {},
            float(cached.get("bid") or 0.0),
            float(cached.get("ask") or 0.0),
            float(cached.get("ltp") or 0.0),
            float(age),
        )

    def _paper_safe_order_id(self, prefix: str, symbol: str) -> str:
        # Issue #39: include monotonic seq so two orders in the same second get distinct IDs
        self._paper_order_seq += 1
        timestamp = int(datetime.now(timezone.utc).timestamp())
        return f"PAPER-{prefix}-{symbol}-{timestamp}-{self._paper_order_seq}"

    async def _paper_quote_price(self, symbol: str, fallback: float = 0.0) -> float:
        if not self._broker_available():
            return fallback

        try:
            quote = await self._fetch_nfo_quote(symbol)
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

    async def _resolve_option_symbol(
        self,
        strike: int,
        option_type: str,
        expiry: str | None = None,
    ) -> str | None:
        if not self._broker_available():
            logger.error("Cannot resolve IC option symbol: broker unavailable")
            return None

        # Include today's date in the cache key so yesterday's expiry symbol is
        # never reused for a different expiry after midnight or a day restart.
        today_key = datetime.now(IST).date().isoformat()
        key = f"{strike}_{option_type}_{expiry or 'default'}_{today_key}"
        if key in self._symbol_cache:
            return self._symbol_cache[key]

        if expiry is None:
            if self.iron_condor_strategy is not None:
                expiry = self.iron_condor_strategy.resolve_entry_expiry(datetime.now(IST)).isoformat()
            else:
                expiry = OptionSelector.get_expiry_api()

        logger.info("IC SYMBOL LOOKUP: strike=%s type=%s expiry=%s", strike, option_type, expiry)

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
                logger.error("IC symbol full-chain also failed: %s", chain.get("validationErrors"))
                return None

            rows = self._extract_chain_rows(chain)

        if not rows:
            samco_expiry = self._format_samco_expiry(expiry)
            if samco_expiry and samco_expiry != expiry:
                logger.info(
                    "Retrying IC option chain with SAMCO expiry format: %s -> %s",
                    expiry,
                    samco_expiry,
                )
                chain = await self.broker.get_option_chain(
                    search_symbol_name=settings.nifty_symbol,
                    exchange="NFO",
                    expiry_date=samco_expiry,
                    strike_price=str(strike),
                    option_type=option_type,
                )
                rows = self._extract_chain_rows(chain) if chain else []

                if not rows:
                    chain = await self.broker.get_option_chain(
                        search_symbol_name=settings.nifty_symbol,
                        exchange="NFO",
                        expiry_date=samco_expiry,
                        strike_price="0",
                        option_type=option_type,
                    )
                    rows = self._extract_chain_rows(chain)

            if not rows:
                response_keys = list(chain.keys()) if isinstance(chain, dict) else []
                validation_errors = chain.get("validationErrors") if isinstance(chain, dict) else None
                response_status = chain.get("status") if isinstance(chain, dict) else None
                status_message = chain.get("statusMessage") if isinstance(chain, dict) else None
                logger.error(
                    "IC option chain empty: strike=%s type=%s expiry=%s response_keys=%s status=%s status_message=%s validation_errors=%s",
                    strike,
                    option_type,
                    expiry,
                    response_keys,
                    response_status,
                    status_message,
                    validation_errors,
                )

                synthetic_symbol = self._build_samco_option_symbol(strike, option_type, expiry)
                if synthetic_symbol:
                    logger.warning(
                        "Using synthetic IC option symbol after empty option chain: strike=%s type=%s expiry=%s symbol=%s",
                        strike,
                        option_type,
                        expiry,
                        synthetic_symbol,
                    )
                    self._symbol_cache[key] = synthetic_symbol
                    return synthetic_symbol

                return None

        best_symbol = None
        best_diff = float("inf")

        for row in rows:
            try:
                diff = abs(float(row.get("strikePrice", 0)) - strike)
                if diff < best_diff:
                    best_diff = diff
                    best_symbol = row.get("tradingSymbol")
            except (TypeError, ValueError):
                continue

        if best_symbol:
            self._symbol_cache[key] = best_symbol
            logger.info("IC SYMBOL RESOLVED: %s (snap_diff=%s) cache_key=%s", best_symbol, best_diff, key)
        else:
            logger.error("No valid IC symbol near strike=%s type=%s", strike, option_type)

        return best_symbol

    @staticmethod
    def _format_samco_expiry(expiry: str | None) -> str | None:
        if not expiry:
            return None
        try:
            return datetime.fromisoformat(str(expiry)).date().strftime("%d-%b-%Y").upper()
        except ValueError:
            return None

    @staticmethod
    def _build_samco_option_symbol(strike: int, option_type: str, expiry: str | None) -> str | None:
        if not expiry:
            return None
        try:
            expiry_tag = datetime.fromisoformat(str(expiry)).date().strftime("%d%b%y").upper()
        except ValueError:
            try:
                expiry_tag = datetime.strptime(str(expiry).upper(), "%d-%b-%Y").date().strftime("%d%b%y").upper()
            except ValueError:
                return None

        root = str(settings.nifty_symbol or "NIFTY").upper().replace(" ", "")
        if root.startswith("NIFTY"):
            root = "NIFTY"
        return f"{root}{expiry_tag}{int(strike)}{str(option_type).upper()}"

    async def _get_leg_quote_snapshot(self, symbol: str) -> tuple[dict, float, float, float]:
        if not self._broker_available():
            raise RuntimeError("Broker unavailable for quote snapshot")

        quote = await self._fetch_nfo_quote(symbol)
        ltp = float(self.broker.parse_ltp(quote) or 0.0)
        bid, ask = self.broker.parse_bid_ask(quote)
        bid = float(bid or 0.0)
        ask = float(ask or 0.0)

        if self._is_valid_quote_prices(bid, ask, ltp):
            self._remember_ic_quote(symbol, quote, bid, ask, ltp)

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
        trade: dict[str, Any],
    ) -> tuple[float, list[dict[str, Any]], str]:
        if not self._broker_available():
            raise RuntimeError("Broker unavailable for IC live snapshot")

        legs = trade.get("legs") or []
        if not legs:
            raise RuntimeError("Active IC trade has no legs")

        current_premium = 0.0
        current_legs: list[dict[str, Any]] = []
        used_cached_quote = False

        for leg in legs:
            symbol = leg.get("symbol")
            side = str(leg.get("side", "")).upper()

            if not symbol or side not in {"BUY", "SELL"}:
                raise RuntimeError(f"Invalid IC leg: {leg}")

            quote, bid, ask, ltp = await self._get_leg_quote_snapshot(symbol)
            price_source = "broker_quote_snapshot"
            quote_age_sec = 0.0

            if not self._is_valid_quote_prices(bid, ask, ltp):
                cached = self._get_cached_ic_quote(symbol)
                if cached is None:
                    raise RuntimeError(
                        f"Invalid IC close quote for {symbol}: bid={bid} ask={ask} ltp={ltp}"
                    )

                quote, bid, ask, ltp, quote_age_sec = cached
                price_source = "broker_quote_snapshot_cached"
                used_cached_quote = True
                self._log_throttled(
                    f"ic_zero_quote_cache_reused:{symbol}",
                    30,
                    "warning",
                    "IC zero quote reused fresh last-good cache symbol=%s age=%.2fs bid=%.2f ask=%.2f ltp=%.2f",
                    symbol,
                    quote_age_sec,
                    bid,
                    ask,
                    ltp,
                )

            close_price = self._close_price_for_leg(side, bid, ask, ltp)
            if close_price <= 0:
                raise RuntimeError(
                    f"Invalid IC close price for {symbol}: bid={bid} ask={ask} ltp={ltp}"
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
                    "option_type": leg.get("option_type"),
                    "strike": leg.get("strike"),
                    "qty": leg.get("qty"),
                    "filled_qty": leg.get("filled_qty") or leg.get("qty"),
                    "entry_price": float(leg.get("entry_price") or leg.get("fill_price") or 0.0),
                    "entry_bid": float(leg.get("entry_bid") or 0.0),
                    "entry_ask": float(leg.get("entry_ask") or 0.0),
                    "entry_ltp": float(leg.get("entry_ltp") or 0.0),
                    "current_bid": float(bid or 0.0),
                    "current_ask": float(ask or 0.0),
                    "current_ltp": float(ltp or 0.0),
                    "current_close_price": float(close_price or 0.0),
                    "exit_price": float(close_price or 0.0),
                    "current_quote": quote,
                    "price_source": price_source,
                    "quote_age_sec": float(quote_age_sec),
                }
            )

        pricing_source = "broker_quote_snapshot_cached" if used_cached_quote else "broker_quote_snapshot"
        return round(current_premium, 2), current_legs, pricing_source

    async def _build_iron_condor_snapshot_legs(
        self,
        strikes: dict[str, int],
        expiry: str,
    ) -> tuple[list[dict[str, Any]], dict[str, float], float] | tuple[None, None, None]:
        if not self._broker_available():
            logger.error("Cannot build IC snapshot legs: broker unavailable")
            return None, None, None

        leg_specs = [
            ("long_put", "BUY", "PE", strikes["long_put"]),
            ("short_put", "SELL", "PE", strikes["short_put"]),
            ("short_call", "SELL", "CE", strikes["short_call"]),
            ("long_call", "BUY", "CE", strikes["long_call"]),
        ]

        legs: list[dict[str, Any]] = []
        premiums: dict[str, float] = {}

        for name, side, option_type, strike in leg_specs:
            symbol = await self._resolve_option_symbol(strike, option_type, expiry)
            if not symbol:
                logger.error("Failed to resolve IC leg symbol for %s strike=%s type=%s expiry=%s", name, strike, option_type, expiry)
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

        net_premium = (
            premiums["short_call"]
            + premiums["short_put"]
            - premiums["long_call"]
            - premiums["long_put"]
        )

        return legs, premiums, net_premium

    async def _place_iron_condor_leg(self, leg: dict[str, Any]) -> dict[str, Any] | None:
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

    async def _rollback_iron_condor(self, legs: list[dict[str, Any]]) -> bool:
        """
        Close every already-filled leg to restore a flat position.

        Returns True only when every leg was successfully reversed.
        On any failure the account may hold an incomplete / naked position —
        callers must treat a False return as a critical event and trigger
        emergency flatten immediately.
        """
        if self._is_paper_mode():
            logger.warning("PAPER MODE: simulated IC rollback for %d legs", len(legs))
            return True

        failed_legs: list[str] = []

        for leg in reversed(legs):
            side = "BUY" if leg["side"] == "SELL" else "SELL"
            qty = int(leg.get("filled_qty") or leg.get("qty") or 0)

            if qty <= 0:
                logger.warning(
                    "Rollback skipped — zero qty leg=%s side=%s",
                    leg.get("symbol"),
                    side,
                )
                continue

            try:
                result = await self.execution_manager.execute_order(
                    {
                        "signal": "IRON_CONDOR_ROLLBACK",
                        "symbol": leg["symbol"],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "quantity": qty,
                        "side": side,
                    }
                )
                if result.is_uncertain or result.state not in {OrderState.FILLED, OrderState.ABORTED}:
                    logger.error(
                        "Rollback leg uncertain/failed: %s %s qty=%d state=%s",
                        side, leg["symbol"], qty, result.state,
                    )
                    failed_legs.append(leg["symbol"])
            except Exception as exc:
                logger.error(
                    "Rollback leg exception: %s %s qty=%d err=%s",
                    side, leg["symbol"], qty, exc,
                )
                failed_legs.append(leg["symbol"])

        if failed_legs:
            logger.critical(
                "IC ROLLBACK PARTIAL — unclosed legs=%s — triggering emergency flatten",
                failed_legs,
            )
            await self.event_bus.publish(
                "ROLLBACK_FAILED",
                {
                    "unclosed_symbols": failed_legs,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            await self.state_manager.update(
                trading_enabled=False,
                last_order_failed=True,
                last_risk_breach="rollback_partial_failure",
                emergency_flatten_unclosed_symbols=failed_legs,
            )
            return False

        return True

    async def _enter_iron_condor_trade(self, payload: dict[str, Any], state) -> None:
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

        # Gap / opening-range filter: skip if spot has moved too far from session open.
        if self._session_open_spot and self._session_open_spot > 0:
            gap_pct = abs(spot - self._session_open_spot) / self._session_open_spot
            skip_gap_threshold = float(getattr(settings, "ic_skip_gap_pct", 0.007))
            if gap_pct > skip_gap_threshold:
                logger.warning(
                    "IC entry blocked by gap filter: spot moved %.3f%% from session open"
                    " (threshold %.3f%%) — likely a trend or gap day",
                    gap_pct * 100,
                    skip_gap_threshold * 100,
                )
                await self.event_bus.publish(
                    "IC_ENTRY_REJECTED",
                    {"reason": "gap_filter", "gap_pct": round(gap_pct, 5)},
                )
                return

        current_time = datetime.now(IST)
        live_iv = getattr(state, "current_iv", None)
        logger.info(
            "IC entry check started spot=%.2f iv=%s time=%s",
            spot,
            f"{live_iv:.4f}" if live_iv else "N/A",
            current_time.isoformat(),
        )

        allowed = self.iron_condor_strategy.can_enter_cycle(current_time, state)
        logger.info("IC can_enter_cycle=%s", allowed)
        if not allowed:
            logger.warning("IC entry blocked by can_enter_cycle()")
            return

        _regime_iv = live_iv or float(getattr(self.iron_condor_strategy, "assumed_iv", 0.15))
        regime_iv_rank = self._compute_session_iv_rank(_regime_iv)
        regime_ok, regime_reason, regime_diag = self.iron_condor_strategy.evaluate_entry_regime(
            spot=spot,
            live_iv=live_iv,
            iv_rank=regime_iv_rank,
        )
        if not regime_ok:
            logger.warning(
                "IC entry rejected by regime filter reason=%s diag=%s",
                regime_reason,
                regime_diag,
            )
            await self.event_bus.publish(
                "IC_ENTRY_REJECTED",
                {
                    "reason": regime_reason,
                    "spot": spot,
                    "live_iv": live_iv,
                    "diagnostics": regime_diag,
                },
            )
            return

        soft_dd_pct = float(getattr(settings, "ic_drawdown_soft_pct", 0.12) or 0.12)
        peak_equity = float(getattr(state, "peak_equity", 0.0) or settings.capital)
        equity_now = float(settings.capital) + float(getattr(state, "daily_pnl", 0.0) or 0.0)
        drawdown_pct = ((peak_equity - equity_now) / peak_equity) if peak_equity > 0 else 0.0
        if drawdown_pct >= soft_dd_pct:
            logger.warning(
                "IC entry blocked by soft drawdown gate dd=%.2f%% threshold=%.2f%%",
                drawdown_pct * 100,
                soft_dd_pct * 100,
            )
            await self.event_bus.publish(
                "IC_ENTRY_REJECTED",
                {
                    "reason": "soft_drawdown_gate",
                    "drawdown_pct": round(drawdown_pct, 4),
                    "threshold_pct": round(soft_dd_pct, 4),
                },
            )
            return

        # Resolve expiry early so DTE is available for delta-based strike selection.
        expiry = self.iron_condor_strategy.resolve_entry_expiry(current_time).isoformat()
        dte_days = self._days_to_expiry(expiry)
        if dte_days is None or dte_days <= 0:
            logger.warning("IC entry: unable to compute DTE for expiry=%s", expiry)
            dte_days = None

        # Use delta-targeting when live IV + DTE are both known (professional grade).
        # Fall back to fixed-distance + IV-adaptive widening when either is missing.
        if live_iv and dte_days:
            strikes = self.iron_condor_strategy.calculate_strikes_by_delta(
                spot, live_iv, float(dte_days)
            )
            logger.info(
                "IC delta-strikes spot=%.2f iv=%.4f dte=%s strikes=%s",
                spot, live_iv, dte_days, strikes,
            )
        else:
            strikes = self.iron_condor_strategy.calculate_strikes(spot, live_iv=live_iv)
            logger.info(
                "IC fixed-distance strikes spot=%.2f iv=%s strikes=%s",
                spot, f"{live_iv:.4f}" if live_iv else "N/A", strikes,
            )

        if not strikes:
            logger.error("Invalid strikes calculated - skipping entry")
            return

        short_distance_used = abs(int(strikes.get("short_call", 0)) - round(spot))
        em_safe, em_diag = self.iron_condor_strategy.is_expected_move_safe(
            spot=spot,
            short_distance=short_distance_used,
            live_iv=live_iv,
        )
        if not em_safe:
            logger.warning(
                "IC entry rejected by expected-move filter diag=%s",
                em_diag,
            )
            await self.event_bus.publish(
                "IC_ENTRY_REJECTED",
                {"reason": "expected_move_exceeds_distance", "diagnostics": em_diag},
            )
            return

        logger.info("IC expected-move filter passed diag=%s", em_diag)

        effective_iv = live_iv or float(getattr(self.iron_condor_strategy, "assumed_iv", 0.15))
        effective_dte = dte_days
        if not effective_dte:
            effective_dte = float(getattr(settings, "ic_days_to_expiry", 7))
            logger.warning(
                "IC score gate: dte_days unavailable, falling back to ic_days_to_expiry=%.0f",
                effective_dte,
            )
        if effective_dte and effective_iv:
            trend_strength = self._compute_trend_strength()
            iv_rank = self._compute_session_iv_rank(effective_iv)
            # Issue #1: _compute_session_iv_rank() returns 0-1 but score_entry()
            # expects 0-100 scale (thresholds like 30.0, 65.0). Multiply here.
            iv_rank_pct = (iv_rank * 100.0) if iv_rank is not None else None
            logger.info(
                "IC score gate inputs: trend_strength=%.3f iv_rank=%s",
                trend_strength,
                f"{iv_rank_pct:.1f}" if iv_rank_pct is not None else "N/A (insufficient history)",
            )
            entry_score = self.iron_condor_strategy.score_entry(
                spot=spot,
                iv=effective_iv,
                dte_days=float(effective_dte),
                iv_rank=iv_rank_pct,
                trend_strength=trend_strength,
            )
            if not entry_score.get("entry_ok", True):
                logger.warning(
                    "IC entry rejected by score gate score=%.1f verdict=%s pop=%.1f%% trend=%.3f iv_rank=%s",
                    entry_score.get("score", 0.0),
                    entry_score.get("verdict", "unknown"),
                    entry_score.get("pop", 0.0),
                    trend_strength,
                    f"{iv_rank_pct:.1f}" if iv_rank_pct is not None else "N/A",
                )
                await self.event_bus.publish(
                    "IC_ENTRY_REJECTED",
                    {
                        "reason": "entry_score_too_low",
                        "score": entry_score.get("score"),
                        "verdict": entry_score.get("verdict"),
                        "pop": entry_score.get("pop"),
                    },
                )
                return
            logger.info(
                "IC entry score passed score=%.1f verdict=%s pop=%.1f%%",
                entry_score.get("score", 0.0),
                entry_score.get("verdict", "ok"),
                entry_score.get("pop", 0.0),
            )

        logger.info("IC entry using expiry=%s", expiry)
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

        # Deduct estimated bid-ask slippage (per leg × 4 legs) from the quoted
        # net premium so viability checks reflect realistic fill prices.
        slippage_per_leg = float(getattr(settings, "ic_slippage_per_leg", 3.0) or 3.0)
        total_slippage = 4 * slippage_per_leg
        if total_slippage >= snapshot_net_premium:
            # Issue #26: slippage ≥ raw premium would produce zero/negative adjusted value
            logger.warning(
                "IC entry aborted: total slippage %.2f >= raw premium %.2f — unrealistic quote",
                total_slippage,
                snapshot_net_premium,
            )
            return
        slippage_adjusted_premium = snapshot_net_premium - total_slippage
        logger.info(
            "IC entry: raw_premium=%.2f slippage_adj=%.2f (slippage/leg=%.1f total=%.1f)",
            snapshot_net_premium,
            slippage_adjusted_premium,
            slippage_per_leg,
            total_slippage,
        )

        abs_min_premium = max(self.iron_condor_strategy.min_premium, 10.0)
        if slippage_adjusted_premium < abs_min_premium:
            logger.warning(
                "Iron Condor premium too low after slippage: %.2f < %.2f (raw=%.2f floor=10.0 config=%.2f)",
                slippage_adjusted_premium,
                abs_min_premium,
                snapshot_net_premium,
                self.iron_condor_strategy.min_premium,
            )
            return

        spread_width = max(
            int(strikes.get("call_width") or 0),
            int(strikes.get("put_width") or 0),
        )
        viable, viability_reason, viability_diag = self.iron_condor_strategy.is_entry_credit_viable(
            entry_premium=slippage_adjusted_premium,
            qty=settings.order_qty,
            spread_width=spread_width or None,
        )
        if not viable:
            logger.warning(
                "IC entry rejected by economics filter reason=%s premium=%.2f qty=%d width=%d diag=%s",
                viability_reason,
                snapshot_net_premium,
                settings.order_qty,
                spread_width,
                viability_diag,
            )
            await self.event_bus.publish(
                "IC_ENTRY_REJECTED",
                {
                    "reason": viability_reason,
                    "net_premium": snapshot_net_premium,
                    "qty": settings.order_qty,
                    "spread_width": spread_width,
                    "diagnostics": viability_diag,
                },
            )
            return

        logger.info(
            "IC economics filter passed reason=%s diag=%s",
            viability_reason,
            viability_diag,
        )

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
        execution_order = [
            next(leg for leg in snapshot_legs if leg["name"] == name)
            for name in execution_order_names
        ]

        placed_legs: list[dict[str, Any]] = []

        for leg in execution_order:
            placed = await self._place_iron_condor_leg(leg)

            if not placed or int(placed.get("filled_qty", 0)) <= 0:
                logger.error("IC leg placement failed: %s", leg["display_symbol"])
                if placed_legs:
                    rollback_ok = await self._rollback_iron_condor(placed_legs)
                    if not rollback_ok:
                        await self._handle_fatal_exception(
                            "IC_ROLLBACK_FAILED",
                            RuntimeError(f"Partial IC rollback after failed leg {leg['display_symbol']}"),
                        )
                return

            placed_legs.append(placed)

        if len(placed_legs) != 4:
            logger.error("Only %d of 4 IC legs filled — rolling back", len(placed_legs))
            if placed_legs:
                rollback_ok = await self._rollback_iron_condor(placed_legs)
                if not rollback_ok:
                    await self._handle_fatal_exception(
                        "IC_ROLLBACK_FAILED",
                        RuntimeError(f"Partial IC rollback — only {len(placed_legs)} of 4 legs filled"),
                    )
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
        logger.info("Spot: %.0f", state.spot_price)
        logger.info("Shorts collected: %.2f", actual_premiums["short_call"] + actual_premiums["short_put"])
        logger.info("Longs paid: %.2f", actual_premiums["long_call"] + actual_premiums["long_put"])
        logger.info("Net credit: %.2f", actual_net_premium)

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
            "entry_price": round(actual_net_premium, 2),
            "entry_ltp": round(actual_net_premium, 2),
            "quote_net_premium": round(snapshot_net_premium, 2),
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "spot_at_entry": float(spot),
            "iv_at_entry": float(live_iv or 0.0),
            "dte_at_entry": int(dte_days or getattr(settings, "ic_days_to_expiry", 7)),
            "iv_rank_at_entry": round(regime_iv_rank * 100.0, 1) if regime_iv_rank is not None else None,  # Issue #13
            "status": "OPEN",
            "size_label": payload.get("size_label", "FULL"),
            "order_ids": order_ids,
            "premiums": actual_premiums,
            "quote_premiums": snapshot_premiums,
            "strikes": strikes,
            "legs": placed_legs,
            "exit_legs": [],
            "current_legs": [
                {
                    "name": leg.get("name"),
                    "symbol": leg.get("symbol"),
                    "display_symbol": leg.get("display_symbol") or leg.get("symbol"),
                    "side": leg.get("side"),
                    "option_type": leg.get("option_type"),
                    "strike": leg.get("strike"),
                    "qty": leg.get("qty"),
                    "filled_qty": leg.get("filled_qty") or leg.get("qty"),
                    "entry_price": float(leg.get("entry_price") or leg.get("fill_price") or 0.0),
                    "entry_bid": float(leg.get("entry_bid") or 0.0),
                    "entry_ask": float(leg.get("entry_ask") or 0.0),
                    "entry_ltp": float(leg.get("entry_ltp") or 0.0),
                    "current_bid": float(leg.get("entry_bid") or 0.0),
                    "current_ask": float(leg.get("entry_ask") or 0.0),
                    "current_ltp": float(leg.get("entry_ltp") or 0.0),
                    "current_close_price": float(leg.get("entry_price") or leg.get("fill_price") or 0.0),
                    "price_source": leg.get("price_source") or "broker_quote_snapshot",
                }
                for leg in placed_legs
            ],
            "pricing_source": "broker_quote_snapshot" if self._is_paper_mode() else "broker_fill",
            "exit_reason": None,
            "exit_premium": None,
            "current_premium": round(actual_net_premium, 2),
        }

        await self.state_manager.update(
            active_trade=trade,
            trade_count=state.trade_count + 1,
            live_pnl=0.0,
        )

        logger.info(
            "IRON_CONDOR position opened with premium=%.2f qty=%d source=%s",
            actual_net_premium,
            trade["qty"],
            trade["pricing_source"],
        )

        await self.state_manager._journal_event(
            "TRADE_ENTRY",
            {
                "symbol": trade.get("symbol"),
                "strategy": "IRON_CONDOR",
                "entry_price": trade.get("entry_price"),
                "qty": trade.get("qty"),
                "expiry": trade.get("expiry"),
                "legs": [
                    {"name": l.get("name"), "strike": l.get("strike"), "side": l.get("side")}
                    for l in (trade.get("legs") or [])
                ],
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )
        await self.event_bus.publish("TRADE_OPENED", {"trade": trade})

    async def _execute_iron_condor_exit_legs(
        self,
        trade: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        exit_legs: list[dict[str, Any]] = []
        current_leg_map = {
            leg.get("name"): leg
            for leg in trade.get("current_legs", [])
            if isinstance(leg, dict) and leg.get("name")
        }

        for leg in trade.get("legs", []):
            if not isinstance(leg, dict):
                logger.warning("IC exit: skipping non-dict leg=%r", leg)
                continue
            exit_side = "BUY" if leg.get("side") == "SELL" else "SELL"
            qty = _to_int(leg.get("filled_qty") or leg.get("qty"), 0)

            result = await self.execution_manager.execute_order(
                {
                    "signal": "IRON_CONDOR_EXIT",
                    "symbol": leg["symbol"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "quantity": qty,
                    "side": exit_side,
                }
            )

            if result.is_uncertain or result.state != OrderState.FILLED:
                logger.error("Iron Condor exit leg failed: %s", leg)
                return None

            snapshot_leg = current_leg_map.get(leg.get("name"), {})
            fallback_exit_price = _to_float(
                snapshot_leg.get("current_close_price")
                or snapshot_leg.get("current_price")
                or snapshot_leg.get("exit_price")
                or leg.get("entry_price"),
                0.0,
            )
            # _to_float(0.0, fallback) returns 0.0 — use explicit guard so a
            # zero avg_price (broker quote failure / paper mode) triggers fallback.
            actual_exit_price = (
                result.avg_price
                if result.avg_price is not None and result.avg_price > 0
                else fallback_exit_price
            )

            exit_legs.append(
                {
                    **leg,
                    **snapshot_leg,
                    "side": leg.get("side"),
                    "exit_side": exit_side,
                    "exit_order_id": result.order_id,
                    "exit_price": round(actual_exit_price, 2),
                    "current_close_price": round(actual_exit_price, 2),
                    "filled_qty": qty,
                    "exit_time": datetime.now(timezone.utc).isoformat(),
                    "price_source": snapshot_leg.get("price_source")
                    or leg.get("price_source")
                    or trade.get("current_pricing_source")
                    or trade.get("pricing_source")
                    or "broker_quote_snapshot",
                }
            )

        return exit_legs

    def _calculate_iron_condor_close_premium(
        self,
        exit_legs: list[dict[str, Any]],
    ) -> float:
        close_premium = 0.0

        for leg in exit_legs:
            side = str(leg.get("side", "")).upper()
            exit_price = _to_float(
                leg.get("exit_price")
                or leg.get("current_close_price")
                or leg.get("current_price"),
                0.0,
            )

            if side == "SELL":
                close_premium += exit_price
            elif side == "BUY":
                close_premium -= exit_price

        return round(close_premium, 2)

    async def _check_and_execute_leg_roll(
        self,
        trade: dict[str, Any],
        current_time: datetime,
    ) -> bool:
        """Check if any short leg should be rolled and execute the roll if so.

        Returns True if a roll was executed (caller should skip normal exit logic
        for this tick and re-evaluate next cycle with updated strikes).
        """
        legs = trade.get("legs") or []
        spot = trade.get("current_spot") or trade.get("spot_at_entry") or 0.0
        spot = float(spot)
        if spot == 0.0:
            return False

        # Only check short legs (the ones that can be threatened).
        short_legs = [l for l in legs if str(l.get("side", "")).upper() == "SELL"]
        if not short_legs:
            return False

        iv = float(trade.get("iv_at_entry") or self.iron_condor_strategy.assumed_iv)
        dte_days = int(trade.get("dte_at_entry") or self.iron_condor_strategy.days_to_expiry_value)
        original_credit = float(trade.get("entry_price") or 0.0)

        # Per-trade roll tracker — max 1 roll per leg per calendar day.
        rolls_today: dict[str, str] = trade.get("rolls_today") or {}
        today_str = current_time.date().isoformat()

        rolled_any = False
        for leg in short_legs:
            leg_name = leg.get("name") or leg.get("symbol") or ""
            opt_type = str(leg.get("option_type") or leg.get("opt_type") or "CE").upper()
            strike = float(leg.get("strike") or leg.get("entry_price") or 0.0)

            # Skip if this leg was already rolled today.
            if rolls_today.get(leg_name) == today_str:
                continue

            roll_signal = self.iron_condor_strategy.should_roll_leg(
                spot=spot,
                threatened_strike=strike,
                opt_type=opt_type,
                iv=iv,
                dte_days=dte_days,
                original_credit=original_credit,
            )
            if not roll_signal.get("should_roll"):
                continue

            new_strike = roll_signal.get("new_strike")
            if not new_strike:
                continue

            expiry_raw = str(trade.get("expiry") or "")
            old_symbol = str(leg.get("symbol") or leg.get("name") or "")

            # Resolve new symbol via broker chain lookup — never construct manually.
            new_symbol = await self._resolve_option_symbol(int(new_strike), opt_type, expiry_raw)
            if not new_symbol:
                logger.error(
                    "IC leg roll: could not resolve symbol for new_strike=%s type=%s expiry=%s — skipping",
                    new_strike, opt_type, expiry_raw,
                )
                continue

            logger.info(
                "IC leg roll: %s strike=%.0f -> %s new_strike=%.0f roll_cost=%.2f",
                old_symbol, strike, new_symbol, new_strike,
                roll_signal.get("roll_cost", 0.0),
            )

            # Step 1: BUY back the threatened short via execution_manager (handles retries + paper mode).
            buy_exec = await self.execution_manager.execute_order({
                "signal": "IRON_CONDOR_ROLL",
                "symbol": old_symbol,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "quantity": int(trade.get("qty") or 1),
                "side": "BUY",
            })
            if buy_exec.is_uncertain or buy_exec.state != OrderState.FILLED:
                logger.error(
                    "IC leg roll BUY-back failed for %s state=%s — aborting roll",
                    old_symbol, buy_exec.state,
                )
                continue

            buy_price = float(buy_exec.avg_price or roll_signal.get("current_price", 0.0))

            # Step 2: SELL the new further-OTM short.
            sell_exec = await self.execution_manager.execute_order({
                "signal": "IRON_CONDOR_ROLL",
                "symbol": new_symbol,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "quantity": int(trade.get("qty") or 1),
                "side": "SELL",
            })
            if sell_exec.is_uncertain or sell_exec.state != OrderState.FILLED:
                # Emergency rollback: re-sell the old strike to restore the hedge.
                logger.error(
                    "IC leg roll SELL failed for %s state=%s — rolling back: re-selling %s",
                    new_symbol, sell_exec.state, old_symbol,
                )
                await self.execution_manager.execute_order({
                    "signal": "IRON_CONDOR_ROLL_ROLLBACK",
                    "symbol": old_symbol,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "quantity": int(trade.get("qty") or 1),
                    "side": "SELL",
                })
                continue

            sell_price = float(sell_exec.avg_price or 0.0)

            # Step 3: Update the leg in the trade dict.
            leg["symbol"] = new_symbol
            leg["name"] = new_symbol
            leg["strike"] = new_strike
            leg["entry_price"] = sell_price  # new credit received for this leg
            leg["rolled_from"] = old_symbol
            leg["rolled_buy_price"] = buy_price

            # Adjust overall entry_price by the net roll debit/credit.
            roll_net = sell_price - buy_price  # positive = credit, negative = debit
            old_entry = float(trade.get("entry_price") or 0.0)
            trade["entry_price"] = round(old_entry + roll_net, 2)

            # Record roll for today so we don't roll the same leg again.
            rolls_today[leg_name] = today_str
            rolled_any = True

            logger.info(
                "IC leg roll complete: %s→%s buy_back=%.2f new_sell=%.2f net=%.2f new_entry_price=%.2f",
                old_symbol,
                new_symbol,
                buy_price,
                sell_price,
                roll_net,
                trade["entry_price"],
            )

        if rolled_any:
            trade["rolls_today"] = rolls_today
            trade["legs"] = legs
            await self.state_manager.update(active_trade=trade)

        return rolled_any

    async def _exit_iron_condor_trade(
        self,
        trade: dict[str, Any],
        reason: str,
        current_premium: float,
    ) -> dict[str, Any] | None:
        if trade.get("status") == "CLOSED":
            return dict(trade)
        if trade.get("exit_in_progress"):
            logger.warning("IRON_CONDOR exit ignored; exit already in progress reason=%s", reason)
            return None

        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade:
                logger.warning("IRON_CONDOR exit skipped: no active trade")
                return None

            active_trade = state.active_trade
            # active_trade (re-read under lock) is the authoritative fresh state.
            # trade (the argument) may be a stale snapshot from before the lock was
            # acquired.  Merge so that fresh DB values win over the caller's snapshot.
            latest_trade = {**trade, **active_trade}
            if latest_trade.get("status") == "CLOSED":
                return dict(latest_trade)
            if latest_trade.get("exit_in_progress") or latest_trade.get("status") == "CLOSING":
                logger.warning("IRON_CONDOR exit ignored inside lock; exit already in progress reason=%s", reason)
                return None
            # Mark CLOSING immediately and persist — prevents any concurrent caller
            # that reads fresh state from also entering this path.
            latest_trade["exit_in_progress"] = True
            latest_trade["status"] = "CLOSING"
            await self.state_manager.update(active_trade=latest_trade)

            logger.info(
                "IRON_CONDOR exit triggered reason=%s quote_premium=%.2f",
                reason,
                current_premium,
            )

            exit_legs = await self._execute_iron_condor_exit_legs(latest_trade)
            if exit_legs is None:
                await self._handle_fatal_exception(
                    "IRON_CONDOR_EXIT_FAILED",
                    RuntimeError("Iron Condor exit failed"),
                )
                return None

            if not self.iron_condor_strategy:
                logger.error("IRON_CONDOR exit failed: strategy helper unavailable")
                return None

            actual_exit_premium = self._calculate_iron_condor_close_premium(exit_legs)
            pnl = self.iron_condor_strategy.compute_pnl(
                float(latest_trade["entry_price"]),
                float(actual_exit_premium),
                int(latest_trade["qty"]),
                entry_legs=latest_trade.get("legs") or [],
                exit_legs=exit_legs,
            )

            gross_pnl = round(float(pnl.get("gross_pnl", 0.0)), 2)
            total_charges = round(float(pnl.get("total_charges", 0.0)), 2)
            net_pnl = round(float(pnl.get("net_pnl", gross_pnl - total_charges)), 2)

            charge_breakdown = {
                "brokerage": round(float(pnl.get("brokerage", 0.0)), 2),           # Issue #22: was incorrectly reading platform_charges
                "platform_charges": round(float(pnl.get("platform_charges", 0.0)), 2),
                "stt": round(float(pnl.get("stt", 0.0)), 2),
                "exchange_txn": round(float(pnl.get("exchange_txn", 0.0)), 2),
                "sebi": round(float(pnl.get("sebi", 0.0)), 2),
                "gst": round(float(pnl.get("gst", 0.0)), 2),
                "stamp_duty": round(float(pnl.get("stamp_duty", 0.0)), 2),
                "total_charges": total_charges,
            }

            new_daily = round(float(state.daily_pnl or 0.0) + net_pnl, 2)
            # Issue #6: update high-water mark so soft/hard drawdown gates track real peak
            _current_peak = float(getattr(state, "peak_equity", 0.0) or settings.capital)
            _equity_now = settings.capital + new_daily
            new_peak_equity = max(_current_peak, _equity_now)
            now_utc = datetime.now(timezone.utc).isoformat()
            today_ist = datetime.now(IST).date().isoformat()
            pricing_source = (
                latest_trade.get("current_pricing_source")
                or latest_trade.get("pricing_source")
                or "broker_quote_snapshot"
            )

            closed = {
                **latest_trade,
                "strategy": "IRON_CONDOR",
                "signal": "IRON_CONDOR",
                "symbol": latest_trade.get("symbol") or "NIFTY",
                "underlying": latest_trade.get("underlying") or settings.nifty_symbol,
                "status": "CLOSED",
                "exit_time": now_utc,
                "exit_reason": reason,
                "reason": reason,
                "consecutive_losses_before_exit": int(getattr(state, "consecutive_losses", 0)),  # Issue #30
                "exit_price": round(float(actual_exit_premium), 2),
                "exit_premium": round(float(actual_exit_premium), 2),
                "current_premium": round(float(actual_exit_premium), 2),
                "quote_exit_premium": round(float(current_premium), 2),
                "gross_pnl": gross_pnl,
                "pnl": net_pnl,
                "net_pnl": net_pnl,
                "charges": charge_breakdown,
                "total_charges": total_charges,
                "brokerage": charge_breakdown["brokerage"],
                "stt": charge_breakdown["stt"],
                "exchange_fee": charge_breakdown["exchange_txn"],
                "gst": charge_breakdown["gst"],
                "stamp_duty": charge_breakdown["stamp_duty"],
                "trade_type": "IRON_CONDOR",
                "pricing_source": pricing_source,
                "current_pricing_source": pricing_source,
                "legs": latest_trade.get("legs") or [],
                "current_legs": exit_legs,
                "exit_legs": exit_legs,
            }

            self.trade_store.append_trade(closed, new_daily)

            await self.state_manager._journal_event(
                "TRADE_EXIT",
                {
                    "symbol": closed.get("symbol"),
                    "strategy": "IRON_CONDOR",
                    "exit_reason": reason,
                    "entry_price": closed.get("entry_price"),
                    "exit_price": closed.get("exit_price"),
                    "gross_pnl": gross_pnl,
                    "net_pnl": net_pnl,
                    "total_charges": total_charges,
                    "new_daily_pnl": new_daily,
                    "ts": datetime.now(timezone.utc).isoformat(),
                },
            )

            await self.state_manager.update(
                active_trade=None,
                daily_pnl=new_daily,
                live_pnl=0.0,
                peak_equity=new_peak_equity,          # Issue #6: maintain high-water mark
                consecutive_losses=self._next_consecutive_losses(
                    state.consecutive_losses,
                    net_pnl,
                ),
                last_iron_condor_date=today_ist,
                last_iron_condor_month=datetime.now(IST).month,
                last_trade_date=today_ist,
                last_ic_trade_date=today_ist,
                iron_condor_trade_date=today_ist,
            )

            await self._enforce_global_risk_stop(new_daily, net_pnl, state)

            logger.info(
                "IRON_CONDOR CLOSED reason=%s gross_pnl=%.2f charges=%.2f net_pnl=%.2f legs=%d exit_legs=%d",
                reason,
                gross_pnl,
                total_charges,
                net_pnl,
                len(closed.get("legs") or []),
                len(closed.get("exit_legs") or []),
            )

            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})
            return closed

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

            if self._no_entry_after_time and datetime.now(IST).time() >= self._no_entry_after_time:
                logger.info("Past no-entry time — skipping")
                return

            raw_signal = payload.get("signal")
            size_label = payload.get("size_label", "FULL")

            logger.info(
                "SIGNAL RECEIVED raw=%s size=%s spot=%.2f",
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

                await self._enter_directional_trade(payload, state, signal, size_label)

            except Exception as exc:
                logger.error("ENTRY FAILED: %s", exc, exc_info=True)
                await self._handle_fatal_exception("entry", exc)

    async def _enter_directional_trade(
        self,
        payload: dict[str, Any],
        state: Any,
        signal: str,
        size_label: str,
    ) -> None:
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
        logger.info("STRIKE CALCULATED spot=%.2f signal=%s -> strike=%d", state.spot_price, signal, strike)

        symbol = await self._resolve_symbol(strike, signal)
        if not symbol:
            logger.error("SYMBOL RESOLUTION FAILED strike=%d signal=%s", strike, signal)
            return

        logger.info("SYMBOL RESOLVED: %s", symbol)

        quote = await self._fetch_nfo_quote(symbol)
        ltp = self.broker.parse_ltp(quote)
        bid, ask = self.broker.parse_bid_ask(quote)

        if not ltp:
            logger.warning("LTP UNAVAILABLE: %s", symbol)
            return

        logger.info("LTP FETCHED %s = %.2f", symbol, ltp)

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

        if not self._validate_trade_setup(state, payload.get("signal"), ltp, bid, ask):
            logger.warning("TRADE VALIDATION FAILED symbol=%s signal=%s", symbol, payload.get("signal"))
            return

        if ltp < settings.min_entry_premium:
            logger.warning(
                "PREMIUM TOO LOW %.1f < min %.1f — skip %s",
                ltp,
                settings.min_entry_premium,
                symbol,
            )
            return

        if settings.min_option_volume > 0:
            volume = _parse_volume(quote)
            if volume < settings.min_option_volume:
                logger.warning("LOW VOLUME %d < %d — skip %s", volume, settings.min_option_volume, symbol)
                return

            logger.info("VOLUME OK %s = %d", symbol, volume)

        requested_qty = self._get_qty(size_label)
        logger.info("QTY CALCULATED size=%s -> qty=%d", size_label, requested_qty)

        logger.info("PLACING BUY ORDER: %s qty=%d", symbol, requested_qty)
        exec_result = await self.execution_manager.execute_order(
            {
                "signal": payload.get("signal"),
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
                logger.warning("PAPER MODE: using LTP as fill price for order=%s qty=%d", order_id, filled_qty)
                entry_price = conservative_ltp
            else:
                logger.error("LIVE MODE: missing fill price despite FILLED state order=%s", order_id)
                return
        else:
            entry_price = fill_price

        logger.info(
            "BUY FILL CONFIRMED order=%s fill=%.2f ltp=%.2f mode=%s",
            order_id,
            entry_price,
            ltp,
            settings.mode.upper(),
        )

        t1_qty = filled_qty // 2 if filled_qty >= 2 else 0
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
                "PAPER MODE: simulated stop-loss order symbol=%s qty=%d trigger=%.2f",
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
                logger.critical("SL placement failed after entry fill; forcing exit and halting. resp=%s", sl_resp)
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
            logger.info("PAPER MODE: skipping broker position validation symbol=%s qty=%d", symbol, filled_qty)

        logger.info(
            "TRADE OPENED %s qty=%d entry=%.2f SL=%.2f T1=%.2f T2=%.2f%s",
            symbol,
            filled_qty,
            entry_price,
            trade["sl_price"],
            trade["t1_price"],
            trade["t2_price"],
            " [PARTIAL FILL]" if trade["partial_fill"] else "",
        )

        if self.strategy:
            self.strategy.set_already_traded_today()
            logger.info("Daily trade limit locked (after successful entry)")

        await self.event_bus.publish("TRADE_OPENED", {"trade": trade})

    async def _track_quote_degradation(
        self,
        *,
        is_degraded: bool,
        streak_attr: str,
        level_attr: str,
        event_name: str,
        context: dict[str, Any],
    ) -> None:
        if not is_degraded:
            if getattr(self, level_attr) > 0:
                streak = getattr(self, streak_attr)
                logger.info("Quote source recovered: %s (streak=%d)", event_name, streak)
                await self.event_bus.publish(
                    f"{event_name}_RECOVERED",
                    {**context, "streak_at_recovery": streak},
                )
            setattr(self, streak_attr, 0)
            setattr(self, level_attr, 0)
            return

        streak = getattr(self, streak_attr) + 1
        setattr(self, streak_attr, streak)
        level = getattr(self, level_attr)

        if streak >= _QUOTE_DEGRADED_CRITICAL_TICKS and level < 2:
            logger.critical(
                "Quote source DEGRADED CRITICAL: %s ticks=%d", event_name, streak
            )
            await self.event_bus.publish(
                f"{event_name}_CRITICAL",
                {**context, "streak_ticks": streak},
            )
            setattr(self, level_attr, 2)
        elif streak >= _QUOTE_DEGRADED_WARN_TICKS and level < 1:
            logger.warning(
                "Quote source degraded: %s ticks=%d", event_name, streak
            )
            await self.event_bus.publish(
                event_name,
                {**context, "streak_ticks": streak},
            )
            setattr(self, level_attr, 1)

    async def _escalate_iron_condor_quote_degradation(
        self,
        trade: dict[str, Any],
        pricing_source: str,
        current_premium: float,
    ) -> None:
        streak = int(getattr(self, "_ic_fallback_streak", 0) or 0)
        degraded_trade = {
            **trade,
            "quote_degraded": True,
            "quote_degraded_streak": streak,
            "display_pnl_is_estimated": pricing_source == "model_fallback",
            "requires_manual_review": pricing_source == "model_fallback",
            "degraded_pricing_source": pricing_source,
        }

        updates: dict[str, Any] = {
            "active_trade": degraded_trade,
            "last_order_failed": pricing_source == "model_fallback",
        }

        if pricing_source == "model_fallback" and streak >= _QUOTE_DEGRADED_CRITICAL_TICKS:
            updates.update(
                trading_enabled=False,
                circuit_breaker_open=True,
                last_risk_breach="ic_quote_degradation_critical",
            )
            logger.critical(
                "IC quote degradation reached critical threshold; trading disabled streak=%d premium=%.2f",
                streak,
                current_premium,
            )
            await self.event_bus.publish(
                "IC_QUOTE_DEGRADATION_LOCKDOWN",
                {
                    "streak_ticks": streak,
                    "pricing_source": pricing_source,
                    "current_premium": float(current_premium),
                    "symbol": trade.get("symbol"),
                },
            )

        await self.state_manager.update(**updates)

    async def _monitor_loop(self):
        while True:
            await asyncio.sleep(2)

            state = await self.state_manager.snapshot()
            trade = state.active_trade

            if not trade or trade.get("status") == "CLOSED":
                continue

            if trade.get("strategy") == "IRON_CONDOR":
                await self._monitor_iron_condor_trade(trade, spot=float(state.spot_price or 0.0))
                continue

            await self._monitor_directional_trade(trade)

    async def _monitor_iron_condor_trade(self, trade: dict[str, Any], spot: float = 0.0) -> None:
        if not self.iron_condor_strategy:
            return

        current_time = datetime.now(IST)
        try:
            entry_time = datetime.fromisoformat(str(trade.get("entry_time") or ""))
        except (TypeError, ValueError) as exc:
            logger.error(
                "IC monitor: invalid entry_time=%r — using current_time as fallback (%s)",
                trade.get("entry_time"),
                exc,
            )
            entry_time = current_time

        try:
            current_premium, current_legs, pricing_source = await self._get_live_iron_condor_close_snapshot(trade)
        except Exception as exc:
            self._log_throttled(
                "ic_live_snapshot_model_fallback",
                30,
                "warning",
                "IC live snapshot failed; using model fallback: %s",
                exc,
            )
            _ep = _to_float(trade.get("entry_price"), 0.0)
            current_premium = self.iron_condor_strategy.estimate_current_premium(
                _ep,
                entry_time,
                current_time,
            )
            current_legs = trade.get("current_legs") or []
            pricing_source = "model_fallback"

        pnl_snapshot = self.iron_condor_strategy.compute_pnl(
            float(_to_float(trade.get("entry_price"), 0.0)),
            float(current_premium),
            int(trade.get("qty", 0) or 0),
        )
        live_pnl = round(float(pnl_snapshot.get("net_pnl", 0.0)), 2)

        updated_trade = {
            **trade,
            "current_premium": round(float(current_premium), 2),
            "current_legs": current_legs,
            "current_pricing_source": pricing_source,
            "current_spot": spot or float(trade.get("spot_at_entry") or 0.0),
        }

        # C1: guard against overwriting CLOSING/CLOSED status set by concurrent exit
        if updated_trade.get("exit_in_progress") or updated_trade.get("status") in ("CLOSED", "CLOSING"):
            return
        await self.state_manager.update(active_trade=updated_trade, live_pnl=live_pnl)

        # Compute live DTE from the trade's stored expiry so get_exit_reason can
        # trigger the 1-DTE gamma-risk force exit (Gap 4 fix).
        _trade_expiry = str(updated_trade.get("expiry") or "")
        current_dte: float | None = None
        if _trade_expiry:
            _raw_dte = self._days_to_expiry(_trade_expiry)
            if _raw_dte is not None:
                current_dte = float(_raw_dte)

        try:
            should_force_exit, reason = self.expiry_safety.should_force_exit(
                current_time,
                entry_time,
                trade_expiry=_trade_expiry,
            )
        except TypeError:
            should_force_exit, reason = self.expiry_safety.should_force_exit(current_time, entry_time)
        if should_force_exit:
            logger.critical("FORCE CLOSING: %s", reason)
            await self._exit_iron_condor_trade(
                updated_trade,
                reason,
                current_premium,
            )
            return

        await self._track_quote_degradation(
            is_degraded=(pricing_source != "broker_quote_snapshot"),
            streak_attr="_ic_fallback_streak",
            level_attr="_ic_fallback_alert_level",
            event_name="IC_QUOTE_DEGRADED",
            context={
                "pricing_source": pricing_source,
                "entry_price": float(updated_trade["entry_price"]),
                "current_premium": float(current_premium),
            },
        )
        if pricing_source != "broker_quote_snapshot":
            await self._escalate_iron_condor_quote_degradation(
                updated_trade,
                pricing_source,
                current_premium,
            )

        allowed_exit_sources = {
            "broker_quote_snapshot",
            "broker_quote_snapshot_cached",
        }

        if pricing_source not in allowed_exit_sources:
            # Time-based EOD exits must fire even when broker quotes are unavailable.
            # Check for those before blocking on pricing source.
            _time_reason = self.iron_condor_strategy.get_exit_reason(
                entry_time,
                current_time,
                updated_trade["entry_price"],
                current_premium,
                int(updated_trade.get("qty", 0) or 0),
                dte_days=current_dte,
            )
            _eod_reasons = {"EOD", "EOD_PROFIT_LOCK", "EOD_NO_POSITIVE_TARGET", "EOD_LOSS_CUT"}
            if _time_reason in _eod_reasons:
                logger.warning(
                    "IC time-based exit %s firing despite degraded pricing_source=%s current_premium=%.2f",
                    _time_reason,
                    pricing_source,
                    current_premium,
                )
                await self._exit_iron_condor_trade(updated_trade, _time_reason, current_premium)
                return
            self._log_throttled(
                f"ic_auto_exit_blocked:{pricing_source}",
                30,
                "warning",
                "IC auto-exit blocked because pricing_source=%s current_premium=%.2f",
                pricing_source,
                current_premium,
            )
            return

        # Ratchet stop: once 50% profit was locked in, exit if premium retraces to breakeven.
        if updated_trade.get("ratchet_breakeven_active"):
            if current_premium >= float(updated_trade["entry_price"]):
                logger.warning(
                    "IC ratchet stop triggered: premium=%.2f retraced to entry=%.2f",
                    current_premium,
                    float(updated_trade["entry_price"]),
                )
                await self._exit_iron_condor_trade(updated_trade, "RATCHET_STOP", current_premium)
                return

        reason = self.iron_condor_strategy.get_exit_reason(
            entry_time,
            current_time,
            updated_trade["entry_price"],
            current_premium,
            int(updated_trade.get("qty", 0) or 0),
            dte_days=current_dte,
        )

        if not reason:
            exit_dt = entry_time.replace(
                hour=self.iron_condor_strategy.exit_time.hour,
                minute=self.iron_condor_strategy.exit_time.minute,
                second=0,
                microsecond=0,
            )
            session_secs = max(1.0, (exit_dt - entry_time).total_seconds())
            elapsed_pct = max(0.0, min(1.0, (current_time - entry_time).total_seconds() / session_secs))
            partial = self.iron_condor_strategy.get_partial_exit_signal(
                entry_premium=float(updated_trade["entry_price"]),
                current_premium=float(current_premium),
                qty=int(updated_trade.get("qty", 0) or 0),
                elapsed_pct=elapsed_pct,
            )
            action = partial.get("action", "hold")
            if action in ("scale_exit_75pct", "scale_exit_eod_lock"):
                reason = action
            elif action == "scale_exit_50pct":
                if not updated_trade.get("ratchet_breakeven_active"):
                    updated_trade["ratchet_breakeven_active"] = True
                    await self.state_manager.update(active_trade=updated_trade)
                    logger.info(
                        "IC ratchet stop activated at 50%% profit=%.1f%% — stop moved to breakeven entry=%.2f",
                        partial.get("profit_pct", 0.0),
                        float(updated_trade["entry_price"]),
                    )
            elif action == "scale_exit_25pct":
                logger.info(
                    "IC partial 25%% signal profit=%.1f%% elapsed=%.0f%% — holding",
                    partial.get("profit_pct", 0.0),
                    elapsed_pct * 100,
                )

        if not reason:
            rolled = await self._check_and_execute_leg_roll(updated_trade, current_time)
            if rolled:
                return  # re-evaluate next tick with updated strikes

        if reason:
            logger.info(
                "IC auto-exit accepted source=%s reason=%s current_premium=%.2f",
                pricing_source,
                reason,
                current_premium,
            )
            await self._exit_iron_condor_trade(updated_trade, reason, current_premium)

    async def _monitor_directional_trade(self, trade: dict[str, Any]) -> None:
        try:
            if not self._broker_available():
                return

            quote = await self._fetch_nfo_quote(trade["symbol"])
            ltp = self.broker.parse_ltp(quote)

            if not ltp or ltp <= 0:
                await self._track_quote_degradation(
                    is_degraded=True,
                    streak_attr="_directional_bad_quote_streak",
                    level_attr="_directional_bad_quote_alert_level",
                    event_name="DIRECTIONAL_QUOTE_DEGRADED",
                    context={"symbol": trade.get("symbol")},
                )
                return

            await self._track_quote_degradation(
                is_degraded=False,
                streak_attr="_directional_bad_quote_streak",
                level_attr="_directional_bad_quote_alert_level",
                event_name="DIRECTIONAL_QUOTE_DEGRADED",
                context={"symbol": trade.get("symbol")},
            )

            entry = _to_float(trade.get("entry_price"), None)
            if entry is None:
                logger.warning("Directional monitor: missing entry_price — skipping tick")
                return
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
                    logger.info("SL upgraded to breakeven: %.2f (ltp %.2f)", sl_price, ltp)

            trail_sl = round(trade["max_price"] * (1 - settings.trailing_pct), 2)

            now = datetime.now(IST)

            if ltp <= sl_price:
                await self._exit_trade(trade, "STOPLOSS", ltp)
                return

            if not t1_booked and ltp >= t1_price:
                await self._book_partial(trade, ltp)
                return

            if t1_booked:
                if ltp >= t2_price:
                    await self._exit_remaining(trade, "TARGET_2", ltp)
                    return

                if ltp <= trail_sl:
                    await self._exit_remaining(trade, "TRAIL_STOP", ltp)
                    return

            if now.time() >= self._square_off_time:
                await self._exit_trade(trade, "EOD_SQUAREOFF", ltp)
                return

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

            if state.active_trade.get("t1_booked"):
                logger.info("BOOK PARTIAL SKIPPED: t1 already booked (idempotency guard)")
                return

            latest_trade = {**state.active_trade, **trade}
            symbol = latest_trade["symbol"]
            t1_qty = latest_trade.get("t1_qty", latest_trade["qty"] // 2)
            logger.info("BOOKING PARTIAL: %s qty=%d ltp=%.2f", symbol, t1_qty, ltp)

            sell_id, fill_price, fill_state, filled_qty = await self._sell_with_retry(
                symbol, t1_qty, "TARGET_1"
            )

            if fill_state == "PARTIAL" and 0 < filled_qty < t1_qty:
                remaining = t1_qty - filled_qty
                logger.warning(
                    "T1 partial fill %d/%d — retrying remainder %d after %.1fs",
                    filled_qty, t1_qty, remaining, _PARTIAL_FILL_RETRY_DELAY,
                )
                await asyncio.sleep(_PARTIAL_FILL_RETRY_DELAY)
                sid2, fp2, fs2, fq2 = await self._sell_with_retry(symbol, remaining, "TARGET_1_RETRY")
                if sid2 and fs2 == "FILLED":
                    filled_qty += fq2
                    if fp2 is not None:
                        fill_price = fp2
                    fill_state = "FILLED"
                else:
                    logger.critical(
                        "T1 partial fill UNRECOVERED filled=%d/%d retry_state=%s",
                        filled_qty, t1_qty, fs2,
                    )
                    await self.event_bus.publish(
                        "T1_PARTIAL_FILL_UNRECOVERED",
                        {"symbol": symbol, "filled": filled_qty, "requested": t1_qty},
                    )
                    await self._handle_fatal_exception(
                        "T1_PARTIAL_FILL_UNRECOVERED",
                        RuntimeError(f"T1 partial fill {filled_qty}/{t1_qty}"),
                    )
                    return

            if not sell_id or fill_state not in {"FILLED"}:
                logger.critical(
                    "T1 SELL FAILED — position may be open! %s qty=%d state=%s",
                    symbol, t1_qty, fill_state,
                )
                await self.event_bus.publish(
                    "SELL_FAILED_CRITICAL",
                    {"symbol": symbol, "qty": t1_qty, "reason": "TARGET_1", "fill_state": fill_state},
                )
                return

            quote = await self._fetch_nfo_quote(symbol)
            bid, _ = self.broker.parse_bid_ask(quote)
            conservative_ltp = bid if bid else ltp
            exit_price = fill_price if fill_price else conservative_ltp
            t1_pnl = round((exit_price - latest_trade["entry_price"]) * filled_qty, 2)

            logger.info(
                "T1 BOOKED: order=%s fill=%.2f pnl=%.2f qty=%d",
                sell_id, exit_price, t1_pnl, filled_qty,
            )

            latest_trade["t1_booked"] = True
            latest_trade["t1_pnl"] = t1_pnl
            latest_trade["t1_exit_price"] = exit_price
            latest_trade["t1_filled_qty"] = filled_qty

            await self.state_manager.update(active_trade=latest_trade)
            await self.event_bus.publish("PARTIAL_BOOKED", {"trade": latest_trade})

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

            logger.info("EXITING REMAINING: %s qty=%d reason=%s ltp=%.2f", symbol, t2_qty, reason, ltp)

            sell_id, fill_price, fill_state, filled_qty = await self._sell_with_retry(
                symbol, t2_qty, reason
            )

            if fill_state == "PARTIAL" and 0 < filled_qty < t2_qty:
                remaining = t2_qty - filled_qty
                logger.warning(
                    "T2 partial fill %d/%d — retrying remainder %d after %.1fs",
                    filled_qty, t2_qty, remaining, _PARTIAL_FILL_RETRY_DELAY,
                )
                await asyncio.sleep(_PARTIAL_FILL_RETRY_DELAY)
                sid2, fp2, fs2, fq2 = await self._sell_with_retry(symbol, remaining, f"{reason}_RETRY")
                if sid2 and fs2 == "FILLED":
                    filled_qty += fq2
                    if fp2 is not None:
                        fill_price = fp2
                    fill_state = "FILLED"
                else:
                    logger.critical(
                        "T2 partial fill UNRECOVERED filled=%d/%d retry_state=%s",
                        filled_qty, t2_qty, fs2,
                    )
                    await self.event_bus.publish(
                        "T2_PARTIAL_FILL_UNRECOVERED",
                        {"symbol": symbol, "filled": filled_qty, "requested": t2_qty, "reason": reason},
                    )
                    await self._handle_fatal_exception(
                        f"exit_remaining_partial_unrecovered:{reason}",
                        RuntimeError(f"T2 partial fill {filled_qty}/{t2_qty}"),
                    )
                    return

            if not sell_id or fill_state != "FILLED":
                logger.critical(
                    "T2 SELL FAILED — position may be open! %s qty=%d reason=%s state=%s",
                    symbol, t2_qty, reason, fill_state,
                )
                await self.event_bus.publish(
                    "SELL_FAILED_CRITICAL",
                    {"symbol": symbol, "qty": t2_qty, "reason": reason, "fill_state": fill_state},
                )
                await self._handle_fatal_exception(
                    f"exit_remaining_sell_failed:{reason}",
                    RuntimeError("Remaining position sell failed"),
                )
                return

            quote = await self._fetch_nfo_quote(symbol)
            bid, _ = self.broker.parse_bid_ask(quote)
            conservative_ltp = bid if bid else ltp
            exit_price = fill_price if fill_price else conservative_ltp
            t2_pnl = round((exit_price - trade["entry_price"]) * t2_qty, 2)
            total_pnl = round(trade.get("t1_pnl", 0) + t2_pnl, 2)
            new_daily = round(state.daily_pnl + t2_pnl, 2)

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
                "TRADE CLOSED (REMAINING): %s reason=%s exit=%.2f pnl=%.2f daily_pnl=%.2f",
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

            logger.info("EXITING TRADE: %s qty=%d reason=%s ltp=%.2f", symbol, qty, reason, ltp)

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

            quote = await self._fetch_nfo_quote(symbol)
            bid, _ = self.broker.parse_bid_ask(quote)
            conservative_ltp = bid if bid else ltp
            exit_price = fill_price if fill_price else conservative_ltp
            exit_pnl = round((exit_price - trade["entry_price"]) * qty, 2)
            total_pnl = round(trade.get("t1_pnl", 0) + exit_pnl, 2)
            new_daily = round(state.daily_pnl + exit_pnl, 2)

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
                "TRADE CLOSED: %s reason=%s exit=%.2f pnl=%.2f daily_pnl=%.2f",
                symbol,
                reason,
                exit_price,
                total_pnl,
                new_daily,
            )

            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})

    async def _buy_with_retry(self, symbol: str, qty: int) -> tuple[str | None, float | None, int]:
        """
        Place a BUY order with retry, fill confirmation, partial-fill handling,
        and safe cancellation of unfilled remainder.

        Returns: (order_id, fill_price, filled_qty). If no quantity is safely
        filled, returns (None, None, 0).
        """
        requested_qty = _to_int(qty, 0)
        if requested_qty <= 0:
            logger.error("BUY rejected locally: invalid qty=%s symbol=%s", qty, symbol)
            return None, None, 0

        if not self._broker_available():
            if self._is_paper_mode():
                fill = await self._paper_quote_price(symbol, fallback=0.0)
                return self._paper_safe_order_id("BUY", symbol), fill, requested_qty
            logger.error("BUY failed: broker unavailable symbol=%s qty=%d", symbol, requested_qty)
            return None, None, 0

        for attempt in range(1, _SELL_MAX_RETRIES + 1):
            order_id: str | None = None
            immediate_fill: float | None = None

            try:
                order_id, immediate_fill = await self.broker.place_order_and_wait_fill(
                    symbol=symbol,
                    side="BUY",
                    quantity=requested_qty,
                )
            except Exception as exc:
                logger.error(
                    "BUY attempt %d/%d exception symbol=%s qty=%d err=%s",
                    attempt,
                    _SELL_MAX_RETRIES,
                    symbol,
                    requested_qty,
                    exc,
                )

            if not order_id:
                if attempt < _SELL_MAX_RETRIES:
                    await asyncio.sleep(_SELL_RETRY_DELAY)
                continue

            status, filled_qty, avg_price = await self._await_fill_confirmation(
                order_id,
                requested_qty,
                "BUY",
            )
            data = status.get("orderDetails") or status.get("data") or status
            if isinstance(data, list):
                data = data[0] if data else {}
            broker_state = str(data.get("orderStatus") or data.get("status") or "").upper()

            fill_price = avg_price if avg_price is not None else immediate_fill

            if filled_qty >= requested_qty or broker_state in {"COMPLETE", "FILLED", "TRADED"}:
                logger.info(
                    "BUY filled attempt=%d order=%s qty=%d/%d fill=%s",
                    attempt,
                    order_id,
                    min(filled_qty or requested_qty, requested_qty),
                    requested_qty,
                    f"{fill_price:.2f}" if fill_price is not None else "N/A",
                )
                return order_id, fill_price, min(filled_qty or requested_qty, requested_qty)

            if filled_qty > 0:
                logger.warning(
                    "BUY partial fill accepted order=%s filled=%d/%d; cancelling remainder",
                    order_id,
                    filled_qty,
                    requested_qty,
                )
                await self._safe_cancel_order(order_id)
                return order_id, fill_price, filled_qty

            if broker_state in {"REJECTED", "CANCELLED", "CANCELED"}:
                logger.warning(
                    "BUY terminal state attempt=%d/%d order=%s state=%s; retrying if allowed",
                    attempt,
                    _SELL_MAX_RETRIES,
                    order_id,
                    broker_state,
                )
            else:
                logger.warning(
                    "BUY not filled attempt=%d/%d order=%s state=%s; cancelling before retry",
                    attempt,
                    _SELL_MAX_RETRIES,
                    order_id,
                    broker_state or "UNKNOWN",
                )
                await self._safe_cancel_order(order_id)

            if attempt < _SELL_MAX_RETRIES:
                await asyncio.sleep(_SELL_RETRY_DELAY)

        logger.error("BUY failed after retries symbol=%s qty=%d", symbol, requested_qty)
        return None, None, 0

    async def _sell_with_retry(
        self, symbol: str, qty: int, reason: str
    ) -> tuple[str | None, float | None, str, int]:
        if self._is_paper_mode():
            ltp = await self._paper_quote_price(symbol, fallback=0.0)
            sell_id = self._paper_safe_order_id("SELL", symbol)
            logger.info("PAPER MODE: simulated SELL symbol=%s qty=%d reason=%s fill=%.2f", symbol, qty, reason, ltp)
            return sell_id, ltp, "FILLED", qty

        for attempt in range(1, _SELL_MAX_RETRIES + 1):
            try:
                sell_id, fill_price, fill_state, filled_qty = await self.broker.place_order_with_fill_info(
                    symbol=symbol,
                    side="SELL",
                    quantity=qty,
                )

                if sell_id and fill_state == "FILLED":
                    logger.info(
                        "SELL OK attempt=%d reason=%s order=%s fill=%s qty=%d",
                        attempt,
                        reason,
                        sell_id,
                        f"{fill_price:.2f}" if fill_price else "N/A",
                        filled_qty,
                    )
                    return sell_id, fill_price, fill_state, filled_qty

                if sell_id and fill_state == "PARTIAL":
                    logger.warning(
                        "SELL PARTIAL attempt=%d reason=%s order=%s filled=%d/%d",
                        attempt,
                        reason,
                        sell_id,
                        filled_qty,
                        qty,
                    )
                    return sell_id, fill_price, fill_state, filled_qty

                if sell_id and fill_state in {"REJECTED", "CANCELLED", "CANCELED"}:
                    logger.error(
                        "SELL %s attempt=%d reason=%s order=%s — broker rejected, not retrying",
                        fill_state,
                        attempt,
                        reason,
                        sell_id,
                    )
                    return sell_id, None, fill_state, 0

                logger.warning(
                    "SELL attempt %d/%d unfilled state=%s reason=%s",
                    attempt,
                    _SELL_MAX_RETRIES,
                    fill_state,
                    reason,
                )
            except Exception as exc:
                logger.error("SELL attempt %d/%d exception reason=%s: %s", attempt, _SELL_MAX_RETRIES, reason, exc)

            if attempt < _SELL_MAX_RETRIES:
                await asyncio.sleep(_SELL_RETRY_DELAY)

        logger.critical(
            "EMERGENCY SELL: all %d retries failed — placing emergency order %s qty=%d",
            _SELL_MAX_RETRIES,
            symbol,
            qty,
        )

        try:
            emergency_id, fill_price, fill_state, filled_qty = await self.broker.place_order_with_fill_info(
                symbol=symbol,
                side="SELL",
                quantity=qty,
            )

            if emergency_id:
                logger.critical(
                    "EMERGENCY SELL placed order=%s state=%s filled=%d/%d fill=%s",
                    emergency_id,
                    fill_state,
                    filled_qty,
                    qty,
                    f"{fill_price:.2f}" if fill_price else "N/A",
                )
                return emergency_id, fill_price, fill_state, filled_qty
        except Exception as exc:
            logger.critical("EMERGENCY SELL also failed: %s", exc)

        return None, None, "FAILED", 0

    async def emergency_exit_active_trade(self, reason: str = "EMERGENCY") -> bool:
        state = await self.state_manager.snapshot()
        trade = state.active_trade

        if not trade:
            return True

        if trade.get("strategy") == "IRON_CONDOR":
            # Attempt a fresh quote fetch so exit P&L uses current prices,
            # not a potentially minutes-old cached value.
            current_premium = _to_float(
                trade.get("current_premium") or trade.get("entry_price"),
                0.0,
            )
            if self._broker_available() and trade.get("legs"):
                try:
                    fresh_premium = 0.0
                    for leg in trade.get("legs", []):
                        sym = leg.get("symbol")
                        side = str(leg.get("side", "")).upper()
                        if not sym or side not in {"BUY", "SELL"}:
                            raise ValueError(f"bad leg {leg}")
                        quote = await asyncio.wait_for(
                            self._fetch_nfo_quote(sym),
                            timeout=float(getattr(settings, "broker_quote_timeout_seconds", 3)),
                        )
                        bid, ask = self.broker.parse_bid_ask(quote)
                        ltp = self.broker.parse_ltp(quote)
                        # close price: buy-back shorts at ask, sell longs at bid
                        close_price = (ask or ltp or 0.0) if side == "SELL" else (bid or ltp or 0.0)
                        if side == "SELL":
                            fresh_premium += float(close_price or 0.0)
                        else:
                            fresh_premium -= float(close_price or 0.0)
                    if fresh_premium > 0:
                        logger.info(
                            "Emergency exit: fresh premium=%.2f (was cached=%.2f)",
                            fresh_premium,
                            current_premium,
                        )
                        current_premium = fresh_premium
                except Exception as quote_exc:
                    logger.warning(
                        "Emergency exit: fresh quote failed, using cached premium=%.2f err=%s",
                        current_premium,
                        quote_exc,
                    )

            closed = await self._exit_iron_condor_trade(trade, reason, current_premium)
            return closed is not None

        symbol = trade.get("symbol")
        qty = trade.get("t2_qty", trade.get("qty", 0) // 2) if trade.get("t1_booked") else trade.get("qty", 0)

        if not symbol or qty <= 0:
            await self.state_manager.update(active_trade=None, live_pnl=0.0)
            return False

        sell_id, _, fill_state, _ = await self._sell_with_retry(symbol, qty, reason)
        if not sell_id or fill_state != "FILLED":
            return False

        closed = await self._ensure_position_closed(symbol, reason, fallback_qty=qty)
        if closed:
            await self.state_manager.update(active_trade=None, live_pnl=0.0)

        return closed

    async def _health_loop(self):
        _consecutive_broker_failures = 0
        _MAX_BROKER_FAILURES_BEFORE_ALERT = 3

        while True:
            # Check more frequently when a trade is open — broker failure during
            # an active position needs to surface faster than every 60 seconds.
            state = await self.state_manager.snapshot()
            sleep_secs = 10 if state.active_trade else 60
            await asyncio.sleep(sleep_secs)

            if not self._broker_available():
                continue

            try:
                if not await self.broker.healthcheck():
                    logger.warning("Healthcheck failed — attempting re-login")
                    try:
                        await self.broker.login()
                        _consecutive_broker_failures = 0
                        logger.info("Re-login successful after healthcheck failure")
                    except Exception as login_exc:
                        _consecutive_broker_failures += 1
                        logger.error(
                            "Re-login failed consecutive=%d: %s",
                            _consecutive_broker_failures,
                            login_exc,
                        )

                    # Only disable trading + alert when broker is persistently down.
                    # Do NOT emergency-exit the position — the broker being
                    # temporarily unreachable is NOT a position breach.  Blindly
                    # exiting would realise a loss for a transient network blip.
                    if _consecutive_broker_failures >= _MAX_BROKER_FAILURES_BEFORE_ALERT:
                        fresh_state = await self.state_manager.snapshot()
                        if fresh_state.active_trade and not self._is_paper_mode():
                            logger.critical(
                                "Broker unreachable for %d consecutive checks with active trade "
                                "— disabling new entries, manual intervention required",
                                _consecutive_broker_failures,
                            )
                            await self.state_manager.update(
                                trading_enabled=False,
                                last_risk_breach=f"broker_unreachable_{_consecutive_broker_failures}",
                                manual_intervention_required=True,
                            )
                            await self.event_bus.publish(
                                "BROKER_UNREACHABLE",
                                {
                                    "consecutive_failures": _consecutive_broker_failures,
                                    "active_trade": True,
                                },
                            )
                else:
                    _consecutive_broker_failures = 0
            except Exception as exc:
                logger.error("Health loop error: %s", exc)

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

        logger.info("SYMBOL LOOKUP requested_strike=%s type=%s expiry=%s", strike, opt_type, expiry)

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
                logger.error("SAMCO validation error on full chain too: errors=%s expiry=%s type=%s", chain.get("validationErrors"), expiry, opt_type)
                return None

            rows = self._extract_chain_rows(chain)

        if not rows:
            response_keys = list(chain.keys()) if isinstance(chain, dict) else []
            logger.error(
                "Option chain empty after both attempts: expiry=%s strike=%s type=%s response_keys=%s",
                expiry,
                strike,
                opt_type,
                response_keys,
            )
            return None

        best_symbol = None
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
                    best_symbol = row.get("tradingSymbol")
                    best_strike = row_strike
            except (TypeError, ValueError):
                continue

        if not best_symbol:
            logger.error("No valid strike in chain near %s. Available strikes: %s", strike, sorted(available_strikes)[:20])
            return None

        if best_diff > 0:
            logger.warning("Strike snap: requested=%s -> got nearest=%s (diff=%s)", strike, best_strike, best_diff)

        self._symbol_cache[key] = best_symbol
        logger.info("SYMBOL RESOLVED: requested_strike=%s type=%s -> %s (snap_diff=%s)", strike, opt_type, best_symbol, best_diff)

        return best_symbol

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
        self._ic_quote_cache.clear()

    def record_market_tick(self, spot: float, iv: float | None) -> None:
        """Called by the scheduler every tick to feed trend and IV-rank tracking."""
        today = datetime.now(IST).date()
        if self._session_open_date != today:
            self._session_open_date = today
            self._session_open_spot = spot
        self._spot_history.append(spot)
        if iv and iv > 0:
            self._engine_iv_history.append(iv)

    def _compute_trend_strength(self) -> float:
        """Directional efficiency ratio: 0 = choppy, 1 = strong trend."""
        prices = list(self._spot_history)
        if len(prices) < 5:
            return 0.0
        price_range = max(prices) - min(prices)
        net_move = abs(prices[-1] - prices[0])
        return round(net_move / price_range, 3) if price_range > 1.0 else 0.0

    def _compute_session_iv_rank(self, current_iv: float) -> float | None:
        """Session-based IV rank (0–1). Returns None if insufficient history."""
        ivs = list(self._engine_iv_history)
        if len(ivs) < 10:
            return None
        iv_min, iv_max = min(ivs), max(ivs)
        if iv_max <= iv_min:
            return None
        return round(max(0.0, min(1.0, (current_iv - iv_min) / (iv_max - iv_min))), 3)

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
        if not self._broker_available():
            logger.info("No broker available: skipping cancel order=%s", order_id)
            return

        if self._is_paper_mode() and not self._paper_mode_use_broker():
            logger.info("PURE PAPER MODE: skipping cancel order=%s", order_id)
            return

        try:
            await self.broker.cancel_order(order_id)
        except Exception as exc:
            logger.warning("Cancel failed order=%s err=%s", order_id, exc)

    async def _ensure_position_closed(self, symbol: str, reason: str, fallback_qty: int) -> bool:
        if not self._broker_position_checks_enabled():
            logger.info(
                "Broker position validation skipped symbol=%s reason=%s fallback_qty=%d",
                symbol,
                reason,
                fallback_qty,
            )
            return True

        for attempt in range(1, _EXIT_VERIFY_ATTEMPTS + 1):
            open_qty = await self._get_open_position_qty(symbol)

            if open_qty < 0:
                logger.critical("EXIT VALIDATION INCONCLUSIVE: position API unavailable symbol=%s reason=%s", symbol, reason)
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
        if not self._broker_position_checks_enabled():
            return 0

        try:
            positions = await self.broker.get_positions()
        except Exception as exc:
            logger.warning("Position fetch failed for exit validation: %s", exc)
            return -1

        total = 0
        symbol_upper = str(symbol).upper()

        for position in positions or []:
            trading_symbol = str(position.get("tradingSymbol") or position.get("symbolName") or "").upper()
            if trading_symbol != symbol_upper:
                continue

            for key in ("netQty", "netQuantity", "quantity", "netPosition"):
                try:
                    total = int(float(str(position.get(key, 0)).replace(",", "").strip()))
                    break
                except (TypeError, ValueError):
                    continue

        return max(total, 0)

    async def _validate_post_order_position(self, symbol: str, expected_qty: int, context: str) -> None:
        if not self._broker_position_checks_enabled():
            logger.info("Broker position validation skipped symbol=%s", symbol)
            return

        if expected_qty <= 0:
            raise RuntimeError(f"{context} invalid expected qty={expected_qty}")

        observed_qty = await self._get_open_position_qty(symbol)

        if observed_qty < 0:
            raise RuntimeError(f"{context} position check failed: broker positions unavailable")

        if observed_qty < expected_qty:
            raise RuntimeError(f"{context} position mismatch expected>={expected_qty} observed={observed_qty}")

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
            logger.critical("PAPER MODE: force exit + halt symbol=%s qty=%s reason=%s", symbol, qty, reason)
            await self.event_bus.publish("ORDER_UNCERTAIN", {"reason": reason, "symbol": symbol, "qty": qty})
            return

        forced_fill_state = "UNKNOWN"
        forced_filled_qty = 0
        forced_order_id: str | None = None

        try:
            forced_order_id, fill_price, forced_fill_state, forced_filled_qty = (
                await self.broker.place_order_with_fill_info(
                    symbol=symbol,
                    side="SELL",
                    quantity=qty,
                )
            )
            if forced_fill_state == "FILLED":
                logger.critical(
                    "Forced exit FILLED symbol=%s qty=%d order=%s fill=%s reason=%s",
                    symbol,
                    qty,
                    forced_order_id,
                    f"{fill_price:.2f}" if fill_price else "N/A",
                    reason,
                )
            else:
                logger.critical(
                    "Forced exit UNCONFIRMED symbol=%s qty=%d order=%s state=%s filled=%d/%d reason=%s",
                    symbol,
                    qty,
                    forced_order_id,
                    forced_fill_state,
                    forced_filled_qty,
                    qty,
                    reason,
                )
        except Exception as exc:
            logger.critical("Forced exit failed symbol=%s qty=%s err=%s", symbol, qty, exc, exc_info=True)

        try:
            await self.broker.cancel_all_open_orders()
        except Exception as exc:
            logger.warning("cancel_all_open_orders failed: %s", exc)

        await self.event_bus.publish(
            "ORDER_UNCERTAIN",
            {
                "reason": reason,
                "symbol": symbol,
                "qty": qty,
                "order_id": forced_order_id,
                "fill_state": forced_fill_state,
                "filled_qty": forced_filled_qty,
            },
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

        orb_high = getattr(state, "orb_high", None)
        orb_low = getattr(state, "orb_low", None)

        if signal == "CALL" and orb_high:
            min_break = orb_high + settings.breakout_buffer
            max_break = orb_high * (1 + settings.max_breakout_extension_pct)

            if state.spot_price < min_break or state.spot_price > max_break:
                logger.warning(
                    "Fake breakout/spike block CALL: spot=%.2f range=[%.2f, %.2f]",
                    state.spot_price,
                    min_break,
                    max_break,
                )
                return False

        if signal == "PUT" and orb_low:
            max_break = orb_low - settings.breakout_buffer
            min_break = orb_low * (1 - settings.max_breakout_extension_pct)

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

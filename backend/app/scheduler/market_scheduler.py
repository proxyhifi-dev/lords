# backend/app/scheduler/market_scheduler.py
from __future__ import annotations

import asyncio
import time as wall_time
from collections import deque
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from backend.app.broker.samco_client import SamcoClient, get_weekly_expiry
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.reconciliation import ReconciliationEngine
from backend.app.engine.state_manager import state_manager
from backend.app.engine.trading_engine import TradingEngine
from backend.app.notifications.telegram import TelegramNotifier
from backend.app.risk.risk_manager import RiskManager
from backend.app.storage.trade_store import TradeStore
from backend.app.utils.logger import get_logger

settings = get_settings()
logger = get_logger("market_scheduler")
IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hhmm(value: Any, setting_name: str) -> time:
    text = str(value or "").strip()

    try:
        hour, minute = map(int, text.split(":")[:2])
        return time(hour, minute)
    except Exception as exc:
        raise RuntimeError(f"Invalid time setting {setting_name}={text!r}") from exc


def required_setting(name: str) -> Any:
    if not hasattr(settings, name):
        raise RuntimeError(f"Missing setting in config_loader.py: {name}")

    value = getattr(settings, name)

    if value is None or str(value).strip() == "":
        raise RuntimeError(f"Empty setting in .env/config_loader.py: {name}")

    return value


def setting_int(name: str) -> int:
    value = required_setting(name)

    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid integer setting {name}={value!r}") from exc


def setting_float(name: str) -> float:
    value = required_setting(name)

    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid float setting {name}={value!r}") from exc


def setting_time(name: str) -> time:
    return parse_hhmm(required_setting(name), name)


def is_market_open() -> bool:
    current_time = now_ist()

    if current_time.weekday() >= 5:
        return False

    market_open = setting_time("market_open_time")
    market_close = setting_time("market_close_time")

    return market_open <= current_time.time() <= market_close


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def is_iron_condor_trade(trade: dict[str, Any] | None) -> bool:
    if not trade:
        return False

    strategy = str(trade.get("strategy") or trade.get("signal") or "").strip().upper()
    return strategy == "IRON_CONDOR"


def _order_status_text(payload: Any) -> str:
    data = payload
    if isinstance(data, dict):
        data = data.get("orderDetails") or data.get("data") or data
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return ""
    return str(data.get("orderStatus") or data.get("status") or "").strip().upper()


def _order_filled_qty(payload: Any) -> int:
    data = payload
    if isinstance(data, dict):
        data = data.get("orderDetails") or data.get("data") or data
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return 0
    for key in ("filledQty", "filledShares", "tradedQty", "executedQty", "tradedQuantity"):
        try:
            qty = int(float(str(data.get(key, 0)).replace(",", "").strip()))
        except (TypeError, ValueError):
            continue
        if qty > 0:
            return qty
    return 0


class MarketScheduler:
    def __init__(self) -> None:
        self.state = state_manager
        self.trade_store = TradeStore()
        self.broker = SamcoClient()
        self.event_bus = EventBus()

        self.risk = RiskManager(self.event_bus, self.state, self.broker)

        self.engine = TradingEngine(
            event_bus=self.event_bus,
            state_manager=self.state,
            trade_store=self.trade_store,
            broker=self.broker,
        )

        self._reconciler = ReconciliationEngine(
            broker=self.broker,
            state_manager=self.state,
            event_bus=self.event_bus,
        )

        self.notifier = TelegramNotifier(self.event_bus)

        self.running = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._startup_task: asyncio.Task[Any] | None = None

        self._last_signal_time = 0.0
        self._signal_block_until = 0.0
        self._last_closed_log_time = 0.0
        self._daily_reset_date = ""
        self._last_tick_time = wall_time.time()
        self._last_good_quote_time = wall_time.time()
        self._consecutive_quote_failures = 0
        self._last_broker_error_time = 0.0
        self._last_manual_flatten_time = 0.0

        self._latest_iv: float | None = None
        self._iv_history: deque[float] = deque(maxlen=20)

        self._entry_window_start = setting_time("ic_entry_window_start")
        self._entry_window_end = setting_time("ic_entry_window_end")

        self._current_candle_minute: datetime | None = None
        self._current_candle: dict[str, Any] | None = None

    def get_status_summary(self) -> dict[str, Any]:
        now_ts = wall_time.time()
        return {
            "running": self.running,
            "last_tick_age_sec": round(max(0.0, now_ts - self._last_tick_time), 2),
            "last_good_quote_age_sec": round(max(0.0, now_ts - self._last_good_quote_time), 2),
            "consecutive_quote_failures": int(self._consecutive_quote_failures),
            "signal_cooldown_active": bool(
                self._last_signal_time
                and (now_ist().timestamp() - self._last_signal_time) < self.signal_cooldown_seconds
            ),
            "manual_flatten_cooldown_active": bool(
                self._last_manual_flatten_time
                and (now_ts - self._last_manual_flatten_time) < self.manual_flatten_cooldown_seconds
            ),
            "scheduler_stall_warn_seconds": self.scheduler_stall_warn_seconds,
            "scheduler_stall_hard_seconds": setting_float("scheduler_stall_hard_seconds"),
        }

    def _update_candle(self, price: float, timestamp: datetime) -> dict[str, Any] | None:
        """
        Build completed 1-minute candles from tick prices.

        Returns None while the current minute is still forming.
        Returns the just-closed candle when a new minute starts.
        """
        minute = timestamp.replace(second=0, microsecond=0)
        price = float(price)

        if self._current_candle is None or self._current_candle_minute is None:
            self._current_candle_minute = minute
            self._current_candle = {
                "time": minute,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
            }
            return None

        if minute == self._current_candle_minute:
            self._current_candle["high"] = max(float(self._current_candle["high"]), price)
            self._current_candle["low"] = min(float(self._current_candle["low"]), price)
            self._current_candle["close"] = price
            return None

        closed = dict(self._current_candle)

        self._current_candle_minute = minute
        self._current_candle = {
            "time": minute,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
        }

        return closed

    @property
    def closed_log_interval_seconds(self) -> int:
        return setting_int("closed_log_interval_seconds")

    @property
    def signal_cooldown_seconds(self) -> int:
        return setting_int("signal_cooldown_seconds")

    @property
    def signal_rejection_cooldown_seconds(self) -> int:
        return setting_int("signal_rejection_cooldown_seconds")

    @property
    def startup_reconcile_timeout_seconds(self) -> int:
        return setting_int("startup_reconcile_timeout_seconds")

    @property
    def broker_quote_timeout_seconds(self) -> int:
        return setting_int("broker_quote_timeout_seconds")

    @property
    def daily_reset_check_interval_seconds(self) -> int:
        return setting_int("daily_reset_check_interval_seconds")

    @property
    def manual_flatten_cooldown_seconds(self) -> int:
        return setting_int("manual_flatten_cooldown_seconds")

    @property
    def scheduler_stall_warn_seconds(self) -> float:
        return setting_float("scheduler_stall_warn_seconds")

    @property
    def reconciliation_interval_seconds(self) -> int:
        return setting_int("reconciliation_interval_seconds")

    def _track_task(
        self,
        task: asyncio.Task[Any],
        name: str,
        *,
        allow_normal_finish: bool = False,
    ) -> asyncio.Task[Any]:
        def done_callback(done_task: asyncio.Task[Any]) -> None:
            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                logger.info("Task cancelled: %s", name)
                return
            except Exception as callback_exc:
                logger.error(
                    "Task callback failed for %s: %s",
                    name,
                    callback_exc,
                    exc_info=True,
                )
                return

            if exc is not None:
                logger.error("Task crashed: %s err=%s", name, exc, exc_info=True)
                return

            if allow_normal_finish:
                logger.info("Task completed: %s", name)
            else:
                logger.warning("Task finished unexpectedly: %s", name)

        task.add_done_callback(done_callback)
        return task

    async def start(self) -> None:
        if self.running:
            logger.warning("Scheduler already running")
            return

        logger.info("SCHEDULER START CALLED")
        logger.info(
            "Starting Lords Bot (Iron Condor) — mode=%s strategy=%s",
            settings.mode.upper(),
            settings.strategy_type.upper(),
        )

        await self.state.load()
        state = await self.state.snapshot()

        logger.info(
            "State check: spot_price=%s trading_enabled=%s active_trade=%s "
            "last_ic_month=%s last_trade_date=%s trade_count=%s",
            state.spot_price,
            state.trading_enabled,
            bool(state.active_trade),
            getattr(state, "last_iron_condor_month", None),
            getattr(state, "last_trade_date", None),
            getattr(state, "trade_count", None),
        )

        await self.event_bus.start()

        try:
            await self.broker.login()
        except Exception as exc:
            logger.error("SAMCO login failed in scheduler.start(): %s", exc, exc_info=True)
            raise

        self.running = True
        await self.state.update(bot_running=True)

        self._startup_task = self._track_task(
            asyncio.create_task(
                self._run_startup_reconciliation(),
                name="reconcile-startup",
            ),
            "reconcile-startup",
            allow_normal_finish=True,
        )

        self._tasks = [
            self._track_task(
                asyncio.create_task(self._loop(), name="market-loop"),
                "market-loop",
            ),
            self._track_task(
                asyncio.create_task(self.risk.run(), name="risk-manager"),
                "risk-manager",
            ),
            self._track_task(
                asyncio.create_task(self.engine.run(), name="trading-engine"),
                "trading-engine",
            ),
            self._track_task(
                asyncio.create_task(self._daily_watcher(), name="daily-reset"),
                "daily-reset",
            ),
            self._track_task(
                asyncio.create_task(
                    self._reconciler.run_loop(self.reconciliation_interval_seconds),
                    name="reconciler",
                ),
                "reconciler",
            ),
            self._track_task(
                asyncio.create_task(self._rejection_watcher(), name="rejection-watcher"),
                "rejection-watcher",
            ),
            self._track_task(
                asyncio.create_task(self.notifier.run(), name="telegram-notifier"),
                "telegram-notifier",
                allow_normal_finish=True,
            ),
        ]

        logger.info("All scheduler tasks started (%d tasks)", len(self._tasks))

    async def stop(self) -> None:
        if not self.running:
            return

        logger.info("Stopping Lords Bot scheduler")
        self.running = False

        await self.event_bus.stop()

        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()

            try:
                await self._startup_task
            except asyncio.CancelledError:
                pass

        self._startup_task = None

        for task in self._tasks:
            if task.done():
                continue

            task.cancel()

            try:
                await task
            except asyncio.CancelledError:
                pass

        self._tasks.clear()

        await self.state.update(bot_running=False)
        logger.info("Scheduler stopped")

    async def _run_startup_reconciliation(self) -> None:
        try:
            result = await asyncio.wait_for(
                self._reconciler.run_once(),
                timeout=self.startup_reconcile_timeout_seconds,
            )
            logger.info("Startup reconciliation completed: %s", result)
        except asyncio.TimeoutError:
            logger.warning("Startup reconciliation timed out")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Startup reconciliation failed: %s", exc, exc_info=True)

    async def _daily_watcher(self) -> None:
        logger.info("DAILY WATCHER TASK STARTED")

        while self.running:
            current_time = now_ist()
            today = current_time.date().isoformat()

            market_open = setting_time("market_open_time")
            market_open_dt = datetime.combine(current_time.date(), market_open, tzinfo=IST)
            reset_start_dt = market_open_dt - timedelta(minutes=1)

            should_reset = (
                reset_start_dt.time() <= current_time.time() < market_open
                and self._daily_reset_date != today
            )

            if should_reset:
                self._daily_reset_date = today

                logger.info("=== DAILY RESET ===")

                await self.state.daily_reset()
                self.trade_store.daily_reset()
                self.engine.clear_cache()

                self._last_signal_time = 0.0
                self._signal_block_until = 0.0
                self._last_manual_flatten_time = 0.0
                self._latest_iv = None
                self._iv_history.clear()
                self._current_candle_minute = None
                self._current_candle = None

                logger.info("=== DAILY RESET COMPLETE ===")

            await asyncio.sleep(self.daily_reset_check_interval_seconds)

    async def _rejection_watcher(self) -> None:
        logger.info("Scheduler rejection watcher started")

        ic_queue = self.event_bus.subscribe("IC_ENTRY_REJECTED")
        risk_queue = self.event_bus.subscribe("RISK_BLOCKED")

        persistent_risk_reasons = {"late_entry", "max_trades_hit"}

        def _extend(source: str, reason: str) -> None:
            cooldown = self.signal_rejection_cooldown_seconds
            self._signal_block_until = wall_time.time() + cooldown
            logger.info(
                "Signal cooldown extended by %ds source=%s reason=%s",
                cooldown,
                source,
                reason,
            )

        async def _watch_ic() -> None:
            async for event in self.event_bus.iter_events(ic_queue):
                payload = event.payload or {}
                _extend("IC_ENTRY_REJECTED", str(payload.get("reason") or "unknown"))

        async def _watch_risk() -> None:
            async for event in self.event_bus.iter_events(risk_queue):
                payload = event.payload or {}
                reason = str(payload.get("reason") or "")
                if reason in persistent_risk_reasons:
                    _extend("RISK_BLOCKED", reason)

        await asyncio.gather(_watch_ic(), _watch_risk())

    async def _loop(self) -> None:
        logger.info("MARKET LOOP TASK STARTED")

        while self.running:
            try:
                if is_market_open():
                    await self._handle_open_market_cycle()
                else:
                    self._handle_closed_market_cycle()
            except Exception as exc:
                logger.error("Market loop error: %s", exc, exc_info=True)

            await asyncio.sleep(settings.poll_seconds)

    async def _handle_open_market_cycle(self) -> None:
        current_ts = wall_time.time()
        delay = current_ts - self._last_tick_time

        if delay > self.scheduler_stall_warn_seconds:
            logger.error("Scheduler stalled! delay=%.2fs", delay)

        hard_stall_seconds = float(getattr(settings, "scheduler_stall_hard_seconds", 60) or 60)
        if delay > hard_stall_seconds:
            logger.critical(
                "Scheduler hard stall detected delay=%.2fs threshold=%.2fs — triggering fail-safe",
                delay,
                hard_stall_seconds,
            )
            await self._fail_safe_on_data_loss(reason=f"scheduler_stall_{delay:.0f}s")
            return

        data_stale = current_ts - self._last_good_quote_time

        if data_stale > settings.deadman_timeout:
            logger.critical(
                "Dead-man switch: market data stale for %.1fs",
                data_stale,
            )
            await self._fail_safe_on_data_loss(reason=f"data_stale_{data_stale:.0f}s")
            return

        await self._tick()

    def _handle_closed_market_cycle(self) -> None:
        current_ts = wall_time.time()

        self._last_tick_time = current_ts
        self._last_good_quote_time = current_ts
        self._consecutive_quote_failures = 0

        if current_ts - self._last_closed_log_time < self.closed_log_interval_seconds:
            return

        current_time = now_ist()
        reason = "weekend" if current_time.weekday() >= 5 else "outside market hours"

        logger.info("Market closed (%s) — polling paused", reason)
        self._last_closed_log_time = current_ts

    async def _tick(self) -> None:
        logger.info("TICK FUNCTION ENTERED")

        self._last_tick_time = wall_time.time()

        try:
            index_quote = await asyncio.wait_for(
                self.broker.get_index_quote(settings.nifty_symbol),
                timeout=self.broker_quote_timeout_seconds,
            )
            self._last_good_quote_time = wall_time.time()
            self._consecutive_quote_failures = 0
        except asyncio.TimeoutError:
            logger.warning("Broker timeout")
            self._consecutive_quote_failures += 1
            return
        except Exception as exc:
            self._consecutive_quote_failures += 1
            current_ts = wall_time.time()

            if current_ts - self._last_broker_error_time >= self.closed_log_interval_seconds:
                logger.warning("Broker quote unavailable: %s", exc, exc_info=True)
                self._last_broker_error_time = current_ts

            return

        spot = SamcoClient.parse_spot(index_quote)

        if spot is None:
            logger.warning("Spot parsing failed")
            self._consecutive_quote_failures += 1
            return

        logger.info("TICK: spot=%.2f", spot)

        closed_candle = self._update_candle(spot, now_ist())
        if closed_candle:
            await self.event_bus.publish("CANDLE_CLOSED", closed_candle)

        iv = self._extract_iv(index_quote)

        if iv is not None:
            self._latest_iv = iv
            self._iv_history.append(iv)

        try:
            updates: dict[str, Any] = {"spot_price": spot}
            if iv is not None:
                updates["current_iv"] = iv
            await self.state.update(**updates)
        except Exception as exc:
            logger.error("state.update spot_price failed: %s", exc, exc_info=True)
            return

        await self.event_bus.publish(
            "TICK",
            {
                "price": spot,
                "volume": float(index_quote.get("volume") or 0),
                "iv": float(iv or 0.0),
            },
        )

        state = await self.state.snapshot()
        current_time = now_ist()

        if not self._iron_condor_can_enter(current_time, spot, state):
            return

        payload = {
            "signal": "IRON_CONDOR",
            "spot_price": spot,
            "size_label": "FULL",
            "trend_score": 0,
        }

        await self.state.update(signal="IRON_CONDOR", signal_meta=payload)
        await self.event_bus.publish("SIGNAL", payload)

        self._last_signal_time = current_time.timestamp()

        logger.info("IRON_CONDOR entry signal emitted spot=%.2f", spot)

    def _iron_condor_can_enter(
        self,
        current_time: datetime,
        spot: float,
        state: Any,
    ) -> bool:
        if str(settings.strategy_type).strip().lower() != "iron_condor":
            logger.info("IC gate blocked: strategy_type is not iron_condor")
            return False

        if not bool(settings.iron_condor_enabled):
            logger.info("IC gate blocked: IRON_CONDOR_ENABLED=false")
            return False

        if state.active_trade:
            logger.info("IC gate blocked: active trade already open")
            return False

        if current_time.weekday() >= 5:
            logger.info("IC gate blocked: weekend")
            return False

        if bool(getattr(settings, "ic_skip_expiry_day_entry", True)):
            current_day = current_time.date()
            if get_weekly_expiry(current_day) == current_day:
                logger.info("IC gate blocked: expiry day %s", current_day.isoformat())
                return False

        if not state.trading_enabled:
            logger.info("IC gate blocked: trading_enabled=False")
            return False

        if self._last_manual_flatten_time:
            flatten_age = wall_time.time() - self._last_manual_flatten_time

            if flatten_age < self.manual_flatten_cooldown_seconds:
                logger.info(
                    "IC gate blocked: manual flatten cooldown active age=%.0fs",
                    flatten_age,
                )
                return False

        if not self._passes_cycle_limit(current_time, state):
            return False

        if not (self._entry_window_start <= current_time.time() < self._entry_window_end):
            logger.info(
                "IC gate blocked: outside time window %s-%s now=%s",
                self._entry_window_start.strftime("%H:%M"),
                self._entry_window_end.strftime("%H:%M"),
                current_time.strftime("%H:%M:%S"),
            )
            return False

        if self._last_signal_time:
            signal_age = current_time.timestamp() - self._last_signal_time

            if signal_age < self.signal_cooldown_seconds:
                logger.info(
                    "IC gate blocked: cooldown active last_signal_age=%.0fs",
                    signal_age,
                )
                return False

        if self._signal_block_until and wall_time.time() < self._signal_block_until:
            remaining = self._signal_block_until - wall_time.time()
            logger.info(
                "IC gate blocked: rejection cooldown active for %.0fs more",
                remaining,
            )
            return False

        logger.info(
            "IC gate passed: one_per_day=%s monthly_only=%s time=%s spot=%.2f",
            bool(getattr(settings, "ic_one_per_day", True)),
            bool(settings.ic_monthly_only),
            current_time.strftime("%H:%M:%S"),
            spot,
        )

        return True

    def _passes_cycle_limit(self, current_time: datetime, state: Any) -> bool:
        today = current_time.date().isoformat()

        if bool(getattr(settings, "ic_one_per_day", True)):
            for value in (
                getattr(state, "last_trade_date", None),
                getattr(state, "last_ic_trade_date", None),
                getattr(state, "iron_condor_trade_date", None),
            ):
                if value and str(value)[:10] == today:
                    logger.info("IC gate blocked: one-per-day lock value=%s", value)
                    return False

        if settings.ic_monthly_only:
            start_day = settings.ic_entry_day_start
            end_day = settings.ic_entry_day_end

            if not (start_day <= current_time.day <= end_day):
                logger.info(
                    "IC gate blocked: monthly mode day filter start=%d end=%d today=%d",
                    start_day,
                    end_day,
                    current_time.day,
                )
                return False

            if getattr(state, "last_iron_condor_month", None) == current_time.month:
                logger.info(
                    "IC gate blocked: already traded this month month=%d",
                    current_time.month,
                )
                return False

            return True

        return True

    def _extract_iv(self, quote: dict[str, Any]) -> float | None:
        if not isinstance(quote, dict):
            return None

        for key in ("impliedVolatility", "iv", "implied_volatility", "volatility"):
            raw = quote.get(key)

            if raw is None:
                continue

            try:
                iv = float(raw)
            except (TypeError, ValueError):
                continue

            if iv > 0:
                return iv

        return None

    async def _fail_safe_on_data_loss(self, reason: str = "stale_quote_stream") -> None:
        state = await self.state.snapshot()

        if not state.trading_enabled:
            return

        await self.state.update(
            trading_enabled=False,
            last_order_failed=True,
            last_risk_breach=reason,
        )
        logger.critical("Trading disabled due to %s", reason)

        if not state.active_trade:
            return

        try:
            result = await self.flatten_position()
            logger.critical("Fail-safe flatten attempted reason=%s result=%s", reason, result)
        except Exception as exc:
            logger.critical(
                "Fail-safe flatten failed reason=%s err=%s",
                reason,
                exc,
                exc_info=True,
            )

    async def flatten_position(self) -> dict[str, Any]:
        state = await self.state.snapshot()

        if not state.active_trade:
            return {"status": "no_active_trade"}

        trade = state.active_trade

        if is_iron_condor_trade(trade):
            return await self._flatten_iron_condor_trade(trade)

        return await self._flatten_directional_trade(trade)

    async def _broker_leg_position_map(self, trade: dict[str, Any]) -> dict[str, int]:
        if settings.is_paper:
            return {}

        positions = await self.broker.get_positions()
        leg_symbols = {
            str(leg.get("symbol")).strip()
            for leg in (trade.get("legs") or [])
            if str(leg.get("symbol") or "").strip()
        }
        result: dict[str, int] = {}
        for pos in positions:
            symbol = str(pos.get("tradingSymbol") or pos.get("symbolName") or "").strip()
            if not symbol or symbol not in leg_symbols:
                continue
            result[symbol] = to_int(
                pos.get("netQty") or pos.get("netQuantity") or pos.get("net_qty"),
                0,
            )
        return result

    async def _verify_flatten_order_statuses(
        self,
        trade: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]], str | None]:
        exit_legs = [
            leg for leg in (trade.get("exit_legs") or [])
            if isinstance(leg, dict) and str(leg.get("exit_order_id") or "").strip()
        ]
        if not exit_legs:
            return False, [], "missing_exit_order_proof"

        if not hasattr(self.broker, "get_order_status"):
            return False, [], "broker_order_status_api_unavailable"

        proof: list[dict[str, Any]] = []
        for leg in exit_legs:
            order_id = str(leg.get("exit_order_id") or "").strip()
            symbol = str(leg.get("symbol") or "").strip()
            try:
                status_payload = await self.broker.get_order_status(order_id)
            except Exception as exc:
                return False, proof, f"order_status_lookup_failed:{symbol}:{exc}"

            status = _order_status_text(status_payload)
            filled_qty = _order_filled_qty(status_payload)
            avg_fill = None
            if hasattr(self.broker, "get_actual_fill_price"):
                try:
                    avg_fill = await self.broker.get_actual_fill_price(order_id)
                except Exception:
                    avg_fill = None

            proof.append(
                {
                    "symbol": symbol,
                    "order_id": order_id,
                    "status": status,
                    "filled_qty": filled_qty,
                    "avg_fill_price": avg_fill,
                }
            )

            if status not in {"COMPLETE", "FILLED", "TRADED", "EXECUTED"}:
                return False, proof, f"ambiguous_exit_order_status:{symbol}:{status or 'UNKNOWN'}"

        return True, proof, None

    async def _verify_iron_condor_flatten(
        self,
        trade: dict[str, Any],
        *,
        max_attempts: int = 3,
        retry_delay: float = 1.0,
    ) -> dict[str, Any]:
        last_open: dict[str, int] = {}
        before = await self._broker_leg_position_map(trade)
        attempts_used = 0
        order_proof: list[dict[str, Any]] = []
        order_proof_ok, order_proof, proof_error = await self._verify_flatten_order_statuses(trade)
        if not order_proof_ok:
            unclosed_symbols = sorted(before.keys())
            await self.state.update(
                trading_enabled=False,
                circuit_breaker_open=True,
                last_order_failed=True,
                last_risk_breach="emergency_flatten_order_proof_failed",
                manual_intervention_required=True,
                emergency_flatten_verified=False,
                emergency_flatten_attempts=0,
                emergency_flatten_unclosed_symbols=unclosed_symbols,
                emergency_flatten_last_error=proof_error,
                emergency_flatten_order_proof=order_proof,
                active_trade={
                    **trade,
                    "manual_intervention_required": True,
                    "emergency_flatten_verified": False,
                    "remaining_broker_positions": before,
                    "emergency_flatten_order_proof": order_proof,
                },
            )
            await self.event_bus.publish(
                "EMERGENCY_FLATTEN_UNVERIFIED",
                {
                    "remaining_positions": before,
                    "last_error": proof_error,
                    "order_proof": order_proof,
                },
            )
            return {
                "verified": False,
                "attempts": 0,
                "before_positions": before,
                "remaining_positions": before,
                "last_error": proof_error,
                "order_proof": order_proof,
            }

        for attempt in range(1, max_attempts + 1):
            attempts_used = attempt
            current = await self._broker_leg_position_map(trade)
            open_positions = {symbol: qty for symbol, qty in current.items() if qty != 0}
            if not open_positions:
                await self.state.update(
                    emergency_flatten_verified=True,
                    manual_intervention_required=False,
                    emergency_flatten_attempts=attempt,
                    emergency_flatten_unclosed_symbols=[],
                    emergency_flatten_last_error=None,
                    emergency_flatten_order_proof=order_proof,
                )
                return {
                    "verified": True,
                    "attempts": attempt,
                    "before_positions": before,
                    "remaining_positions": {},
                    "last_error": None,
                    "order_proof": order_proof,
                }

            last_open = dict(open_positions)
            if attempt < max_attempts:
                for symbol, qty in open_positions.items():
                    side = "SELL" if qty > 0 else "BUY"
                    order_id = None
                    fill_state = None
                    avg_fill = None
                    if hasattr(self.broker, "place_order_with_fill_info"):
                        try:
                            order_id, avg_fill, fill_state, _ = await self.broker.place_order_with_fill_info(
                                symbol=symbol,
                                side=side,
                                quantity=abs(qty),
                            )
                        except Exception:
                            order_id = None
                    if order_id is None:
                        resp = await self.broker.place_order(symbol=symbol, side=side, quantity=abs(qty))
                        if isinstance(resp, dict):
                            order_id = resp.get("orderNumber") or resp.get("orderId") or resp.get("order_id")
                            fill_state = resp.get("status")
                    order_proof.append(
                        {
                            "symbol": symbol,
                            "order_id": order_id,
                            "status": str(fill_state or "SUBMITTED").upper(),
                            "filled_qty": abs(qty),
                            "avg_fill_price": avg_fill,
                            "phase": "retry_close",
                            "attempt": attempt,
                        }
                    )
                await asyncio.sleep(retry_delay)

        unclosed_symbols = sorted(last_open.keys())
        last_error = "remaining_broker_positions_after_flatten"
        await self.state.update(
            trading_enabled=False,
            circuit_breaker_open=True,
            last_order_failed=True,
            last_risk_breach="emergency_flatten_unverified",
            manual_intervention_required=True,
            emergency_flatten_verified=False,
            emergency_flatten_attempts=attempts_used,
            emergency_flatten_unclosed_symbols=unclosed_symbols,
            emergency_flatten_last_error=last_error,
            emergency_flatten_order_proof=order_proof,
            active_trade={
                **trade,
                "manual_intervention_required": True,
                "emergency_flatten_verified": False,
                "remaining_broker_positions": last_open,
                "emergency_flatten_order_proof": order_proof,
            },
        )
        await self.event_bus.publish(
            "EMERGENCY_FLATTEN_UNVERIFIED",
            {
                "remaining_positions": last_open,
                "last_error": last_error,
                "order_proof": order_proof,
            },
        )
        return {
            "verified": False,
            "attempts": attempts_used,
            "before_positions": before,
            "remaining_positions": last_open,
            "last_error": last_error,
            "order_proof": order_proof,
        }

    async def _flatten_iron_condor_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        qty = to_int(trade.get("qty"), 0)
        symbol = str(
            trade.get("symbol")
            or trade.get("underlying")
            or settings.nifty_symbol
        ).strip()

        current_premium = to_float(
            trade.get("current_premium")
            or trade.get("exit_premium")
            or trade.get("entry_price"),
            to_float(trade.get("entry_price"), 0.0),
        )

        if settings.is_paper:
            closed_trade = self._build_paper_closed_iron_condor_trade(
                trade=trade,
                exit_premium=current_premium,
                reason="MANUAL_FLATTEN",
            )

            gross_pnl = to_float(closed_trade.get("gross_pnl"), 0.0)
            net_pnl = to_float(
                closed_trade.get("net_pnl") or closed_trade.get("pnl"),
                gross_pnl,
            )

            state = await self.state.snapshot()
            current_daily_pnl = to_float(getattr(state, "daily_pnl", 0.0), 0.0)
            new_daily_pnl = round(current_daily_pnl + net_pnl, 2)

            self.trade_store.append_trade(closed_trade, new_daily_pnl)

            today = now_ist().date().isoformat()

            await self.state.update(
                active_trade=None,
                live_pnl=0.0,
                daily_pnl=new_daily_pnl,
                last_trade_date=today,
                last_ic_trade_date=today,
                iron_condor_trade_date=today,
                last_iron_condor_month=now_ist().month,
            )

            self._last_manual_flatten_time = wall_time.time()
            self._last_signal_time = now_ist().timestamp()

            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed_trade})

            logger.info(
                "Manual IC flatten simulated in PAPER mode symbol=%s qty=%d premium=%.2f pnl=%.2f",
                symbol,
                qty,
                current_premium,
                net_pnl,
            )

            return {
                "status": "flattened",
                "symbol": symbol,
                "qty": qty,
                "exit_premium": round(current_premium, 2),
                "pnl": round(net_pnl, 2),
                "order_id": f"PAPER-FLATTEN-IC-{int(wall_time.time())}",
                "emergency_flatten_verified": True,
            }

        if not self.engine:
            return {"status": "error", "message": "trading_engine_unavailable"}

        closed_trade = await self.engine._exit_iron_condor_trade(
            trade=trade,
            reason="MANUAL_FLATTEN",
            current_premium=current_premium,
        )
        verification_trade = closed_trade if isinstance(closed_trade, dict) else trade
        verification = await self._verify_iron_condor_flatten(verification_trade)

        self._last_manual_flatten_time = wall_time.time()
        self._last_signal_time = now_ist().timestamp()

        return {
            "status": "flattened" if verification["verified"] else "manual_intervention_required",
            "symbol": symbol,
            "qty": qty,
            "exit_premium": round(current_premium, 2),
            "emergency_flatten_verified": bool(verification["verified"]),
            "emergency_flatten_attempts": int(verification["attempts"]),
            "remaining_positions": verification["remaining_positions"],
            "emergency_flatten_last_error": verification.get("last_error"),
            "emergency_flatten_order_proof": verification.get("order_proof", []),
        }

    def _build_paper_closed_iron_condor_trade(
        self,
        trade: dict[str, Any],
        exit_premium: float,
        reason: str,
    ) -> dict[str, Any]:
        qty = to_int(trade.get("qty"), 0)
        entry_premium = to_float(trade.get("entry_price"), 0.0)

        if self.engine and self.engine.iron_condor_strategy:
            pnl = self.engine.iron_condor_strategy.compute_pnl(
                entry_premium,
                exit_premium,
                qty,
            )

            gross_pnl = round(to_float(pnl.get("gross_pnl"), 0.0), 2)
            total_charges = round(to_float(pnl.get("total_charges"), 0.0), 2)
            net_pnl = round(to_float(pnl.get("net_pnl"), gross_pnl - total_charges), 2)

            charges = {
                "brokerage": round(to_float(pnl.get("platform_charges"), 0.0), 2),
                "stt": round(to_float(pnl.get("stt"), 0.0), 2),
                "exchange_txn": round(to_float(pnl.get("exchange_txn"), 0.0), 2),
                "sebi": round(to_float(pnl.get("sebi"), 0.0), 2),
                "gst": round(to_float(pnl.get("gst"), 0.0), 2),
                "stamp_duty": round(to_float(pnl.get("stamp_duty"), 0.0), 2),
                "total_charges": total_charges,
            }
        else:
            gross_pnl = round((entry_premium - exit_premium) * qty, 2)
            total_charges = 0.0
            net_pnl = gross_pnl
            charges = {"total_charges": total_charges}

        now_utc = datetime.now(UTC).isoformat()

        return {
            **trade,
            "strategy": "IRON_CONDOR",
            "signal": "IRON_CONDOR",
            "symbol": trade.get("symbol") or settings.nifty_symbol,
            "underlying": trade.get("underlying") or settings.nifty_symbol,
            "status": "CLOSED",
            "exit_time": now_utc,
            "exit_reason": reason,
            "reason": reason,
            "exit_price": round(exit_premium, 2),
            "exit_premium": round(exit_premium, 2),
            "gross_pnl": gross_pnl,
            "pnl": net_pnl,
            "net_pnl": net_pnl,
            "charges": charges,
            "total_charges": total_charges,
            "brokerage": charges.get("brokerage", 0.0),
            "stt": charges.get("stt", 0.0),
            "exchange_fee": charges.get("exchange_txn", 0.0),
            "gst": charges.get("gst", 0.0),
            "stamp_duty": charges.get("stamp_duty", 0.0),
            "trade_type": "IRON_CONDOR",
            "pricing_source": (
                trade.get("current_pricing_source")
                or trade.get("pricing_source")
                or "broker_quote_snapshot"
            ),
            "sell_order_id": f"PAPER-FLATTEN-IC-{int(wall_time.time())}",
        }

    async def _flatten_directional_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        symbol = trade.get("symbol")

        qty = (
            trade.get("t2_qty", to_int(trade.get("qty"), 0) // 2)
            if trade.get("t1_booked")
            else trade.get("qty", 0)
        )
        qty = to_int(qty, 0)

        if not symbol or qty <= 0:
            await self.state.update(active_trade=None, live_pnl=0.0)
            self._last_manual_flatten_time = wall_time.time()
            return {"status": "error", "message": "invalid_active_trade"}

        if settings.is_paper:
            await self.state.update(active_trade=None, live_pnl=0.0)

            self._last_manual_flatten_time = wall_time.time()
            self._last_signal_time = now_ist().timestamp()

            logger.info("Manual flatten simulated in PAPER mode symbol=%s qty=%d", symbol, qty)

            return {
                "status": "flattened",
                "symbol": symbol,
                "qty": qty,
                "order_id": f"PAPER-FLATTEN-{symbol}",
            }

        try:
            order_id, _ = await self.broker.place_order_and_wait_fill(
                symbol=symbol,
                side="SELL",
                quantity=qty,
            )

            if not order_id:
                return {"status": "error", "message": "no_order_id"}

            await self.state.update(active_trade=None, live_pnl=0.0)

            self._last_manual_flatten_time = wall_time.time()
            self._last_signal_time = now_ist().timestamp()

            logger.info("Manual flatten %s qty=%d order=%s", symbol, qty, order_id)

            return {
                "status": "flattened",
                "symbol": symbol,
                "qty": qty,
                "order_id": order_id,
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}


scheduler = MarketScheduler()

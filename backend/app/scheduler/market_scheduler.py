from __future__ import annotations

import asyncio
import time as _time
from collections import deque
from datetime import datetime, time
from zoneinfo import ZoneInfo

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.reconciliation import ReconciliationEngine
from backend.app.engine.state_manager import state_manager
from backend.app.engine.trading_engine import TradingEngine
from backend.app.risk.risk_manager import RiskManager
from backend.app.storage.trade_store import TradeStore
from backend.app.utils.logger import get_logger

settings = get_settings()
logger = get_logger("market_scheduler")

_MARKET_OPEN = time(9, 15)
_MARKET_CLOSE = time(15, 30)
_LOG_INTERVAL = 60
IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _market_open() -> bool:
    now = _now_ist()
    if now.weekday() >= 5:
        return False
    return _MARKET_OPEN <= now.time() <= _MARKET_CLOSE


class MarketScheduler:
    def __init__(self):
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

        self.running = False
        self._tasks: list[asyncio.Task] = []

        self._last_signal_time = 0.0
        self._last_closed_log = 0.0
        self._daily_reset_date = ""
        self._last_tick_time = _time.time()
        self._last_good_quote_time = _time.time()
        self._consecutive_quote_failures = 0
        self._last_broker_error = 0.0

        self._latest_iv: float | None = None
        self._iv_history: deque = deque(maxlen=20)

    def _track_task(self, task: asyncio.Task, name: str) -> asyncio.Task:
        def _done_callback(done_task: asyncio.Task) -> None:
            try:
                exc = done_task.exception()
                if exc:
                    logger.error("Task crashed: %s err=%s", name, exc, exc_info=True)
                else:
                    logger.warning("Task finished unexpectedly: %s", name)
            except asyncio.CancelledError:
                logger.info("Task cancelled: %s", name)
            except Exception as callback_exc:
                logger.error("Task callback failed for %s: %s", name, callback_exc, exc_info=True)

        task.add_done_callback(_done_callback)
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
            "last_ic_month=%s last_trade_date=%s",
            state.spot_price,
            state.trading_enabled,
            bool(state.active_trade),
            getattr(state, "last_iron_condor_month", None),
            getattr(state, "last_trade_date", None),
        )

        await self.event_bus.start()

        try:
            await self.broker.login()
        except Exception as exc:
            logger.error("SAMCO login failed in scheduler.start(): %s", exc, exc_info=True)
            raise

        self.running = True
        await self.state.update(bot_running=True)

        startup_reconcile = asyncio.create_task(
            self._reconciler.run_once(),
            name="reconcile-startup",
        )
        self._track_task(startup_reconcile, "reconcile-startup")

        self._tasks = [
            self._track_task(asyncio.create_task(self._loop(), name="market-loop"), "market-loop"),
            self._track_task(asyncio.create_task(self.risk.run(), name="risk-manager"), "risk-manager"),
            self._track_task(asyncio.create_task(self.engine.run(), name="trading-engine"), "trading-engine"),
            self._track_task(asyncio.create_task(self._daily_watcher(), name="daily-reset"), "daily-reset"),
            self._track_task(asyncio.create_task(self._reconciler.run_loop(300), name="reconciler"), "reconciler"),
        ]

        logger.info("All scheduler tasks started (%d tasks)", len(self._tasks))

    async def stop(self) -> None:
        if not self.running:
            return

        logger.info("Stopping Lords Bot scheduler")
        self.running = False
        await self.event_bus.stop()

        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._tasks.clear()
        await self.state.update(bot_running=False)
        logger.info("Scheduler stopped")

    async def _daily_watcher(self) -> None:
        logger.info("DAILY WATCHER TASK STARTED")
        while self.running:
            now = _now_ist()
            today = now.date().isoformat()

            if (
                now.time() >= time(9, 14)
                and now.time() < time(9, 15)
                and self._daily_reset_date != today
            ):
                self._daily_reset_date = today
                logger.info("=== DAILY RESET ===")

                await self.state.daily_reset()
                self.trade_store.daily_reset()
                self.engine.clear_cache()

                self._last_signal_time = 0.0
                self._latest_iv = None
                self._iv_history.clear()

                logger.info("=== DAILY RESET COMPLETE ===")

            await asyncio.sleep(10)

    async def _loop(self) -> None:
        logger.info("MARKET LOOP TASK STARTED")
        while self.running:
            try:
                if _market_open():
                    delay = _time.time() - self._last_tick_time
                    if delay > 10:
                        logger.error("Scheduler stalled! delay=%.2fs", delay)

                    data_stale = _time.time() - self._last_good_quote_time
                    if data_stale > settings.deadman_timeout:
                        logger.critical(
                            "Dead-man switch: market data stale for %.1fs",
                            data_stale,
                        )
                        await self._fail_safe_on_data_loss()
                else:
                    self._last_tick_time = _time.time()
                    self._last_good_quote_time = _time.time()
                    self._consecutive_quote_failures = 0

                if not _market_open():
                    now_ts = _time.time()
                    if now_ts - self._last_closed_log >= _LOG_INTERVAL:
                        now = _now_ist()
                        reason = "weekend" if now.weekday() >= 5 else "outside market hours"
                        logger.info("Market closed (%s) — polling paused", reason)
                        self._last_closed_log = now_ts
                else:
                    await self._tick()

            except Exception as exc:
                logger.error("Market loop error: %s", exc, exc_info=True)

            await asyncio.sleep(settings.poll_seconds)

    async def _tick(self) -> None:
        logger.info("TICK FUNCTION ENTERED")
        self._last_tick_time = _time.time()

        try:
            index_quote = await asyncio.wait_for(
                self.broker.get_index_quote(settings.nifty_symbol),
                timeout=3,
            )
            self._last_good_quote_time = _time.time()
            self._consecutive_quote_failures = 0
        except asyncio.TimeoutError:
            logger.warning("Broker timeout")
            self._consecutive_quote_failures += 1
            return
        except Exception as exc:
            self._consecutive_quote_failures += 1
            now_ts = _time.time()
            if now_ts - self._last_broker_error >= _LOG_INTERVAL:
                logger.warning("Broker quote unavailable: %s", exc, exc_info=True)
                self._last_broker_error = now_ts
            return

        spot = SamcoClient.parse_spot(index_quote)
        if spot is None:
            logger.warning("Spot parsing failed")
            self._consecutive_quote_failures += 1
            return

        logger.info("TICK: spot=%.2f", spot)

        iv = self._extract_iv(index_quote)
        if iv is not None:
            self._latest_iv = iv
            self._iv_history.append(iv)

        try:
            await self.state.update(spot_price=spot)
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
        now = _now_ist()

        if not self._iron_condor_can_enter(now, spot, state):
            return

        payload = {
            "signal": "IRON_CONDOR",
            "spot_price": spot,
            "size_label": "FULL",
            "trend_score": 0,
        }

        await self.state.update(signal="IRON_CONDOR", signal_meta=payload)
        await self.event_bus.publish("SIGNAL", payload)
        self._last_signal_time = now.timestamp()

        logger.info("IRON_CONDOR entry signal emitted spot=%.2f", spot)

    def _iron_condor_can_enter(self, now: datetime, spot: float, state) -> bool:
        if state.active_trade:
            logger.info("IC gate blocked: active trade already open")
            return False

        if now.weekday() >= 5:
            logger.info("IC gate blocked: weekend")
            return False

        if not state.trading_enabled:
            logger.info("IC gate blocked: trading_enabled=False")
            return False

        monthly_only = bool(getattr(settings, "ic_monthly_only", False))

        if monthly_only:
            start_day = int(getattr(settings, "ic_entry_day_start", 1))
            end_day = int(getattr(settings, "ic_entry_day_end", 5))

            if not (start_day <= now.day <= end_day):
                logger.info(
                    "IC gate blocked: monthly mode day filter start=%d end=%d today=%d",
                    start_day,
                    end_day,
                    now.day,
                )
                return False

            if getattr(state, "last_iron_condor_month", None) == now.month:
                logger.info("IC gate blocked: already traded this month month=%d", now.month)
                return False
        else:
            today = now.date().isoformat()
            for value in (
                getattr(state, "last_trade_date", None),
                getattr(state, "last_ic_trade_date", None),
                getattr(state, "iron_condor_trade_date", None),
            ):
                if value and str(value)[:10] == today:
                    logger.info("IC gate blocked: already traded today value=%s", value)
                    return False

        try:
            sh, sm = map(int, str(settings.ic_entry_window_start).split(":"))
            eh, em = map(int, str(settings.ic_entry_window_end).split(":"))
        except Exception:
            sh, sm, eh, em = 10, 0, 10, 5
            logger.warning("Failed to parse IC entry window — using defaults 10:00-10:05")

        if not (time(sh, sm) <= now.time() < time(eh, em)):
            logger.info(
                "IC gate blocked: outside time window %02d:%02d-%02d:%02d now=%s",
                sh,
                sm,
                eh,
                em,
                now.strftime("%H:%M:%S"),
            )
            return False

        if self._last_signal_time and now.timestamp() - self._last_signal_time < 60:
            logger.info(
                "IC gate blocked: cooldown active last_signal_age=%.0fs",
                now.timestamp() - self._last_signal_time,
            )
            return False

        logger.info(
            "IC gate passed: monthly_only=%s time=%s spot=%.2f",
            monthly_only,
            now.strftime("%H:%M:%S"),
            spot,
        )
        return True

    def _extract_iv(self, quote: dict) -> float | None:
        if not isinstance(quote, dict):
            return None

        for key in ("impliedVolatility", "iv", "implied_volatility", "volatility"):
            raw = quote.get(key)
            if raw is not None:
                try:
                    iv = float(raw)
                    if iv > 0:
                        return iv
                except (TypeError, ValueError):
                    continue
        return None

    async def _fail_safe_on_data_loss(self) -> None:
        state = await self.state.snapshot()
        if state.trading_enabled:
            await self.state.update(trading_enabled=False)
            logger.critical("Trading disabled due to stale quote stream")

    async def flatten_position(self) -> dict:
        state = await self.state.snapshot()
        if not state.active_trade:
            return {"status": "no_active_trade"}

        trade = state.active_trade
        symbol = trade.get("symbol")
        qty = (
            trade.get("t2_qty", trade.get("qty", 0) // 2)
            if trade.get("t1_booked")
            else trade.get("qty", 0)
        )

        if str(getattr(settings, "mode", "paper")).strip().lower() == "paper":
            await self.state.update(active_trade=None, live_pnl=0.0)
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
"""
Lords Bot — Market Scheduler  v6.0 (Iron Condor Only)
======================================================

All ORB strategy code has been removed. This scheduler runs only the
Iron Condor strategy (strategy_type=iron_condor in settings/.env).

What was removed:
  - ORB build/freeze logic (9:15-9:30 window)
  - 3-component trend filter and score
  - Breakout candle confirmation
  - today_open / orb_open / orb_close / prev_day_close state
  - Volume spike filter (ORB-specific)
  - Skip-first-candle logic
  - Candle builder (not needed for IC — IC monitors by spot/premium only)
  - _compute_atr, _trend_score, _update_candle
  - _volume_spike_ok, _iv_ok, _iv_percentile (ORB-only filters)
  - Auto-retrigger (ORB-specific)

What was kept:
  - All lifecycle (start / stop / _loop / _daily_watcher)
  - SAMCO tick polling and spot price updates
  - Dead-man switch
  - Reconciliation engine wiring
  - flatten_position (emergency dashboard button)
  - _iron_condor_can_enter (complete entry gate)
  - _extract_iv (for future IV-based IC filters)
  - IV history tracking

To switch back to ORB: restore market_scheduler_ORB_v5.3.py from backup
and set STRATEGY_TYPE=orb in .env.
"""
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
logger   = get_logger("market_scheduler")

_MARKET_OPEN  = time(9, 15)
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
        self.state       = state_manager
        self.trade_store = TradeStore()
        self.broker      = SamcoClient()
        self.event_bus   = EventBus()
        self.risk        = RiskManager(self.event_bus, self.state, self.broker)
        self.engine      = TradingEngine(
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

        # Timing
        self._last_signal_time           = 0.0
        self._last_closed_log            = 0.0
        self._daily_reset_date           = ""
        self._last_tick_time             = _time.time()
        self._last_good_quote_time       = _time.time()
        self._consecutive_quote_failures = 0
        self._last_broker_error          = 0.0

        # IV tracking (available for future IC entry filters)
        self._latest_iv: float | None = None
        self._iv_history: deque = deque(maxlen=20)

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        if self.running:
            logger.warning("Scheduler already running")
            return

        logger.info(
            "Starting Lords Bot v6.0 (Iron Condor) — mode=%s strategy=%s",
            settings.mode.upper(),
            settings.strategy_type.upper(),
        )

        if settings.strategy_type != "iron_condor":
            logger.critical(
                "🚨 strategy_type=%s but this scheduler is Iron Condor only. "
                "Set STRATEGY_TYPE=iron_condor in .env to proceed.",
                settings.strategy_type,
            )

        await self.state.load()
        state = await self.state.snapshot()
        logger.info(
            "State check: spot_price=%s trading_enabled=%s "
            "active_trade=%s last_ic_month=%s",
            state.spot_price, state.trading_enabled,
            bool(state.active_trade), state.last_iron_condor_month,
        )

        await self.event_bus.start()

        try:
            await self.broker.login()
        except Exception as exc:
            logger.error("SAMCO login failed: %s — running offline", exc)

        self.running = True
        await self.state.update(bot_running=True)

        asyncio.create_task(
            self._reconciler.run_once(), name="reconcile-startup")

        self._tasks = [
            asyncio.create_task(self._loop(),                   name="market-loop"),
            asyncio.create_task(self.risk.run(),                name="risk-manager"),
            asyncio.create_task(self.engine.run(),              name="trading-engine"),
            asyncio.create_task(self._daily_watcher(),          name="daily-reset"),
            asyncio.create_task(self._reconciler.run_loop(300), name="reconciler"),
        ]
        logger.info("All tasks started (%d tasks)", len(self._tasks))

    async def stop(self) -> None:
        if not self.running:
            return
        logger.info("Stopping Lords Bot scheduler")
        self.running = False
        await self.event_bus.stop()
        for t in self._tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        self._tasks.clear()
        await self.state.update(bot_running=False)
        logger.info("Scheduler stopped")

    # ── Daily reset watcher ───────────────────────────────────

    async def _daily_watcher(self) -> None:
        while self.running:
            now   = _now_ist()
            today = now.date().isoformat()

            if (now.time() >= time(9, 14) and
                    now.time() <  time(9, 15) and
                    self._daily_reset_date != today):

                self._daily_reset_date = today
                logger.info("=== DAILY RESET ===")

                await self.state.daily_reset()
                self.trade_store.daily_reset()
                self.engine.clear_cache()

                self._last_signal_time = 0.0
                self._latest_iv        = None
                self._iv_history.clear()

                logger.info("=== DAILY RESET COMPLETE ===")

            await asyncio.sleep(10)

    # ── Main poll loop ────────────────────────────────────────

    async def _loop(self) -> None:
        logger.info("🔄 Market loop started")
        while self.running:
            try:
                if _market_open():
                    delay = _time.time() - self._last_tick_time
                    if delay > 10:
                        logger.error("Scheduler stalled! delay=%.2fs", delay)
                    data_stale = _time.time() - self._last_good_quote_time
                    if data_stale > settings.deadman_timeout:
                        logger.critical(
                            "Dead-man switch: market data stale for %.1fs", data_stale)
                        await self._fail_safe_on_data_loss()
                else:
                    # Outside market hours — reset timers so watchdog doesn't fire at open
                    self._last_tick_time       = _time.time()
                    self._last_good_quote_time = _time.time()
                    self._consecutive_quote_failures = 0

                if not _market_open():
                    now_ts = _time.time()
                    if now_ts - self._last_closed_log >= _LOG_INTERVAL:
                        now    = _now_ist()
                        reason = ("weekend" if now.weekday() >= 5
                                  else "outside market hours")
                        logger.info("Market closed (%s) — polling paused", reason)
                        self._last_closed_log = now_ts
                else:
                    await self._tick()

            except Exception as exc:
                logger.error("Market loop error: %s", exc, exc_info=True)

            await asyncio.sleep(settings.poll_seconds)

    # ── Tick ──────────────────────────────────────────────────

    async def _tick(self) -> None:
        self._last_tick_time = _time.time()

        # ── Fetch NIFTY spot ────────────────────────────────
        try:
            index_quote = await asyncio.wait_for(
                self.broker.get_index_quote(settings.nifty_symbol),
                timeout=3,
            )
            self._last_good_quote_time       = _time.time()
            self._consecutive_quote_failures = 0
        except asyncio.TimeoutError:
            logger.warning("⏰ Broker timeout")
            self._consecutive_quote_failures += 1
            return
        except RuntimeError as exc:
            self._consecutive_quote_failures += 1
            now_ts = _time.time()
            if now_ts - self._last_broker_error >= _LOG_INTERVAL:
                logger.warning("❌ Broker quote unavailable: %s", exc)
                self._last_broker_error = now_ts
            return

        spot = SamcoClient.parse_spot(index_quote)
        if spot is None:
            logger.warning("❌ Spot parsing failed")
            self._consecutive_quote_failures += 1
            return

        logger.info("💰 TICK: spot=%.2f", spot)

        # Extract IV if the broker provides it
        iv = self._extract_iv(index_quote)
        if iv is not None:
            self._latest_iv = iv
            self._iv_history.append(iv)

        # ── Update spot in state ────────────────────────────
        # IC trade monitoring (P&L, exit checks) is handled entirely by
        # trading_engine._monitor_loop. Scheduler only needs to keep
        # spot_price current so the engine can read it.
        try:
            await self.state.update(spot_price=spot)
        except Exception as exc:
            logger.error("state.update spot_price failed: %s", exc)
            return

        logger.info("✅ TICK: spot_price=%.2f", spot)

        # ── Publish tick for any subscribers ────────────────
        await self.event_bus.publish("TICK", {
            "price":  spot,
            "volume": float(index_quote.get("volume") or 0),
            "iv":     float(iv or 0.0),
        })

        # ── IC entry gate ────────────────────────────────────
        state = await self.state.snapshot()
        now   = _now_ist()

        if not self._iron_condor_can_enter(now, spot, state):
            return

        payload = {
            "signal":      "IRON_CONDOR",
            "spot_price":  spot,
            "size_label":  "FULL",
            "trend_score": 0,
        }
        try:
            await self.state.update(signal="IRON_CONDOR", signal_meta=payload)
        except Exception as exc:
            logger.error("state.update signal failed: %s", exc)
            return

        await self.event_bus.publish("SIGNAL", payload)
        self._last_signal_time = now.timestamp()
        logger.info("✅ IRON_CONDOR entry signal emitted spot=%.2f", spot)

    # ── IC entry gate ─────────────────────────────────────────

    def _iron_condor_can_enter(self, now: datetime, spot: float, state) -> bool:
        """
        All conditions that must be true before emitting an IC entry signal.

        Returns True only when every gate passes.
        Logs the blocking reason at DEBUG so logs stay readable; only
        the final SIGNAL log is at INFO.
        """
        if state.active_trade:
            logger.debug("IC gate: active trade already open")
            return False

        if state.last_iron_condor_month == now.month:
            logger.debug("IC gate: already traded this month (month=%d)", now.month)
            return False

        if now.weekday() >= 5:
            logger.debug("IC gate: weekend")
            return False

        if not (settings.ic_entry_day_start <= now.day <= settings.ic_entry_day_end):
            logger.debug(
                "IC gate: not in entry days (%d-%d), today=day %d",
                settings.ic_entry_day_start, settings.ic_entry_day_end, now.day,
            )
            return False

        try:
            sh, sm = map(int, settings.ic_entry_window_start.split(":"))
            eh, em = map(int, settings.ic_entry_window_end.split(":"))
        except Exception:
            sh, sm, eh, em = 9, 20, 10, 0
            logger.warning("Failed to parse IC entry window — using defaults 09:20-10:00")

        if not (time(sh, sm) <= now.time() < time(eh, em)):
            logger.debug(
                "IC gate: outside time window %02d:%02d-%02d:%02d, now=%s",
                sh, sm, eh, em, now.strftime("%H:%M:%S"),
            )
            return False

        if not state.trading_enabled:
            logger.debug("IC gate: trading_enabled=False")
            return False

        # Signal cooldown — prevents rapid re-emission if SIGNAL not consumed yet
        if self._last_signal_time and now.timestamp() - self._last_signal_time < 60:
            logger.debug("IC gate: cooldown active (last signal %.0fs ago)",
                         now.timestamp() - self._last_signal_time)
            return False

        return True

    # ── Helpers ───────────────────────────────────────────────

    def _extract_iv(self, quote: dict) -> float | None:
        """Extract implied volatility from broker quote payload if present."""
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

    # ── Dead-man switch ───────────────────────────────────────

    async def _fail_safe_on_data_loss(self) -> None:
        """Disable trading and attempt emergency flatten when data stream dies."""
        state = await self.state.snapshot()
        if state.trading_enabled:
            await self.state.update(trading_enabled=False)
            logger.critical("Trading disabled due to stale quote stream")
        if state.active_trade:
            logger.critical("Emergency flatten triggered by dead-man switch")
            closed = False
            for attempt in range(1, 4):
                result = await self.flatten_position()
                closed = result.get("status") in {"flattened", "no_active_trade"}
                if closed:
                    logger.critical(
                        "Dead-man switch exit success attempt=%d", attempt)
                    break
                await asyncio.sleep(1.0)
            if not closed:
                logger.critical("Dead-man switch exit failed after retries")
            await self.state.update(
                trading_enabled=False,
                last_risk_breach="deadman_switch",
            )

    # ── Manual flatten ────────────────────────────────────────

    async def flatten_position(self) -> dict:
        """Emergency flatten — callable from dashboard FLATTEN button."""
        state = await self.state.snapshot()
        if not state.active_trade:
            return {"status": "no_active_trade"}
        trade  = state.active_trade
        symbol = trade.get("symbol")
        qty    = (
            trade.get("t2_qty", trade.get("qty", 0) // 2)
            if trade.get("t1_booked")
            else trade.get("qty", 0)
        )
        try:
            order_id, _ = await self.broker.place_order_and_wait_fill(
                symbol=symbol, side="SELL", quantity=qty,
            )
            if not order_id:
                return {"status": "error", "message": "no_order_id"}
            await self.state.update(active_trade=None, live_pnl=0.0)
            logger.info("Manual flatten %s qty=%d order=%s", symbol, qty, order_id)
            return {
                "status":   "flattened",
                "symbol":   symbol,
                "qty":      qty,
                "order_id": order_id,
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}


# ── Global instance ───────────────────────────────────────────
scheduler = MarketScheduler()
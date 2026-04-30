"""
Lords Bot — Market Scheduler  v5.3
====================================
v5.3 fixes (over v5.2, evidence from 182k log lines):
  1. _today_open captured ONLY during 09:15-09:17 window
     (was: captured on first tick after startup → wrong if bot starts mid-day)
  2. orb_close persisted on EVERY tick during ORB build window, not only at freeze
     (was: if v5.1 froze ORB before v5.2 deployed orb_close persist, value lost)
  3. Startup audit: if it's after 09:30 and trend vars missing, log CRITICAL
  4. Today_open daily reset now also persisted (was: in-memory only)

Full pipeline: poll → ORB build → candle confirm → trend filter → signal → trade.

v5.2 additions (over v5.1):
  1. Persist & restore trend variables across restarts:
       _orb_open, _orb_close, _today_open, _prev_day_close
     are now saved to state.* whenever set, and restored on startup.
     Fixes silent score=0 after mid-day restarts.

  2. _prev_day_close fallback: if bot starts after the 09:14 reset
     window, attempt to load from state on first tick instead of
     leaving it None for the entire day.

  3. Diagnostic logging in _trend_score: every score computation logs
     all four input variables AND each component contribution. Run for
     a week, then decide on threshold tuning from real data, not guesses.

v5.1 features (preserved):
  • Reconciler run_loop(300) added as background task
  • 3-component trend score (prev close, gap, ORB direction) — no lookahead
  • Skip first candle after ORB (65s buffer, avoids 09:30 fakeouts)
  • ORB max range guard (skip chaotic days >150pts)
  • _prev_day_close stored at daily reset
  • _orb_frozen_time tracks when ORB froze
  • ReconciliationEngine wired — startup + every 5 min
"""
from __future__ import annotations

import asyncio
import time as _time
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

        # ORB state
        self._orb_frozen        = False
        self._orb_frozen_time: float | None = None
        self._last_signal_time  = 0.0
        self._last_closed_log   = 0.0
        self._daily_reset_date  = ""
        self._last_tick_time = _time.time()
        self._last_good_quote_time = _time.time()
        self._consecutive_quote_failures = 0
        self._last_broker_error = 0.0

        # Candle builder
        self._current_minute: datetime | None = None
        self._candle_open:    float | None     = None
        self._candle_high:    float            = 0.0
        self._candle_low:     float            = 999_999.0
        self._candle_close:   float | None     = None

        # Trend state (v4.0) — now persisted to state for restart safety
        self._orb_open:       float | None = None
        self._orb_close:      float | None = None
        self._today_open:     float | None = None
        self._prev_day_close: float | None = None
        self._recent_spots:   list         = []
        self._day_candles:    list         = []

    # ── Lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        if self.running:
            logger.warning("Scheduler already running"); return

        logger.info(
            "Starting Lords Bot v5.3 — mode=%s trend_filter=%s skip_first=%s",
            settings.mode.upper(),
            settings.trend_filter_enabled,
            settings.skip_first_candle,
        )

        await self.state.load()

        # Check if ORB was already frozen from previous run
        state = await self.state.snapshot()
        logger.info(
            "State check: orb_high=%s orb_low=%s spot_price=%s trading_enabled=%s",
            state.orb_high, state.orb_low, state.spot_price, state.trading_enabled,
        )
        if state.orb_high is not None and state.orb_low is not None:
            self._orb_frozen = True
            self._orb_frozen_time = _time.time()  # Approximate
            logger.info(
                "ORB restored from state: high=%.2f low=%.2f",
                state.orb_high, state.orb_low,
            )
            # Re-enable trading since ORB was already established
            await self.state.update(trading_enabled=True)
            logger.info("Trading re-enabled after ORB restoration")
        else:
            logger.info("No ORB found in state, starting fresh")

        # ✅ NEW: Restore trend variables from state (fixes silent score=0 after restart)
        # If the state has these values (from prior session), use them. Otherwise leave None
        # and let the daily watcher / first tick populate them as normal.
        restored = []
        if getattr(state, "orb_open", None) is not None:
            self._orb_open = state.orb_open
            restored.append(f"orb_open={self._orb_open:.2f}")
        if getattr(state, "orb_close", None) is not None:
            self._orb_close = state.orb_close
            restored.append(f"orb_close={self._orb_close:.2f}")
        if getattr(state, "today_open", None) is not None:
            self._today_open = state.today_open
            restored.append(f"today_open={self._today_open:.2f}")
        if getattr(state, "prev_day_close", None) is not None:
            self._prev_day_close = state.prev_day_close
            restored.append(f"prev_day_close={self._prev_day_close:.2f}")

        if restored:
            logger.info("Trend vars restored from state: %s", ", ".join(restored))
        else:
            logger.info("No trend vars in state — will populate from market data")

        # ✅ v5.3: If bot started after 09:30, check for missing trend vars
        # Trend score will degrade if these are missing — log loudly so it's
        # explainable in audit.
        now_check = _now_ist()
        if now_check.time() > time(9, 30) and now_check.weekday() < 5:
            missing = []
            if self._prev_day_close is None: missing.append("prev_day_close")
            if self._today_open is None:     missing.append("today_open")
            if self._orb_open is None:       missing.append("orb_open")
            if self._orb_close is None:      missing.append("orb_close")
            if missing:
                logger.critical(
                    "🚨 STARTUP AFTER 09:30 BUT TREND VARS MISSING: %s — "
                    "trend score components depending on these will be 0. "
                    "Filter at ±3 will block all signals today.",
                    ", ".join(missing),
                )

        await self.event_bus.start()

        try:
            await self.broker.login()
        except Exception as exc:
            logger.error("SAMCO login failed: %s — running offline", exc)

        self.running = True
        await self.state.update(bot_running=True)

        # Startup reconciliation (runs once immediately in all modes)
        asyncio.create_task(
            self._reconciler.run_once(), name="reconcile-startup")

        self._tasks = [
            asyncio.create_task(self._loop(),
                                name="market-loop"),
            asyncio.create_task(self.risk.run(),
                                name="risk-manager"),
            asyncio.create_task(self.engine.run(),
                                name="trading-engine"),
            asyncio.create_task(self._daily_watcher(),
                                name="daily-reset"),
            asyncio.create_task(self._reconciler.run_loop(300),
                                name="reconciler"),
        ]
        logger.info("All tasks started (%d tasks)", len(self._tasks))

        # Auto-retrigger: publish after engine/risk listeners are running
        asyncio.create_task(self._delayed_retrigger(), name="auto-retrigger")

    async def _delayed_retrigger(self) -> None:
        await asyncio.sleep(0.25)
        state = await self.state.snapshot()
        if (self._orb_frozen and state.spot_price and
            state.orb_high is not None and state.spot_price > state.orb_high
            and state.trading_enabled):
            logger.info(
                "🔄 Auto-retrigger: price %.2f > ORB high %.2f, emitting LONG signal",
                state.spot_price, state.orb_high,
            )
            await self.event_bus.publish("RISK_APPROVED", {
                "signal": "LONG",
                "size_label": "FULL",
            })
        else:
            logger.info(
                "🚫 Auto-retrigger skipped: frozen=%s spot=%s orb_high=%s enabled=%s",
                self._orb_frozen, state.spot_price, state.orb_high,
                state.trading_enabled,
            )

    async def stop(self) -> None:
        if not self.running: return
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

    # ── Daily reset watcher ──────────────────────────────────

    async def _daily_watcher(self) -> None:
        while self.running:
            now   = _now_ist()
            today = now.date().isoformat()

            if (now.time() >= time(9, 14) and
                    now.time() <  time(9, 15) and
                    self._daily_reset_date != today):

                self._daily_reset_date = today
                logger.info("=== DAILY RESET ===")

                # Store prev day close BEFORE resetting state
                state = await self.state.snapshot()
                if state.spot_price and state.spot_price > 0:
                    self._prev_day_close = state.spot_price
                    # ✅ Persist to state so it survives restart
                    await self.state.update(prev_day_close=self._prev_day_close)
                    logger.info("Prev day close stored: %.2f", self._prev_day_close)

                await self.state.daily_reset()
                self.trade_store.daily_reset()
                self.engine.clear_cache()

                self._orb_frozen       = False
                self._orb_frozen_time  = None
                self._orb_open         = None
                self._orb_close        = None
                self._today_open       = None
                self._recent_spots     = []
                self._day_candles      = []
                self._current_minute   = None
                self._last_signal_time = 0.0

                # ✅ Clear persisted trend vars too (fresh day)
                await self.state.update(
                    orb_open=None,
                    orb_close=None,
                    today_open=None,
                    # Note: _prev_day_close was just SET above, don't clear it
                )

                logger.info("=== DAILY RESET COMPLETE ===")

            await asyncio.sleep(10)

    # ── Main poll loop ───────────────────────────────────────
    async def _loop(self) -> None:
        logger.info("🔄 Market loop started")
        while self.running:
            try:
                if _market_open():
                    delay = _time.time() - self._last_tick_time
                    if delay > 10:
                        logger.error("Scheduler stalled! delay=%.2fs", delay)
                    data_stale_seconds = _time.time() - self._last_good_quote_time
                    if data_stale_seconds > settings.deadman_timeout:
                        logger.critical(
                            "Dead-man switch: market data stale for %.1fs",
                            data_stale_seconds,
                        )
                        await self._fail_safe_on_data_loss()
                else:
                    self._last_tick_time = _time.time()
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
                    logger.info("📊 Market open — calling _tick")
                    await self._tick()

            except Exception as exc:
                logger.error("Market loop error: %s", exc, exc_info=True)

            await asyncio.sleep(settings.poll_seconds)

    # ── Tick ─────────────────────────────────────────────────

    async def _tick(self) -> None:
        self._last_tick_time = _time.time()
        logger.info("🕐 TICK: Starting market data fetch")
        try:
            index_quote = await asyncio.wait_for(
                self.broker.get_index_quote(settings.nifty_symbol),
                timeout=3,
            )
            self._last_good_quote_time = _time.time()
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

        state = await self.state.snapshot()

        if state.active_trade:
            symbol = state.active_trade.get("symbol")

            try:
                option_quote = await self.broker.get_quote(
                    symbol_name=symbol,
                    exchange="NFO",
                )

                option_ltp = self.broker.parse_ltp(option_quote)

                if option_ltp is not None and option_ltp > 0:
                    await self.state.update(
                        spot_price=spot,
                        active_trade={
                            **state.active_trade,
                            "ltp": option_ltp,
                        },
                    )
                else:
                    logger.warning("⚠️ LTP missing or zero for %s", symbol)
                    await self.state.update(spot_price=spot)

            except Exception as e:
                logger.warning("⚠️ Option quote failed: %s", e)
                await self.state.update(spot_price=spot)

        else:
            await self.state.update(spot_price=spot)

        logger.info("✅ TICK: State updated with spot_price=%.2f", spot)

        now   = _now_ist()

        # Capture today's open price — but ONLY during 09:15-09:17 window.
        # v52 bug: captured on first tick after startup, so a 10:30 restart
        # would set today_open=10:30_spot, breaking trend score Component 1.
        if self._today_open is None:
            if time(9, 15) <= now.time() <= time(9, 17):
                self._today_open = spot
                try:
                    await self.state.update(today_open=self._today_open)
                except Exception as exc:
                    logger.warning("Failed to persist today_open: %s", exc)
                logger.info("Today open captured: %.2f (persisted)", spot)
            elif now.time() > time(9, 17):
                # Bot started after 09:17 — too late to capture real open
                # Log once per session so degraded trend scores are explainable
                if not getattr(self, "_warned_today_open_missed", False):
                    logger.warning(
                        "Bot started after 09:17 (now=%s) — today_open cannot be "
                        "captured today. Trend score Component 1 will be 0.",
                        now.strftime("%H:%M:%S"),
                    )
                    self._warned_today_open_missed = True

        await self.event_bus.publish("TICK", {
            "price":  spot,
            "volume": float(index_quote.get("volume") or 0),
        })

        self._recent_spots.append(spot)
        if len(self._recent_spots) > 10:
            self._recent_spots.pop(0)

        completed_candle = self._update_candle(spot, now)
        if completed_candle:
            self._day_candles.append(completed_candle)
            if len(self._day_candles) > 60:
                self._day_candles.pop(0)

        market_open_today = now.replace(
            hour=9, minute=15, second=0, microsecond=0)
        seconds_since_open = (now - market_open_today).total_seconds()

        if seconds_since_open < 0:
            return  # pre-market

        in_orb = seconds_since_open < settings.orb_duration_seconds

        # ── Build ORB 9:15–9:30 ──────────────────────────
        if in_orb and not self._orb_frozen:
            if self._orb_open is None:
                self._orb_open = spot
                try:
                    await self.state.update(orb_open=self._orb_open)
                except Exception as exc:
                    logger.warning("Failed to persist orb_open: %s", exc)
                logger.info("ORB open captured: %.2f (persisted)", spot)
            high = spot if state.orb_high is None else max(state.orb_high, spot)
            low  = spot if state.orb_low  is None else min(state.orb_low,  spot)
            self._orb_close = spot  # rolling close

            # ✅ v5.3: Persist orb_close on EVERY tick during ORB build window.
            # v5.2 only persisted at freeze time, so if a v5.1 freeze ran without
            # persisting, the value was lost. Now: as long as bot is up during
            # 09:15-09:30 at least once, orb_close persists.
            try:
                await self.state.update(
                    orb_high=high,
                    orb_low=low,
                    orb_close=self._orb_close,
                )
            except Exception as exc:
                logger.warning("Failed to persist ORB build state: %s", exc)
            return

        # ── Freeze ORB at 9:30 ───────────────────────────
        if not in_orb and not self._orb_frozen:
            self._orb_frozen      = True
            self._orb_frozen_time = now.timestamp()
            atr_val               = self._compute_atr()

            # Persist final orb_close at freeze time (defensive — should already
            # be persisted from the last build tick, but belt + braces)
            if self._orb_close is not None:
                try:
                    await self.state.update(orb_close=self._orb_close)
                except Exception as exc:
                    logger.warning("Failed to persist final orb_close: %s", exc)
                logger.info("ORB close captured: %.2f (persisted)", self._orb_close)

            if state.orb_high is not None and state.orb_low is not None:
                orb_range = state.orb_high - state.orb_low

                if orb_range < settings.min_orb_range:
                    logger.warning(
                        "ORB range %.1f < min %.1f — trading disabled",
                        orb_range, settings.min_orb_range)
                    await self.state.update(trading_enabled=False)
                    return

                orb_max = getattr(settings, "orb_max_range", 150.0)
                if orb_range > orb_max:
                    logger.warning(
                        "ORB range %.1f > max %.1f (chaotic) — trading disabled",
                        orb_range, orb_max)
                    await self.state.update(trading_enabled=False)
                    return

                logger.info(
                    "ORB FROZEN high=%.2f low=%.2f range=%.2f ATR=%.2f",
                    state.orb_high, state.orb_low, orb_range, atr_val,
                )

                if orb_range < atr_val * settings.orb_atr_multiplier:
                    logger.warning(
                        "Choppy ORB range=%.2f < %.1f×ATR=%.2f — disabled",
                        orb_range, settings.orb_atr_multiplier, atr_val)
                    await self.state.update(trading_enabled=False)
            else:
                logger.warning(
                    "Bot started after ORB window — trading disabled today")
                await self.state.update(trading_enabled=False)

        # ── Post-ORB guards ───────────────────────────────
        if state.active_trade:        return
        if state.signal is not None:  return
        if not state.trading_enabled: return
        if state.orb_high is None or state.orb_low is None: return
        if state.orb_high - state.orb_low < settings.min_orb_range: return

        no_h, no_m = map(int, settings.no_entry_after.split(":"))
        if now.time() >= time(no_h, no_m): return

        now_ts = now.timestamp()
        if now_ts - self._last_signal_time < settings.signal_cooldown: return

        # ── Candle-close confirmation ─────────────────────
        if completed_candle is None: return

        # ── Skip first candle after ORB freeze ───────────
        if settings.skip_first_candle and self._orb_frozen_time is not None:
            if now.timestamp() - self._orb_frozen_time < 65:
                return

        bu = state.orb_high + settings.breakout_buffer
        bd = state.orb_low  - settings.breakout_buffer

        candle_above = completed_candle["close"] > bu
        candle_below = completed_candle["close"] < bd
        if not candle_above and not candle_below: return

        # ── 3-component trend score ───────────────────────
        ts = self._trend_score(state.orb_high, state.orb_low)

        if candle_above:
            signal_type = "CALL"
            if settings.trend_filter_enabled:
                if ts < 3:
                    logger.info(
                        "TREND FILTER: CALL skipped score=%+d (need +3)", ts)
                    return
            elif ts < 0:
                return
        else:
            signal_type = "PUT"
            if settings.trend_filter_enabled:
                if ts > -3:
                    logger.info(
                        "TREND FILTER: PUT skipped score=%+d (need -3)", ts)
                    return
            elif ts > 0:
                return

        total_min = now.hour * 60 + now.minute
        if   total_min <= 10 * 60 + 30: size = "FULL"
        elif total_min <= 12 * 60:      size = "MEDIUM"
        elif total_min <= 13 * 60 + 30: size = "HALF"
        else: return

        logger.info(
            "SIGNAL %s close=%.2f trend=%+d size=%s orb=%.2f/%.2f",
            signal_type, completed_candle["close"], ts, size,
            state.orb_high, state.orb_low,
        )

        payload = {
            "signal":      signal_type,
            "spot_price":  completed_candle["close"],
            "size_label":  size,
            "trend_score": ts,
        }
        await self.state.update(signal=signal_type, signal_meta=payload)
        await self.event_bus.publish("SIGNAL", payload)
        self._last_signal_time = now_ts

    # ── Candle builder ───────────────────────────────────────

    def _update_candle(self, spot: float, now: datetime) -> dict | None:
        minute_ts = now.replace(second=0, microsecond=0)
        if self._current_minute is None:
            self._current_minute = minute_ts
            self._candle_open = self._candle_high = self._candle_low = self._candle_close = spot
            return None
        if minute_ts > self._current_minute:
            completed = {
                "ts":    self._current_minute,
                "open":  self._candle_open,
                "high":  self._candle_high,
                "low":   self._candle_low,
                "close": self._candle_close,
            }
            self._current_minute = minute_ts
            self._candle_open = self._candle_high = self._candle_low = self._candle_close = spot
            return completed
        self._candle_high  = max(self._candle_high, spot)
        self._candle_low   = min(self._candle_low,  spot)
        self._candle_close = spot
        return None

    def _compute_atr(self) -> float:
        if len(self._day_candles) < 3:
            return 15.0
        return (
            sum(c["high"] - c["low"] for c in self._day_candles)
            / len(self._day_candles)
        )

    def _trend_score(self, orb_high: float, orb_low: float) -> int:
        """
        3-component trend score. Range: -3 to +3.
        All inputs are knowable at signal time — zero lookahead.

        Component 1 — Gap direction:
            +1 today opened ABOVE prev day close  (gap up = bullish continuation)
            -1 today opened BELOW prev day close  (gap down = bearish continuation)

        Component 2 — ORB candle direction:
            +1 ORB closed ABOVE ORB open  (9:15-9:30 was bullish)
            -1 ORB closed BELOW ORB open  (9:15-9:30 was bearish)

        Component 3 — Price vs prev close:
            +1 ORB close ABOVE prev day close  (market holding above yesterday)
            -1 ORB close BELOW prev day close  (market holding below yesterday)

        Signal rules (TREND_FILTER_ENABLED=true):
            CALL only if score == +3  (all three bullish)
            PUT  only if score == -3  (all three bearish)
        """
        score = 0
        c1 = c2 = c3 = 0  # individual component values for diagnostic logging

        # Component 1: gap direction
        if self._today_open is not None and self._prev_day_close is not None:
            if self._today_open > self._prev_day_close:
                c1 = +1
            elif self._today_open < self._prev_day_close:
                c1 = -1
        score += c1

        # Component 2: ORB candle direction
        if self._orb_open is not None and self._orb_close is not None:
            if self._orb_close > self._orb_open:
                c2 = +1
            elif self._orb_close < self._orb_open:
                c2 = -1
        score += c2

        # Component 3: ORB close vs prev day close
        if self._orb_close is not None and self._prev_day_close is not None:
            if self._orb_close > self._prev_day_close:
                c3 = +1
            elif self._orb_close < self._prev_day_close:
                c3 = -1
        score += c3

        # ✅ DIAGNOSTIC LOG — every score computation logs all inputs + components
        # Use this for a week, then decide if threshold needs tuning.
        # Watch for "MISSING" markers — those mean a component is silently
        # contributing 0 because its input variables are None.
        logger.info(
            "TREND DEBUG | today_open=%s prev_close=%s orb_open=%s orb_close=%s "
            "| C1(gap)=%+d C2(orb)=%+d C3(close)=%+d | TOTAL=%+d",
            f"{self._today_open:.2f}" if self._today_open is not None else "MISSING",
            f"{self._prev_day_close:.2f}" if self._prev_day_close is not None else "MISSING",
            f"{self._orb_open:.2f}" if self._orb_open is not None else "MISSING",
            f"{self._orb_close:.2f}" if self._orb_close is not None else "MISSING",
            c1, c2, c3, score,
        )

        return score

    async def _fail_safe_on_data_loss(self) -> None:
        """Dead-man switch: retry exit and hard-disable trading."""
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
                    logger.critical("Dead-man switch exit success attempt=%d", attempt)
                    break
                await asyncio.sleep(1.0)
            if not closed:
                logger.critical("Dead-man switch exit failed after retries")
            await self.state.update(trading_enabled=False, last_risk_breach="deadman_switch")

    # ── Manual flatten ───────────────────────────────────────

    async def flatten_position(self) -> dict:
        """Emergency flatten — called from dashboard FLATTEN button."""
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
            return {"status": "flattened", "symbol": symbol, "qty": qty, "order_id": order_id}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}


scheduler = MarketScheduler()
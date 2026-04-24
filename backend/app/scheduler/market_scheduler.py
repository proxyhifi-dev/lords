"""
Lords Bot — Market Scheduler  v4.0
===================================
Full pipeline: poll → ORB build → candle confirm → trend filter → signal → trade.

v4.0 improvements:
  1. 3-component trend score (prev close, gap, ORB direction) — no lookahead
  2. Skip first candle after ORB (09:30 has 43% WR, 09:31+ has 63%)
  3. ORB max range guard (skip chaotic days >150pts)
  4. _prev_day_close stored at daily reset
  5. _orb_frozen_time tracks when 09:30 fired
"""
from __future__ import annotations
import asyncio
import time as _time
from datetime import datetime, time
from zoneinfo import ZoneInfo

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import state_manager
from backend.app.engine.trading_engine import TradingEngine
from backend.app.risk.risk_manager import RiskManager
from backend.app.storage.trade_store import TradeStore
from backend.app.utils.logger import get_logger
from backend.app.engine.reconciliation import ReconciliationEngine

settings = get_settings()
logger   = get_logger("market_scheduler")

_MARKET_OPEN  = time(9, 0)
_MARKET_CLOSE = time(15, 35)
_LOG_INTERVAL = 60
IST = ZoneInfo("Asia/Kolkata")

def _now_ist(): return datetime.now(IST)
def _market_open():
    now = _now_ist()
    if now.weekday() >= 5: return False
    return _MARKET_OPEN <= now.time() <= _MARKET_CLOSE


class MarketScheduler:

    def __init__(self):
        self.state       = state_manager
        self.trade_store = TradeStore()
        self.broker      = SamcoClient()
        self.event_bus   = EventBus()
        self.risk        = RiskManager(self.event_bus, self.state)
        self.engine      = TradingEngine(
            event_bus=self.event_bus,
            state_manager=self.state,
            trade_store=self.trade_store,
            broker=self.broker,
        )
        self.running = False
        self._tasks: list[asyncio.Task] = []

        # ORB state
        self._orb_frozen        = False
        self._orb_frozen_time: float | None = None
        self._last_signal_time  = 0.0
        self._last_closed_log   = 0.0
        self._daily_reset_date  = ""
        self._last_broker_error = 0.0

        # Candle builder
        self._current_minute: datetime | None = None
        self._candle_open: float | None  = None
        self._candle_high: float         = 0.0
        self._candle_low:  float         = 999_999.0
        self._candle_close: float | None = None

        # Trend state (v4.0)
        self._orb_open:       float | None = None
        self._orb_close:      float | None = None
        self._today_open:     float | None = None
        self._prev_day_close: float | None = None
        self._recent_spots:   list         = []
        self._day_candles:    list         = []
        self._reconciler = ReconciliationEngine(broker=self.broker, state_manager=self.state, event_bus=self.event_bus)

    # ── Lifecycle ──────────────────────────────────────
    async def start(self):
        if self.running: logger.warning("Scheduler already running"); return
        logger.info("Starting Lords Bot v4.0 — mode=%s", settings.mode.upper())
        await self.state.load()
        await self.event_bus.start()
        try:
            await self.broker.login()
        except Exception as exc:
            logger.error("SAMCO login failed: %s — running offline", exc)
        self.running = True
        await self.state.update(bot_running=True)
        if settings.is_live:
            asyncio.create_task(self._reconciler.run_once())
        self._tasks = [
            asyncio.create_task(self._loop(),          name="market-loop"),
            asyncio.create_task(self.risk.run(),       name="risk-manager"),
            asyncio.create_task(self.engine.run(),     name="trading-engine"),
            asyncio.create_task(self._daily_watcher(), name="daily-reset"),
        ]
        logger.info("All tasks started — trend_filter=%s skip_first_candle=%s",
                    settings.trend_filter_enabled, settings.skip_first_candle)

    async def stop(self):
        if not self.running: return
        logger.info("Stopping Lords Bot scheduler")
        self.running = False
        await self.event_bus.stop()
        for t in self._tasks:
            if not t.done():
                t.cancel()
                try: await t
                except asyncio.CancelledError: pass
        self._tasks.clear()
        await self.state.update(bot_running=False)
        logger.info("Scheduler stopped")

    # ── Daily reset watcher ───────────────────────────
    async def _daily_watcher(self):
        while self.running:
            now   = _now_ist()
            today = now.date().isoformat()
            if (now.time() >= time(9, 14) and now.time() < time(9, 15) and
                    self._daily_reset_date != today):
                self._daily_reset_date = today
                logger.info("=== DAILY RESET ===")
                # Store prev day close BEFORE resetting state
                state = await self.state.snapshot()
                if state.spot_price and state.spot_price > 0:
                    self._prev_day_close = state.spot_price
                    logger.info("Prev day close stored: %.2f", self._prev_day_close)
                await self.state.daily_reset()
                self.trade_store.daily_reset()
                self.engine.clear_cache()
                self._orb_frozen      = False
                self._orb_frozen_time = None
                self._orb_open        = None
                self._orb_close       = None
                self._today_open      = None
                self._recent_spots    = []
                self._day_candles     = []
                self._current_minute  = None
                self._last_signal_time= 0.0
                logger.info("=== DAILY RESET COMPLETE ===")
            await asyncio.sleep(10)

    # ── Main poll loop ────────────────────────────────
    async def _loop(self):
        while self.running:
            try:
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

    # ── Tick ─────────────────────────────────────────
    async def _tick(self):
        try:
            quote = await self.broker.get_index_quote(settings.nifty_symbol)
        except RuntimeError as exc:
            now_ts = _time.time()
            if now_ts - self._last_broker_error >= _LOG_INTERVAL:
                logger.warning("Broker quote unavailable: %s", exc)
                self._last_broker_error = now_ts
            return

        spot = SamcoClient.parse_spot(quote)
        if spot is None: return

        # Capture today's open
        if self._today_open is None:
            self._today_open = spot
            logger.info("Today open captured: %.2f", spot)

        await self.state.update(spot_price=spot)
        await self.event_bus.publish("TICK", {"price": spot, "volume": float(quote.get("volume") or 0)})

        self._recent_spots.append(spot)
        if len(self._recent_spots) > 10: self._recent_spots.pop(0)

        state = await self.state.snapshot()
        now   = _now_ist()

        completed_candle = self._update_candle(spot, now)
        if completed_candle:
            self._day_candles.append(completed_candle)
            if len(self._day_candles) > 60: self._day_candles.pop(0)

        market_open_today = now.replace(hour=9, minute=15, second=0, microsecond=0)
        seconds_since_open = (now - market_open_today).total_seconds()
        if seconds_since_open < 0: return

        in_orb = seconds_since_open < settings.orb_duration_seconds

        # ── Build ORB 9:15–9:30 ──────────────────────
        if in_orb and not self._orb_frozen:
            if self._orb_open is None:
                self._orb_open = spot
                logger.info("ORB open captured: %.2f", spot)
            high = spot if state.orb_high is None else max(state.orb_high, spot)
            low  = spot if state.orb_low  is None else min(state.orb_low,  spot)
            self._orb_close = spot
            await self.state.update(orb_high=high, orb_low=low)
            return

        # ── Freeze ORB at 9:30 ───────────────────────
        if not in_orb and not self._orb_frozen:
            self._orb_frozen      = True
            self._orb_frozen_time = now.timestamp()
            atr = self._compute_atr()

            if state.orb_high is not None and state.orb_low is not None:
                orb_range = state.orb_high - state.orb_low

                if orb_range < settings.min_orb_range:
                    logger.warning("ORB range %.1f < min %.1f — trading disabled",
                                   orb_range, settings.min_orb_range)
                    await self.state.update(trading_enabled=False)
                    return

                orb_max = getattr(settings, "orb_max_range", 150.0)
                if orb_range > orb_max:
                    logger.warning("ORB range %.1f > max %.1f (chaotic) — trading disabled",
                                   orb_range, orb_max)
                    await self.state.update(trading_enabled=False)
                    return

                logger.info("ORB FROZEN high=%.2f low=%.2f range=%.2f ATR=%.2f",
                            state.orb_high, state.orb_low, orb_range, atr)

                if orb_range < atr * settings.orb_atr_multiplier:
                    logger.warning("Choppy ORB — trading disabled")
                    await self.state.update(trading_enabled=False)
            else:
                logger.warning("Bot started after ORB window — trading disabled today")
                await self.state.update(trading_enabled=False)

        # ── Post-ORB guards ───────────────────────────
        if state.active_trade:        return
        if state.signal is not None:  return
        if not state.trading_enabled: return
        if state.orb_high is None or state.orb_low is None: return
        if state.orb_high - state.orb_low < settings.min_orb_range: return

        no_h, no_m = map(int, settings.no_entry_after.split(":"))
        if now.time() >= time(no_h, no_m): return

        now_ts = now.timestamp()
        if now_ts - self._last_signal_time < settings.signal_cooldown: return

        # Candle-close confirmation
        if completed_candle is None: return

        # ── Skip first candle after ORB freeze ───────
        if settings.skip_first_candle and self._orb_frozen_time is not None:
            if now.timestamp() - self._orb_frozen_time < 65:  # skip exactly 1 candle (65s buffer)
                return

        bu = state.orb_high + settings.breakout_buffer
        bd = state.orb_low  - settings.breakout_buffer

        candle_above = completed_candle["close"] > bu
        candle_below = completed_candle["close"] < bd
        if not candle_above and not candle_below: return

        # ── 3-component trend score ───────────────────
        ts = self._trend_score(state.orb_high, state.orb_low)

        if candle_above:
            signal_type = "CALL"
            if settings.trend_filter_enabled and ts < 3:
                logger.info("TREND FILTER: CALL skipped score=%+d (need +3)", ts); return
            elif not settings.trend_filter_enabled and ts < 0:
                return
        else:
            signal_type = "PUT"
            if settings.trend_filter_enabled and ts > -3:
                logger.info("TREND FILTER: PUT skipped score=%+d (need -3)", ts); return
            elif not settings.trend_filter_enabled and ts > 0:
                return

        total_min = now.hour * 60 + now.minute
        if   total_min <= 10*60+30: size = "FULL"
        elif total_min <= 12*60:    size = "MEDIUM"
        elif total_min <= 13*60+30: size = "HALF"
        else: return

        logger.info("SIGNAL %s close=%.2f trend=%+d size=%s orb=%.2f/%.2f",
                    signal_type, completed_candle["close"], ts, size,
                    state.orb_high, state.orb_low)

        payload = {"signal": signal_type, "spot_price": completed_candle["close"],
                   "size_label": size, "trend_score": ts}
        await self.state.update(signal=signal_type, signal_meta=payload)
        await self.event_bus.publish("SIGNAL", payload)
        self._last_signal_time = now_ts

    # ── Candle builder ────────────────────────────────
    def _update_candle(self, spot, now):
        minute_ts = now.replace(second=0, microsecond=0)
        if self._current_minute is None:
            self._current_minute = minute_ts
            self._candle_open = self._candle_high = self._candle_low = self._candle_close = spot
            return None
        if minute_ts > self._current_minute:
            completed = {"ts": self._current_minute, "open": self._candle_open,
                         "high": self._candle_high, "low": self._candle_low, "close": self._candle_close}
            self._current_minute = minute_ts
            self._candle_open = self._candle_high = self._candle_low = self._candle_close = spot
            return completed
        self._candle_high  = max(self._candle_high, spot)
        self._candle_low   = min(self._candle_low,  spot)
        self._candle_close = spot
        return None

    def _compute_atr(self):
        if len(self._day_candles) < 3: return 15.0
        return sum(c["high"]-c["low"] for c in self._day_candles) / len(self._day_candles)

    def _trend_score(self, orb_high, orb_low) -> int:
        """
        3-component trend score (range -3 to +3, no lookahead):
        Component 1: Gap direction (today_open vs prev_close)
        Component 2: ORB candle direction (orb_close vs orb_open)
        Component 3: Price vs prev close (orb_close vs prev_close)
        CALL only on score=+3, PUT only on score=-3
        """
        score = 0
        if self._today_open and self._prev_day_close:
            if self._today_open > self._prev_day_close: score += 1
            elif self._today_open < self._prev_day_close: score -= 1
        if self._orb_open and self._orb_close:
            if self._orb_close > self._orb_open: score += 1
            elif self._orb_close < self._orb_open: score -= 1
        if self._orb_close and self._prev_day_close:
            if self._orb_close > self._prev_day_close: score += 1
            elif self._orb_close < self._prev_day_close: score -= 1
        return score

    async def flatten_position(self):
        state = await self.state.snapshot()
        if not state.active_trade: return {"status": "no_active_trade"}
        trade  = state.active_trade
        symbol = trade.get("symbol")
        qty    = trade.get("t2_qty", trade.get("qty",0)//2) if trade.get("t1_booked") else trade.get("qty",0)
        try:
            resp = await self.broker.place_order(symbol=symbol, side="SELL", quantity=qty)
            await self.state.update(active_trade=None, live_pnl=0.0)
            logger.info("Manual flatten %s qty=%s", symbol, qty)
            return {"status": "flattened", "symbol": symbol, "qty": qty}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}


scheduler = MarketScheduler()

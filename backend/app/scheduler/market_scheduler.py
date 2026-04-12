from __future__ import annotations

import asyncio
from datetime import datetime, time

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import state_manager
from backend.app.engine.trading_engine import TradingEngine
from backend.app.storage.trade_store import TradeStore
from backend.app.utils.logger import get_logger

settings = get_settings()

MARKET_OPEN_HOUR   = 9
MARKET_OPEN_MINUTE = 15


class MarketScheduler:

    def __init__(self):
        self.logger      = get_logger("market_scheduler")
        self.state       = state_manager
        self.trade_store = TradeStore()
        self.broker      = SamcoClient()
        self.event_bus   = EventBus()

        self.engine = TradingEngine(
            event_bus=self.event_bus,
            state_manager=self.state,
            trade_store=self.trade_store,
            broker=self.broker,
        )

        self.running = False
        self._task:        asyncio.Task | None = None
        self._engine_task: asyncio.Task | None = None

        # ORB state
        self._orb_frozen       = False
        self._last_signal_time = 0.0
        self._previous_spot:   float | None = None

        # Candle builder (aggregates 1s ticks → 1-min candles)
        self._current_minute:  datetime | None = None
        self._candle_open:     float | None    = None
        self._candle_high:     float           = 0.0
        self._candle_low:      float           = 999999.0
        self._candle_close:    float | None    = None

        # ORB analytics (for trend score)
        self._orb_open:        float | None = None
        self._orb_close:       float | None = None
        self._recent_spots:    list         = []
        self._day_candles:     list         = []

    # --------------------------------------------------
    # START / STOP
    # --------------------------------------------------
    async def start(self):
        if self.running:
            return
        self.logger.info("Starting MarketScheduler")
        self.running = True
        await self.event_bus.start()
        await self.broker.login()
        self._engine_task = asyncio.create_task(self.engine.run(), name="trading-engine")
        self._task        = asyncio.create_task(self._loop(),       name="market-loop")

    async def stop(self):
        if not self.running:
            return
        self.logger.info("Stopping MarketScheduler")
        self.running = False
        await self.event_bus.stop()
        for task in (self._task, self._engine_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    # --------------------------------------------------
    # MAIN POLL LOOP
    # --------------------------------------------------
    async def _loop(self):
        while self.running:
            try:
                await self._tick()
            except Exception as exc:
                self.logger.error("Market loop error: %s", exc, exc_info=True)
            await asyncio.sleep(settings.poll_seconds)

    # --------------------------------------------------
    # 1-MIN CANDLE BUILDER
    # Aggregates 1-second ticks into completed 1-min OHLC candles.
    # Returns completed candle dict when minute rolls over, else None.
    # --------------------------------------------------
    def _update_candle(self, spot: float, now: datetime) -> dict | None:
        minute_ts = now.replace(second=0, microsecond=0)

        if self._current_minute is None:
            self._current_minute = minute_ts
            self._candle_open    = spot
            self._candle_high    = spot
            self._candle_low     = spot
            self._candle_close   = spot
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
            self._candle_open    = spot
            self._candle_high    = spot
            self._candle_low     = spot
            self._candle_close   = spot
            return completed

        self._candle_high  = max(self._candle_high, spot)
        self._candle_low   = min(self._candle_low,  spot)
        self._candle_close = spot
        return None

    # --------------------------------------------------
    # ATR — average 1-min candle range
    # --------------------------------------------------
    def _compute_atr(self) -> float:
        if len(self._day_candles) < 3:
            return 15.0
        return sum(c["high"] - c["low"] for c in self._day_candles) / len(self._day_candles)

    # --------------------------------------------------
    # 3-FACTOR TREND SCORE
    # Returns: +2 strong bull / +1 mild bull / 0 neutral
    #          -1 mild bear  / -2 strong bear
    # --------------------------------------------------
    def _trend_score(self, orb_high: float, orb_low: float) -> int:
        score   = 0
        orb_mid = (orb_high + orb_low) / 2

        # Factor 1: ORB open vs midpoint
        if self._orb_open is not None:
            if self._orb_open > orb_mid:   score += 1
            elif self._orb_open < orb_mid: score -= 1

        # Factor 2: ORB body direction
        if self._orb_open and self._orb_close:
            if self._orb_close > self._orb_open:   score += 1
            elif self._orb_close < self._orb_open: score -= 1

        # Factor 3: recent 5-tick momentum
        if len(self._recent_spots) >= 5:
            mom = self._recent_spots[-1] - self._recent_spots[-5]
            if mom > 5:    score += 1
            elif mom < -5: score -= 1

        return score

    # --------------------------------------------------
    # TICK
    # --------------------------------------------------
    async def _tick(self):

        # 1. Fetch spot ───────────────────────────────
        quote = await self.broker.get_index_quote("NIFTY 50")
        spot  = SamcoClient.parse_spot(quote)
        if spot is None:
            self.logger.warning("Spot unavailable — skipping tick")
            return

        self.logger.info("NIFTY spot=%.2f", spot)
        await self.state.update(spot_price=spot)
        await self.event_bus.publish("TICK", {"price": spot})

        # Track recent spots for momentum
        self._recent_spots.append(spot)
        if len(self._recent_spots) > 10:
            self._recent_spots.pop(0)

        state = await self.state.snapshot()
        now   = datetime.now()

        # 2. Build 1-min candle ───────────────────────
        completed_candle = self._update_candle(spot, now)
        if completed_candle:
            self._day_candles.append(completed_candle)
            if len(self._day_candles) > 60:
                self._day_candles.pop(0)

        # 3. ORB phase calculation ─────────────────────
        market_open_today = now.replace(
            hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE,
            second=0, microsecond=0,
        )
        seconds_since_open = (now - market_open_today).total_seconds()

        # Pre-market: bot started before 9:15
        if seconds_since_open < 0:
            self.logger.debug("Pre-market — %.0fs until 09:15", abs(seconds_since_open))
            return

        in_orb_window = seconds_since_open < settings.orb_duration_seconds

        # 4. Build ORB range (9:15–9:30) ─────────────
        if in_orb_window and not self._orb_frozen:
            # Capture first tick of ORB as open
            if self._orb_open is None:
                self._orb_open = spot
                self.logger.info("ORB open captured: %.2f", spot)

            high = state.orb_high
            low  = state.orb_low
            high = spot if high is None else max(high, spot)
            low  = spot if low  is None else min(low,  spot)
            self._orb_close = spot   # update until window ends

            await self.state.update(orb_high=high, orb_low=low)
            self.logger.info("ORB building — high=%.2f  low=%.2f", high, low)
            return

        # 5. Freeze ORB at 9:30 ───────────────────────
        if not in_orb_window and not self._orb_frozen:
            self._orb_frozen = True
            atr = self._compute_atr()

            if state.orb_high is not None and state.orb_low is not None:
                orb_range = state.orb_high - state.orb_low
                self.logger.info(
                    "ORB COMPLETE — high=%.2f  low=%.2f  range=%.2f  ATR=%.2f",
                    state.orb_high, state.orb_low, orb_range, atr,
                )
                # ATR quality filter: skip choppy days
                if orb_range < atr * settings.orb_atr_multiplier:
                    self.logger.warning(
                        "ORB range %.2f < %.1fx ATR %.2f — choppy, trading disabled",
                        orb_range, settings.orb_atr_multiplier, atr,
                    )
                    await self.state.update(trading_enabled=False)
            else:
                self.logger.warning(
                    "Bot started after ORB window — no range built. "
                    "Trading disabled today. Restart before 09:15 tomorrow."
                )
                await self.state.update(trading_enabled=False)

        # 6. Post-ORB guards ──────────────────────────
        if state.active_trade:   return
        if state.signal is not None: return
        if not state.trading_enabled: return
        if state.orb_high is None or state.orb_low is None: return

        orb_range = state.orb_high - state.orb_low
        if orb_range < settings.min_orb_range:
            return

        # 7. No-entry time guard ──────────────────────
        no_entry_h, no_entry_m = map(int, settings.no_entry_after.split(":"))
        if now.time() >= time(no_entry_h, no_entry_m):
            return

        # 8. Signal cooldown ──────────────────────────
        now_ts = now.timestamp()
        if now_ts - self._last_signal_time < settings.signal_cooldown:
            return

        # 9. CANDLE CLOSE CONFIRMATION ────────────────
        # Only fire when a 1-min candle CLOSES above/below ORB level.
        # Wick touches are ignored → eliminates 41% fake breakouts.
        if completed_candle is None:
            return   # wait for a completed candle

        bu = state.orb_high + settings.breakout_buffer
        bd = state.orb_low  - settings.breakout_buffer

        candle_above = completed_candle["close"] > bu
        candle_below = completed_candle["close"] < bd

        if not candle_above and not candle_below:
            return

        # 10. TREND FILTER (optional, config-controlled)
        ts          = self._trend_score(state.orb_high, state.orb_low)
        signal_type = None

        if candle_above:
            if settings.trend_filter_enabled and ts < 0:
                self.logger.info(
                    "CALL rejected — bearish trend score %d  close=%.2f", ts,
                    completed_candle["close"],
                )
                return
            signal_type = "CALL"

        elif candle_below:
            if settings.trend_filter_enabled and ts > 0:
                self.logger.info(
                    "PUT rejected — bullish trend score %d  close=%.2f", ts,
                    completed_candle["close"],
                )
                return
            signal_type = "PUT"

        # 11. Time-weighted size label ─────────────────
        total_min = now.hour * 60 + now.minute
        if   total_min <= 10 * 60 + 30: size = "FULL"
        elif total_min <= 12 * 60:      size = "MEDIUM"
        elif total_min <= 13 * 60 + 30: size = "HALF"
        else:
            self.logger.info("Entry time %02d:%02d past 13:30 — skipping", now.hour, now.minute)
            return

        # 12. Publish signal ──────────────────────────
        direction = "BREAKOUT_UP" if signal_type == "CALL" else "BREAKOUT_DOWN"
        self.logger.info(
            "%s → %s  close=%.2f  trend=%+d  size=%s  orb=%.2f/%.2f",
            direction, signal_type,
            completed_candle["close"], ts, size,
            state.orb_high, state.orb_low,
        )

        await self.event_bus.publish(
            "SIGNAL", {
                "signal":      signal_type,
                "spot_price":  completed_candle["close"],
                "size_label":  size,
                "trend_score": ts,
            }
        )
        self._last_signal_time = now_ts
        self._previous_spot    = spot


# Singleton
scheduler = MarketScheduler()
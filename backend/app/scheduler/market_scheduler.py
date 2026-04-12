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
        self.logger = get_logger("market_scheduler")

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

        # ULTRA PRO MAX: trend scoring state
        self._today_open:      float | None = None
        self._orb_open:        float | None = None
        self._orb_close:       float | None = None
        self._recent_spots:    list  = []      # last 5 spots for momentum
        self._atr:             float | None = None
        self._day_candles:     list  = []      # 1-min candles built from ticks

        # ULTRA PRO MAX: candle close confirmation state
        self._current_minute:  datetime | None = None
        self._candle_open:     float | None = None
        self._candle_high:     float = 0.0
        self._candle_low:      float = 999999.0
        self._candle_close:    float | None = None

    # --------------------------------------------------
    # START
    # --------------------------------------------------
    async def start(self):
        if self.running:
            return

        self.logger.info("Starting MarketScheduler")
        self.running = True

        await self.event_bus.start()
        await self.broker.login()

        self._engine_task = asyncio.create_task(
            self.engine.run(), name="trading-engine"
        )
        self._task = asyncio.create_task(
            self._loop(), name="market-loop"
        )

    # --------------------------------------------------
    # STOP
    # --------------------------------------------------
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
    # ULTRA PRO MAX: Build 1-min candle from ticks
    # Returns completed candle when minute changes
    # --------------------------------------------------
    def _update_candle(self, spot: float, now: datetime) -> dict | None:
        """
        Aggregates 1-second ticks into 1-minute OHLC candles.
        Returns the completed candle when a new minute starts.
        """
        minute_ts = now.replace(second=0, microsecond=0)

        if self._current_minute is None:
            # First tick ever
            self._current_minute = minute_ts
            self._candle_open    = spot
            self._candle_high    = spot
            self._candle_low     = spot
            self._candle_close   = spot
            return None

        if minute_ts > self._current_minute:
            # New minute — emit completed candle
            completed = {
                "ts":    self._current_minute,
                "open":  self._candle_open,
                "high":  self._candle_high,
                "low":   self._candle_low,
                "close": self._candle_close,
            }
            # Start new candle
            self._current_minute = minute_ts
            self._candle_open    = spot
            self._candle_high    = spot
            self._candle_low     = spot
            self._candle_close   = spot
            return completed

        # Same minute — update running candle
        self._candle_high  = max(self._candle_high, spot)
        self._candle_low   = min(self._candle_low, spot)
        self._candle_close = spot
        return None

    # --------------------------------------------------
    # ULTRA PRO MAX: Compute 3-factor trend score
    # Returns +2/+1/0/-1/-2 (positive=bullish, negative=bearish)
    # --------------------------------------------------
    def _trend_score(self, state) -> int:
        score = 0

        if state.orb_high is None or state.orb_low is None:
            return 0

        orb_mid = (state.orb_high + state.orb_low) / 2

        # Factor 1: ORB open vs ORB midpoint
        if self._orb_open is not None:
            if self._orb_open > orb_mid:
                score += 1
            elif self._orb_open < orb_mid:
                score -= 1

        # Factor 2: ORB body direction (open vs close of ORB period)
        if self._orb_open is not None and self._orb_close is not None:
            if self._orb_close > self._orb_open:
                score += 1
            elif self._orb_close < self._orb_open:
                score -= 1

        # Factor 3: Recent momentum — last 5 spots
        if len(self._recent_spots) >= 5:
            mom = self._recent_spots[-1] - self._recent_spots[-5]
            if mom > 5:
                score += 1
            elif mom < -5:
                score -= 1

        return score

    # --------------------------------------------------
    # ULTRA PRO MAX: Compute ATR from day candles
    # --------------------------------------------------
    def _compute_atr(self) -> float:
        if len(self._day_candles) < 3:
            return 15.0  # default ATR if not enough data
        ranges = [c["high"] - c["low"] for c in self._day_candles]
        return sum(ranges) / len(ranges)

    # --------------------------------------------------
    # TICK
    # --------------------------------------------------
    async def _tick(self):

        # ── 1. Fetch NIFTY spot ──────────────────────────────
        quote = await self.broker.get_index_quote("NIFTY 50")
        spot  = SamcoClient.parse_spot(quote)

        if spot is None:
            self.logger.warning("Spot price unavailable — skipping tick")
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

        # ── 2. Build 1-min candle ────────────────────────────
        completed_candle = self._update_candle(spot, now)
        if completed_candle:
            self._day_candles.append(completed_candle)
            # Keep last 50 candles only
            if len(self._day_candles) > 50:
                self._day_candles.pop(0)

        # ── 3. Determine ORB phase ───────────────────────────
        market_open_today = now.replace(
            hour=MARKET_OPEN_HOUR,
            minute=MARKET_OPEN_MINUTE,
            second=0,
            microsecond=0,
        )
        seconds_since_open = (now - market_open_today).total_seconds()

        # Pre-market idle
        if seconds_since_open < 0:
            self.logger.debug(
                "Pre-market — %.0fs until ORB window opens",
                abs(seconds_since_open),
            )
            return

        in_orb_window = seconds_since_open < settings.orb_duration_seconds

        # ── 4. Build ORB range (9:15–9:30) ──────────────────
        if in_orb_window and not self._orb_frozen:
            high = state.orb_high
            low  = state.orb_low

            # Capture ORB open (very first tick at 9:15)
            if self._orb_open is None:
                self._orb_open = spot
                self.logger.info("ORB open captured: %.2f", spot)

            high = spot if high is None else max(high, spot)
            low  = spot if low  is None else min(low,  spot)

            # Track ORB close (keeps updating until window ends)
            self._orb_close = spot

            await self.state.update(orb_high=high, orb_low=low)
            self.logger.info("ORB building — high=%.2f  low=%.2f", high, low)
            return

        # ── 5. Freeze ORB once window closes ────────────────
        if not in_orb_window and not self._orb_frozen:
            self._orb_frozen = True
            self._atr = self._compute_atr()

            if state.orb_high is not None and state.orb_low is not None:
                orb_range = state.orb_high - state.orb_low

                self.logger.info(
                    "ORB COMPLETE — high=%.2f  low=%.2f  range=%.2f  ATR=%.2f",
                    state.orb_high, state.orb_low, orb_range, self._atr,
                )

                # ULTRA PRO MAX: skip if ORB range is less than 1.5x ATR
                # Means market is compressed — no energy for breakout
                if orb_range < self._atr * 1.5:
                    self.logger.warning(
                        "ORB range %.2f < 1.5x ATR %.2f — choppy day, trading disabled",
                        orb_range, self._atr,
                    )
                    await self.state.update(trading_enabled=False)

            else:
                self.logger.warning(
                    "Bot started after ORB window — no range built. "
                    "Trading disabled for today. Restart before 09:15 tomorrow."
                )
                await self.state.update(trading_enabled=False)

        # ── 6. Post-ORB guards ───────────────────────────────
        if state.active_trade:
            return

        if state.signal is not None:
            return

        if not state.trading_enabled:
            return

        if state.orb_high is None or state.orb_low is None:
            return

        # ── 7. No-entry time guard ───────────────────────────
        no_entry_h, no_entry_m = map(int, settings.no_entry_after.split(":"))
        if now.time() >= time(no_entry_h, no_entry_m):
            return

        # ── 8. Signal cooldown ───────────────────────────────
        now_ts = now.timestamp()
        if now_ts - self._last_signal_time < settings.signal_cooldown:
            return

        # ── 9. ULTRA PRO MAX: Candle close confirmation ──────
        # We only fire a signal when a completed 1-min candle
        # CLOSES above/below the ORB breakout level.
        # Wick touches are ignored — only candle BODY matters.
        if completed_candle is None:
            return  # Wait for a completed candle

        bu = state.orb_high + settings.breakout_buffer
        bd = state.orb_low  - settings.breakout_buffer

        candle_closed_above = completed_candle["close"] > bu
        candle_closed_below = completed_candle["close"] < bd

        if not candle_closed_above and not candle_closed_below:
            return  # No confirmed breakout this candle

        # ── 10. ULTRA PRO MAX: Trend score filter ────────────
        ts = self._trend_score(state)
        signal_type = None

        if candle_closed_above:
            if ts < 0:
                # Strong bearish bias — skip CALL signal
                self.logger.info(
                    "CALL rejected — bearish trend score %d  "
                    "candle_close=%.2f  orb_high=%.2f",
                    ts, completed_candle["close"], state.orb_high,
                )
                return
            signal_type = "CALL"

        elif candle_closed_below:
            if ts > 0:
                # Strong bullish bias — skip PUT signal
                self.logger.info(
                    "PUT rejected — bullish trend score %d  "
                    "candle_close=%.2f  orb_low=%.2f",
                    ts, completed_candle["close"], state.orb_low,
                )
                return
            signal_type = "PUT"

        # ── 11. ULTRA PRO MAX: Time-weighted size ────────────
        total_min = now.hour * 60 + now.minute
        if total_min <= 10 * 60 + 30:
            size_label = "FULL"
        elif total_min <= 12 * 60:
            size_label = "MEDIUM"
        elif total_min <= 13 * 60 + 30:
            size_label = "HALF"
        else:
            self.logger.info("Entry time %02d:%02d past 13:30 — skipping", now.hour, now.minute)
            return

        # ── 12. Publish signal ───────────────────────────────
        self.logger.info(
            "CONFIRMED %s → %s  candle_close=%.2f  "
            "trend_score=%d  size=%s  orb=%.2f/%.2f",
            "BREAKOUT_UP" if signal_type=="CALL" else "BREAKOUT_DOWN",
            signal_type,
            completed_candle["close"],
            ts,
            size_label,
            state.orb_high,
            state.orb_low,
        )

        await self.event_bus.publish(
            "SIGNAL", {
                "signal":     signal_type,
                "spot_price": completed_candle["close"],
                "size_label": size_label,
                "trend_score": ts,
            }
        )
        self._last_signal_time = now_ts
        self._previous_spot = spot


# Singleton — imported by main.py
scheduler = MarketScheduler()

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

# ----------------------------------------------------------------
# Market open reference — ORB always starts from 09:15 IST
# ----------------------------------------------------------------
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15


class MarketScheduler:

    def __init__(self):
        self.logger = get_logger("market_scheduler")

        self.state = state_manager
        self.trade_store = TradeStore()
        self.broker = SamcoClient()
        self.event_bus = EventBus()

        self.engine = TradingEngine(
            event_bus=self.event_bus,
            state_manager=self.state,
            trade_store=self.trade_store,
            broker=self.broker,
        )

        self.running = False

        self._task: asyncio.Task | None = None
        self._engine_task: asyncio.Task | None = None

        # ORB state
        self._orb_frozen = False
        self._orb_logged = False
        self._last_signal_time = 0.0
        self._previous_spot: float | None = None

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
    # TICK — called every poll_seconds
    # --------------------------------------------------
    async def _tick(self):

        # ── 1. Fetch NIFTY spot ──────────────────────────────
        quote = await self.broker.get_index_quote("NIFTY 50")
        spot = SamcoClient.parse_spot(quote)

        if spot is None:
            self.logger.warning("Spot price unavailable — skipping tick")
            return

        self.logger.info("NIFTY spot=%.2f", spot)
        await self.state.update(spot_price=spot)
        await self.event_bus.publish("TICK", {"price": spot})

        state = await self.state.snapshot()
        now = datetime.now()

        # ── 2. Determine ORB phase ───────────────────────────
        # Pin ORB window to wall-clock 09:15–09:30, regardless of
        # when the bot was started.
        market_open_today = now.replace(
            hour=MARKET_OPEN_HOUR,
            minute=MARKET_OPEN_MINUTE,
            second=0,
            microsecond=0,
        )
        seconds_since_open = (now - market_open_today).total_seconds()

        in_orb_window = 0 <= seconds_since_open < settings.orb_duration_seconds

        # ── 3. Build ORB range (first 15 min) ───────────────
        if in_orb_window and not self._orb_frozen:

            high = state.orb_high
            low = state.orb_low

            high = spot if high is None else max(high, spot)
            low = spot if low is None else min(low, spot)

            await self.state.update(orb_high=high, orb_low=low)

            self.logger.info("ORB building — high=%.2f  low=%.2f", high, low)
            return

        # ── 4. Freeze ORB once window closes ────────────────
        if not in_orb_window and not self._orb_frozen:

            self._orb_frozen = True

            if state.orb_high is not None and state.orb_low is not None:
                self.logger.info(
                    "ORB COMPLETE — high=%.2f  low=%.2f  range=%.2f",
                    state.orb_high, state.orb_low,
                    state.orb_high - state.orb_low,
                )
            else:
                self.logger.warning("ORB frozen but range is None — no data before 09:30?")

        # ── 5. Post-ORB guards ───────────────────────────────
        if state.active_trade:
            return

        if state.signal is not None:
            return

        if not state.trading_enabled:
            return

        if state.orb_high is None or state.orb_low is None:
            return

        orb_range = state.orb_high - state.orb_low
        if orb_range < settings.min_orb_range:
            self.logger.debug("ORB range %.2f too small — skipping", orb_range)
            return

        # ── 6. No-entry time guard ───────────────────────────
        no_entry_h, no_entry_m = map(int, settings.no_entry_after.split(":"))
        if now.time() >= time(no_entry_h, no_entry_m):
            return

        # ── 7. Signal cooldown ───────────────────────────────
        now_ts = now.timestamp()
        if now_ts - self._last_signal_time < settings.signal_cooldown:
            return

        # ── 8. Breakout detection ────────────────────────────
        breakout_up   = state.orb_high + settings.breakout_buffer
        breakout_down = state.orb_low  - settings.breakout_buffer

        call_signal = spot > breakout_up
        put_signal  = spot < breakout_down

        # Gap-open is also treated as a valid breakout
        call_gap = spot > breakout_up + settings.gap_threshold
        put_gap  = spot < breakout_down - settings.gap_threshold

        if call_signal or call_gap:
            self.logger.info(
                "BREAKOUT UP → CALL  spot=%.2f  orb_high=%.2f", spot, state.orb_high
            )
            await self.event_bus.publish(
                "SIGNAL", {"signal": "CALL", "spot_price": spot}
            )
            self._last_signal_time = now_ts

        elif put_signal or put_gap:
            self.logger.info(
                "BREAKOUT DOWN → PUT  spot=%.2f  orb_low=%.2f", spot, state.orb_low
            )
            await self.event_bus.publish(
                "SIGNAL", {"signal": "PUT", "spot_price": spot}
            )
            self._last_signal_time = now_ts

        self._previous_spot = spot


# Singleton — imported by main.py
scheduler = MarketScheduler()

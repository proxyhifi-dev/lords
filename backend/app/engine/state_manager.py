# backend/app/engine/state_manager.py

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.app.core.config_loader import get_settings
from backend.app.utils.logger import get_logger

settings = get_settings()
logger   = get_logger("state_manager")


# ─────────────────────────────────────────────────────────
# STATE MODEL (UPDATED)
# ─────────────────────────────────────────────────────────
@dataclass
class RuntimeState:
    spot_price:       float | None = None
    orb_high:         float | None = None
    orb_low:          float | None = None
    signal:           str   | None = None
    signal_meta:      dict  | None = None

    active_trade:     dict[str, Any] | None = None

    daily_pnl:        float = 0.0
    live_pnl:         float = 0.0
    unrealized_pnl:   float = 0.0   # ✅ CRITICAL FIX

    trade_count:      int   = 0
    trading_enabled:  bool  = True
    trading_mode:     str   = "PAPER"
    bot_running:      bool  = False

    # ✅ NEW: cooldown system
    cooldown_active:  bool  = False
    cooldown_until:   str   | None = None

    # ✅ NEW: execution safety
    last_order_failed: bool = False

    trade_date:       str   = ""


# ─────────────────────────────────────────────────────────
# STATE MANAGER
# ─────────────────────────────────────────────────────────
class StateManager:

    def __init__(self, state_file: str | None = None):
        self.lock        = asyncio.Lock()   # ✅ EXPOSED LOCK (IMPORTANT)
        self._state_file = Path(state_file or settings.state_file)
        self._state_file.parent.mkdir(parents=True, exist_ok=True)

        self._state      = RuntimeState()
        self._dirty      = False
        self._persist_task: asyncio.Task | None = None

    # ─────────────────────────────────────────────────────
    # LOAD
    # ─────────────────────────────────────────────────────
    async def load(self) -> None:
        if not self._state_file.exists():
            await self._write()
            return

        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            known = {f for f in RuntimeState.__dataclass_fields__}

            self._state = RuntimeState(**{k: v for k, v in raw.items() if k in known})
            logger.info("State loaded — trade_date=%s", self._state.trade_date)

        except Exception as exc:
            logger.warning("State load failed (%s) — fresh state", exc)
            self._state = RuntimeState()
            await self._write()

        await self._maybe_daily_reset()

    # ─────────────────────────────────────────────────────
    # DAILY RESET
    # ─────────────────────────────────────────────────────
    async def _maybe_daily_reset(self) -> None:
        today = date.today().isoformat()

        if self._state.trade_date == today:
            return

        logger.info("Daily reset: %s → %s", self._state.trade_date, today)

        async with self.lock:
            self._state.trade_date     = today
            self._state.daily_pnl      = 0.0
            self._state.live_pnl       = 0.0
            self._state.unrealized_pnl = 0.0

            self._state.trade_count    = 0
            self._state.trading_enabled = True

            self._state.orb_high       = None
            self._state.orb_low        = None
            self._state.signal         = None
            self._state.signal_meta    = None
            self._state.active_trade   = None

            # reset safety flags
            self._state.cooldown_active   = False
            self._state.cooldown_until    = None
            self._state.last_order_failed = False

            self._dirty = True

        await self._write()
        logger.info("Daily reset complete")

    async def daily_reset(self) -> None:
        self._state.trade_date = ""
        await self._maybe_daily_reset()

    # ─────────────────────────────────────────────────────
    # SNAPSHOT
    # ─────────────────────────────────────────────────────
    async def snapshot(self) -> RuntimeState:
        async with self.lock:
            return RuntimeState(**asdict(self._state))

    # ─────────────────────────────────────────────────────
    # UPDATE
    # ─────────────────────────────────────────────────────
    async def update(self, **kwargs: Any) -> None:
        async with self.lock:
            for k, v in kwargs.items():
                if hasattr(self._state, k):
                    setattr(self._state, k, v)
                else:
                    logger.warning("StateManager: unknown key '%s'", k)

            self._dirty = True
            self._schedule_persist()

    # ─────────────────────────────────────────────────────
    # UNREALIZED PnL (CRITICAL)
    # ─────────────────────────────────────────────────────
    async def update_unrealized(self, current_price: float) -> None:
        async with self.lock:
            if not self._state.active_trade:
                self._state.unrealized_pnl = 0.0
                return

            entry_price = self._state.active_trade.get("entry_price", 0)
            qty         = self._state.active_trade.get("qty", settings.order_qty)

            self._state.unrealized_pnl = (current_price - entry_price) * qty

            self._dirty = True
            self._schedule_persist()

    # ─────────────────────────────────────────────────────
    # COOLDOWN SYSTEM
    # ─────────────────────────────────────────────────────
    async def trigger_cooldown(self, minutes: int = 15) -> None:
        async with self.lock:
            until = datetime.now() + timedelta(minutes=minutes)

            self._state.cooldown_active = True
            self._state.cooldown_until  = until.isoformat()

            logger.warning("Cooldown triggered until %s", until)

            self._dirty = True
            self._schedule_persist()

    async def check_cooldown(self) -> None:
        async with self.lock:
            if not self._state.cooldown_active:
                return

            if not self._state.cooldown_until:
                return

            if datetime.now() > datetime.fromisoformat(self._state.cooldown_until):
                self._state.cooldown_active = False
                self._state.cooldown_until  = None

                logger.info("Cooldown expired")

                self._dirty = True
                self._schedule_persist()

    # ─────────────────────────────────────────────────────
    # PERSIST
    # ─────────────────────────────────────────────────────
    def _schedule_persist(self) -> None:
        if self._persist_task is None or self._persist_task.done():
            try:
                loop = asyncio.get_running_loop()
                self._persist_task = loop.create_task(self._debounce_write())
            except RuntimeError:
                pass

    async def _debounce_write(self) -> None:
        await asyncio.sleep(1.0)
        if self._dirty:
            await self._write()
            self._dirty = False

    async def _write(self) -> None:
        try:
            self._state_file.write_text(
                json.dumps(asdict(self._state), indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("State persist failed: %s", exc)


# ─────────────────────────────────────────────────────────
# SINGLETON
# ─────────────────────────────────────────────────────────
state_manager = StateManager()
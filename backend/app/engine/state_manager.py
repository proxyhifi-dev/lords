from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from backend.app.core.config_loader import get_settings

settings = get_settings()


@dataclass
class RuntimeState:

    spot_price:      float | None = None
    orb_high:        float | None = None
    orb_low:         float | None = None
    signal:          str   | None = None
    signal_meta:     dict  | None = None   # ULTRA PRO MAX: size_label, trend_score
    active_trade:    dict[str, Any] | None = None
    daily_pnl:       float = 0.0
    live_pnl:        float = 0.0
    trade_count:     int   = 0
    trading_enabled: bool  = True
    trading_mode:    str   = "PAPER"


class StateManager:

    def __init__(self, state_file: str | None = None):
        self._lock        = asyncio.Lock()
        self._state_file  = Path(state_file or settings.state_file)
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state       = RuntimeState()
        self._dirty       = False
        self._persist_task: asyncio.Task | None = None

    async def load(self):
        if not self._state_file.exists():
            await self._write()
            return
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            fields = {f.name for f in self._state.__dataclass_fields__.values()}
            filtered = {k: v for k, v in raw.items() if k in fields}
            self._state = RuntimeState(**filtered)
        except Exception:
            await self._write()

    async def snapshot(self) -> RuntimeState:
        async with self._lock:
            return RuntimeState(**asdict(self._state))

    async def update(self, **kwargs: Any):
        async with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)
            self._dirty = True
            self._schedule_persist()

    def _schedule_persist(self):
        if self._persist_task is None or self._persist_task.done():
            try:
                loop = asyncio.get_running_loop()
                self._persist_task = loop.create_task(self._debounce_write())
            except RuntimeError:
                pass

    async def _debounce_write(self):
        await asyncio.sleep(1.0)
        if self._dirty:
            await self._write()
            self._dirty = False

    async def _write(self):
        self._state_file.write_text(
            json.dumps(asdict(self._state), indent=2),
            encoding="utf-8",
        )


state_manager = StateManager()

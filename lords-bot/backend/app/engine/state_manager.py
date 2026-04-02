from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from backend.config import settings


# -------------------------------------------------
# RUNTIME STATE MODEL
# -------------------------------------------------

@dataclass
class RuntimeState:

    spot_price: float | None = None

    orb_high: float | None = None
    orb_low: float | None = None

    signal: str | None = None

    active_trade: dict[str, Any] | None = None

    daily_pnl: float = 0.0

    trade_count: int = 0

    trading_enabled: bool = True


# -------------------------------------------------
# STATE MANAGER
# -------------------------------------------------

class StateManager:

    def __init__(self, state_file: str | None = None) -> None:

        self._lock = asyncio.Lock()

        self._state_file = Path(state_file or settings.state_file)

        self._state_file.parent.mkdir(parents=True, exist_ok=True)

        self._state = RuntimeState()

    # -------------------------------------------
    # LOAD STATE FROM DISK
    # -------------------------------------------

    async def load(self) -> None:

        if not self._state_file.exists():

            await self.persist()

            return

        try:

            data = json.loads(
                self._state_file.read_text(encoding="utf-8")
            )

            self._state = RuntimeState(**data)

        except Exception:

            await self.persist()

    # -------------------------------------------
    # SNAPSHOT (READ ONLY)
    # -------------------------------------------

    async def snapshot(self) -> RuntimeState:

        async with self._lock:

            return RuntimeState(**asdict(self._state))

    # -------------------------------------------
    # UPDATE STATE
    # -------------------------------------------

    async def update(self, **kwargs: Any) -> None:

        async with self._lock:

            for key, value in kwargs.items():

                if hasattr(self._state, key):

                    setattr(self._state, key, value)

            await self.persist()

    # -------------------------------------------
    # SAVE STATE
    # -------------------------------------------

    async def persist(self) -> None:

        self._state_file.write_text(

            json.dumps(
                asdict(self._state),
                indent=2
            ),

            encoding="utf-8"
        )
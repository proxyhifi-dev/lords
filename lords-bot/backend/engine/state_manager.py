from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from config import settings
from models import EngineState


class StateManager:

    def __init__(self, path: str | None = None) -> None:

        self.path = Path(path or settings.state_file)

        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------
    # LOAD STATE
    # ------------------------------------------------

    def load(self) -> EngineState:

        if not self.path.exists():

            return EngineState(
                trading_mode=settings.trading_mode.upper()
            )

        try:

            payload: dict[str, Any] = json.loads(
                self.path.read_text(encoding="utf-8")
            )

            state = EngineState(**payload)

        except Exception:
            # corrupted state recovery
            state = EngineState(
                trading_mode=settings.trading_mode.upper()
            )

        today = date.today().isoformat()

        if state.trade_day != today:

            state.trade_day = today

            state.trades_today = 0

            state.realized_pnl = 0.0

            state.consecutive_losses = 0

            state.active_trade = {}

        return state

    # ------------------------------------------------
    # SAVE STATE
    # ------------------------------------------------

    def save(self, state: EngineState) -> None:

        payload = json.dumps(
            state.to_dict(),
            indent=2,
        )

        tmp_file = self.path.with_suffix(".tmp")

        tmp_file.write_text(payload, encoding="utf-8")

        tmp_file.replace(self.path)
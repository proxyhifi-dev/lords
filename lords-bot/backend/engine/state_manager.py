from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from config import settings
from models import EngineState


class StateManager:
    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or settings.state_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> EngineState:
        if not self.path.exists():
            return EngineState(trading_mode=settings.trading_mode.upper())

        payload = json.loads(self.path.read_text(encoding='utf-8'))
        state = EngineState(**payload)
        if state.trade_day != date.today().isoformat():
            state.trade_day = date.today().isoformat()
            state.trades_today = 0
            state.realized_pnl = 0.0
            state.active_trade = {}
        return state

    def save(self, state: EngineState) -> None:
        self.path.write_text(json.dumps(state.to_dict(), indent=2), encoding='utf-8')

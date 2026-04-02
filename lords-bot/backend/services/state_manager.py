from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import settings


@dataclass
class RuntimeState:
    open_trades: dict[str, dict[str, Any]] = field(default_factory=dict)
    pnl: float = 0.0
    trade_count: int = 0
    last_known_prices: dict[str, float] = field(default_factory=dict)


class StateManager:
    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path or settings.state_file)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> RuntimeState:
        if not self._path.exists():
            return RuntimeState()
        payload = json.loads(self._path.read_text())
        return RuntimeState(**payload)

    def save(self, state: RuntimeState) -> None:
        self._path.write_text(json.dumps(asdict(state), indent=2))

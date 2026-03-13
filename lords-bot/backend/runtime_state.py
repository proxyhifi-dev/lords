from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config import settings


@dataclass
class RuntimeState:
    symbol: str = settings.symbol
    expiry: str = settings.expiry
    trading_mode: str = settings.trading_mode.upper()
    latest_option_chain: list[dict[str, Any]] = field(default_factory=list)
    latest_analysis: dict[str, Any] = field(default_factory=dict)
    latest_signal: dict[str, Any] = field(default_factory=dict)
    latest_underlying_price: float = 0.0
    last_execution: dict[str, Any] = field(default_factory=dict)


runtime_state = RuntimeState()

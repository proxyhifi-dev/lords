from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
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
    latest_best_trades: list[dict[str, Any]] = field(default_factory=list)
    latest_underlying_price: float = 0.0
    last_execution: dict[str, Any] = field(default_factory=dict)
    pending_trade: dict[str, Any] = field(default_factory=dict)
    scheduler_errors: list[str] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    day_pnl: float = 0.0
    trade_day: date = field(default_factory=date.today)


runtime_state = RuntimeState()

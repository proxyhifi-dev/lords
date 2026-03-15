from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class TradePosition:
    symbol: str
    side: str
    quantity: int
    entry_price: float
    stop_loss: float
    target_price: float
    entry_time: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    order_id: str = ''
    status: str = 'OPEN'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EngineState:
    trade_day: str = field(default_factory=lambda: date.today().isoformat())
    trading_mode: str = 'PAPER'
    system_status: str = 'RUNNING'
    strategy_state: dict[str, Any] = field(default_factory=dict)
    active_trade: dict[str, Any] = field(default_factory=dict)
    trades_today: int = 0
    realized_pnl: float = 0.0
    consecutive_losses: int = 0
    last_error: str = ''
    latest_signal: dict[str, Any] = field(default_factory=dict)
    orb_range: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

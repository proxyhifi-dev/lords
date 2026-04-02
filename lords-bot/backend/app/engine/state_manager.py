from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass
class RuntimeState:
    spot: float | None = None
    signal: str | None = None
    active_trade: dict[str, Any] | None = None
    pnl: float = 0.0


class StateManager:
    """Thread-safe in-memory runtime state for dashboard and orchestration."""

    def __init__(self, active_trade: dict[str, Any] | None = None) -> None:
        self._lock = Lock()
        self._state = RuntimeState(active_trade=active_trade)

    def snapshot(self) -> RuntimeState:
        with self._lock:
            return RuntimeState(
                spot=self._state.spot,
                signal=self._state.signal,
                active_trade=self._state.active_trade.copy() if self._state.active_trade else None,
                pnl=self._state.pnl,
            )

    def set_spot(self, spot: float) -> None:
        with self._lock:
            self._state.spot = spot

    def set_signal(self, signal: str) -> None:
        with self._lock:
            self._state.signal = signal

    def set_active_trade(self, trade: dict[str, Any] | None) -> None:
        with self._lock:
            self._state.active_trade = trade

    def update_pnl(self, pnl: float) -> None:
        with self._lock:
            self._state.pnl = pnl

from __future__ import annotations

from models import SignalResult


class SignalStore:
    def __init__(self) -> None:
        self._latest: SignalResult | None = None

    def set(self, signal: SignalResult) -> None:
        self._latest = signal

    def get(self) -> SignalResult | None:
        return self._latest


signal_store = SignalStore()

from __future__ import annotations

from backend.services.signal_service import SignalService


class StrategyEngine:
    def __init__(self) -> None:
        self.signal_service = SignalService()

    def run(self, analysis: dict) -> dict:
        return self.signal_service.generate_signal(analysis)


strategy_engine = StrategyEngine()

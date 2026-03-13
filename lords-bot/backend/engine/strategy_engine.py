from __future__ import annotations

from core.cache import TTLCache
from services.signal_service import SignalService


class StrategyEngine:
    def __init__(self, cache: TTLCache) -> None:
        self.signal_service = SignalService(cache)

    def run(self, analysis: dict, option_chain: list[dict]) -> dict:
        return self.signal_service.generate_signal(analysis, option_chain)


strategy_engine = StrategyEngine(TTLCache())

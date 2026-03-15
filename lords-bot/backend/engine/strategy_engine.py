from __future__ import annotations

from core.cache import TTLCache
from services.signal_service import SignalService
from strategies import PCRStrategy


class StrategyEngine:
    def __init__(self, cache: TTLCache) -> None:
        self.signal_service = SignalService(cache)
        self.pcr_strategy = PCRStrategy()

    def run(self, analysis: dict, option_chain: list[dict]) -> dict:
        signal = self.signal_service.generate_signal(analysis, option_chain)
        signal['strategy_signal'] = self.pcr_strategy.generate(float(analysis.get('pcr', 0) or 0))
        return signal


strategy_engine = StrategyEngine(TTLCache())

from __future__ import annotations

from typing import Any

from strategies.strategy_manager import StrategyManager, SignalStrategy


class StrategyEngine:
    def __init__(self, strategies: list[SignalStrategy]) -> None:
        self._manager = StrategyManager(strategies)

    def choose(self, market_data: dict[str, Any]) -> dict[str, Any]:
        return self._manager.choose(market_data)

    def run_all(self, market_data: dict[str, Any]) -> list[dict[str, Any]]:
        return self._manager.run(market_data)

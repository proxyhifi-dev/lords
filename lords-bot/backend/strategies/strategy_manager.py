from __future__ import annotations

from typing import Any, Protocol


class SignalStrategy(Protocol):
    def evaluate_signal(self, market_data: dict[str, Any]) -> dict[str, Any]:
        ...


class StrategyManager:
    """Runs one or many strategies and returns normalized signals."""

    def __init__(self, strategies: list[SignalStrategy] | None = None) -> None:
        self._strategies: list[SignalStrategy] = strategies or []

    def register(self, strategy: SignalStrategy) -> None:
        self._strategies.append(strategy)

    def run(self, market_data: dict[str, Any]) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        for strategy in self._strategies:
            result = strategy.evaluate_signal(market_data)
            outputs.append(
                {
                    'strategy': strategy.__class__.__name__,
                    'signal': result.get('signal', 'HOLD'),
                    'reason': result.get('reason', 'NA'),
                    'entry_price': float(result.get('entry_price') or 0.0),
                    'stop_loss': float(result.get('stop_loss') or 0.0),
                    'target_price': float(result.get('target_price') or 0.0),
                    'option_side': result.get('option_side', ''),
                }
            )
        return outputs

    def choose(self, market_data: dict[str, Any]) -> dict[str, Any]:
        """Prioritize first BUY signal; otherwise return first HOLD/NO TRADE signal."""
        results = self.run(market_data)
        for result in results:
            if str(result.get('signal', '')).upper() in {'BUY', 'BUY CALL', 'BUY PUT'}:
                return result
        return results[0] if results else {
            'strategy': 'None',
            'signal': 'HOLD',
            'reason': 'NO_STRATEGY_REGISTERED',
            'entry_price': 0.0,
            'stop_loss': 0.0,
            'target_price': 0.0,
            'option_side': '',
        }

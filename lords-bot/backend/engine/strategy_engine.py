from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class Strategy(Protocol):
    name: str

    def evaluate_signal(self, market_data: dict[str, Any]) -> dict[str, Any]:
        ...


@dataclass
class SignalEnvelope:
    strategy: str
    signal: str
    reason: str
    entry_price: float
    stop_loss: float
    target_price: float
    option_side: str


class StrategyEngine:
    def __init__(self, strategies: list[Strategy] | None = None) -> None:
        self._strategies = strategies or []

    def register(self, strategy: Strategy) -> None:
        self._strategies.append(strategy)

    def evaluate(self, market_data: dict[str, Any]) -> list[SignalEnvelope]:
        output: list[SignalEnvelope] = []
        for strategy in self._strategies:
            signal = strategy.evaluate_signal(market_data)
            output.append(
                SignalEnvelope(
                    strategy=strategy.name,
                    signal=str(signal.get('signal', 'HOLD')),
                    reason=str(signal.get('reason', 'NA')),
                    entry_price=float(signal.get('entry_price') or 0.0),
                    stop_loss=float(signal.get('stop_loss') or 0.0),
                    target_price=float(signal.get('target_price') or 0.0),
                    option_side=str(signal.get('option_side') or ''),
                )
            )
        return output

    def choose(self, market_data: dict[str, Any]) -> dict[str, Any]:
        for signal in self.evaluate(market_data):
            if signal.signal.upper() in {'BUY', 'BUY CALL', 'BUY PUT'}:
                return signal.__dict__
        return self.evaluate(market_data)[0].__dict__ if self._strategies else {
            'strategy': 'None',
            'signal': 'HOLD',
            'reason': 'NO_STRATEGY_REGISTERED',
            'entry_price': 0.0,
            'stop_loss': 0.0,
            'target_price': 0.0,
            'option_side': '',
        }

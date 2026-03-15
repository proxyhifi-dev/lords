from __future__ import annotations

from typing import Any

from strategies.orb_strategy import OrbStrategy


class StrategyEngine:
    def __init__(self) -> None:
        self.orb = OrbStrategy()

    def run(
        self,
        spot: float,
        orb_high: float,
        orb_low: float,
        option_chain_bias: str,
        candles: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.orb.generate(spot, orb_high, orb_low, option_chain_bias, candles).__dict__


strategy_engine = StrategyEngine()

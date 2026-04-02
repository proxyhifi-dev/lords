from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TradeSignal:
    action: str
    reason: str
    strike: int
    option_type: str
    qty: int


class OrbStrategy:
    def __init__(self) -> None:
        self._orb_high: float | None = None
        self._orb_low: float | None = None

    def set_orb(self, orb_high: float, orb_low: float) -> None:
        self._orb_high = orb_high
        self._orb_low = orb_low

    def on_tick(self, spot_price: float, qty: int) -> TradeSignal | None:
        if self._orb_high is None or self._orb_low is None:
            return None
        strike = int(round(spot_price / 50.0) * 50)
        if spot_price > self._orb_high:
            return TradeSignal(action='BUY', reason='orb_breakout_up', strike=strike, option_type='CE', qty=qty)
        if spot_price < self._orb_low:
            return TradeSignal(action='BUY', reason='orb_breakout_down', strike=strike, option_type='PE', qty=qty)
        return None

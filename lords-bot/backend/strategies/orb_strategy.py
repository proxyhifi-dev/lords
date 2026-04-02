from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TradeSignal:
    action: str
    reason: str
    strike: int
    option_type: str
    qty: int


@dataclass
class Signal:
    signal: str
    reason: str
    entry_price: float
    stop_loss: float
    target_price: float
    option_side: str


class OrbStrategy:
    name = 'ORB'


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

    def generate(self, spot: float, orb_high: float, orb_low: float, bias: str, candles: list[dict] | None = None) -> Signal:
        if orb_high <= 0 or orb_low <= 0:
            return Signal('HOLD', 'ORB_NOT_READY', spot, 0.0, 0.0, '')
        if spot > orb_high and bias in {'BULLISH', 'NEUTRAL'}:
            return Signal('BUY CALL', 'ORB_BREAKOUT_UP', spot, max(spot * 0.99, orb_low), spot * 1.02, 'CALL')
        if spot < orb_low and bias in {'BEARISH', 'NEUTRAL'}:
            return Signal('BUY PUT', 'ORB_BREAKOUT_DOWN', spot, min(spot * 1.01, orb_high), spot * 0.98, 'PUT')
        return Signal('HOLD', 'NO_BREAKOUT', spot, 0.0, 0.0, '')

    def evaluate_signal(self, market_data: dict) -> dict:
        signal = self.generate(
            float(market_data.get('spot_price') or 0.0),
            float(market_data.get('orb_high') or 0.0),
            float(market_data.get('orb_low') or 0.0),
            str(market_data.get('option_chain_bias') or 'NEUTRAL').upper(),
            market_data.get('candles') or [],
        )
        return {
            'signal': 'BUY' if signal.option_side else signal.signal,
            'reason': signal.reason,
            'entry_price': signal.entry_price,
            'stop_loss': signal.stop_loss,
            'target_price': signal.target_price,
            'option_side': signal.option_side,
        }

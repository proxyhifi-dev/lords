from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def compute_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        elif diff < 0:
            losses += abs(diff)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


@dataclass
class OrbSignal:
    signal: str
    option_side: str
    reason: str
    entry_price: float
    stop_loss: float
    target_price: float


class OrbStrategy:
    def generate_signal(self, market_data: dict[str, Any]) -> dict[str, Any]:
        signal = self.generate(
            spot_price=float(market_data.get('spot_price') or 0.0),
            orb_high=float(market_data.get('orb_high') or 0.0),
            orb_low=float(market_data.get('orb_low') or 0.0),
            option_chain_bias=str(market_data.get('option_chain_bias') or 'NEUTRAL'),
            candles=list(market_data.get('candles') or []),
        )
        return {
            'signal': signal.signal,
            'reason': signal.reason,
            'entry_price': signal.entry_price,
            'stop_loss': signal.stop_loss,
            'target_price': signal.target_price,
            'option_side': signal.option_side,
        }

    def generate(self, spot_price: float, orb_high: float, orb_low: float, option_chain_bias: str, candles: list[dict[str, Any]]) -> OrbSignal:
        if orb_high <= 0 or orb_low <= 0 or orb_high <= orb_low:
            return OrbSignal('HOLD', '', 'INVALID_ORB_RANGE', spot_price, 0.0, 0.0)

        closes = [float(c.get('close') or 0.0) for c in candles if float(c.get('close') or 0) > 0]
        rsi = compute_rsi(closes)
        last_close = closes[-1] if closes else spot_price
        previous_close = closes[-2] if len(closes) > 1 else last_close

        breakout_up = previous_close <= orb_high < last_close and spot_price >= orb_high
        breakout_down = previous_close >= orb_low > last_close and spot_price <= orb_low

        if breakout_up and option_chain_bias in {'BULLISH', 'NEUTRAL'} and rsi >= 55:
            stop_loss = orb_low
            risk = abs(spot_price - stop_loss)
            return OrbSignal('BUY CALL', 'CALL', f'ORB_BREAKOUT_UP;rsi={rsi:.2f}', spot_price, stop_loss, spot_price + (risk * 1.5))

        if breakout_down and option_chain_bias in {'BEARISH', 'NEUTRAL'} and rsi <= 45:
            stop_loss = orb_high
            risk = abs(spot_price - stop_loss)
            return OrbSignal('BUY PUT', 'PUT', f'ORB_BREAKOUT_DOWN;rsi={rsi:.2f}', spot_price, stop_loss, spot_price - (risk * 1.5))

        return OrbSignal('HOLD', '', f'NO_BREAKOUT;rsi={rsi:.2f}', spot_price, 0.0, 0.0)

    def should_exit(self, spot_price: float, signal: OrbSignal, rsi: float) -> bool:
        if signal.signal not in {'BUY CALL', 'BUY PUT'}:
            return False
        if signal.option_side == 'CALL':
            return spot_price <= signal.stop_loss or spot_price >= signal.target_price or rsi >= 70
        if signal.option_side == 'PUT':
            return spot_price >= signal.stop_loss or spot_price <= signal.target_price or rsi <= 30
        return False

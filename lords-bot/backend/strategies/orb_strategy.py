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

        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)

    if losses == 0:
        return 100.0

    rs = (gains / period) / (losses / period)

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

    def generate(
        self,
        spot_price: float,
        orb_high: float,
        orb_low: float,
        option_chain_bias: str,
        candles: list[dict[str, Any]],
    ) -> OrbSignal:

        closes = [float(c["close"]) for c in candles]

        rsi = compute_rsi(closes)

        if spot_price > orb_high and option_chain_bias in {"BULLISH", "NEUTRAL"} and rsi >= 55:

            risk = max(1.0, spot_price - orb_low)

            target = spot_price + risk * 1.5

            return OrbSignal(
                "BUY",
                "CALL",
                f"ORB_BREAKOUT_UP;rsi={rsi:.2f}",
                spot_price,
                orb_low,
                target,
            )

        if spot_price < orb_low and option_chain_bias in {"BEARISH", "NEUTRAL"} and rsi <= 45:

            risk = max(1.0, orb_high - spot_price)

            target = spot_price - risk * 1.5

            return OrbSignal(
                "BUY",
                "PUT",
                f"ORB_BREAKOUT_DOWN;rsi={rsi:.2f}",
                spot_price,
                orb_high,
                target,
            )

        return OrbSignal("HOLD", "", f"NO_BREAKOUT;rsi={rsi:.2f}", spot_price, 0.0, 0.0)

    def should_exit(self, spot_price: float, signal: OrbSignal, rsi: float) -> bool:

        if signal.signal != "BUY":
            return False

        if signal.option_side == "CALL":

            if (
                spot_price <= signal.stop_loss
                or spot_price >= signal.target_price
                or rsi >= 70
            ):
                return True

        if signal.option_side == "PUT":

            if (
                spot_price >= signal.stop_loss
                or spot_price <= signal.target_price
                or rsi <= 30
            ):
                return True

        return False
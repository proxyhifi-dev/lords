import time
from collections import deque
from typing import Any


class TickEngine:
    def __init__(self, buffer_size: int = 50) -> None:
        self._last_price: float | None = None
        self.ticks: deque[dict[str, Any]] = deque(maxlen=buffer_size)

    def on_quote(self, quote: dict[str, Any]) -> dict[str, Any] | None:
        price = (
            quote.get("lastTradedPrice")
            or quote.get("ltp")
            or quote.get("close")
            or quote.get("last_traded_price")
        )
        if price is None:
            return None

        price = float(price)
        if self._last_price is not None and self._last_price == price:
            return None

        tick = {
            "symbol": "NIFTY",
            "price": price,
            "timestamp": time.time(),
            "volume": quote.get("volume", 0),
        }
        self._last_price = price
        self.ticks.append(tick)
        return tick

    def last_n(self, n: int) -> list[dict[str, Any]]:
        return list(self.ticks)[-n:]

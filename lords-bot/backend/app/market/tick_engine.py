from __future__ import annotations

from backend.app.core.event_bus import EventBus


class TickEngine:
    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self._last_price: float | None = None

    async def run(self) -> None:
        queue = self.event_bus.subscribe("MARKET_QUOTE")
        async for event in self.event_bus.iter_events(queue):
            quote = event.payload["quote"]
            raw = quote.get("lastTradedPrice") or quote.get("ltp") or quote.get("close") or quote.get("last_traded_price")
            if raw is None:
                continue
            price = float(raw)
            if self._last_price == price:
                continue
            self._last_price = price
            await self.event_bus.publish(
                "TICK",
                {
                    "symbol": "NIFTY",
                    "price": price,
                    "volume": float(quote.get("volume") or 0),
                },
            )

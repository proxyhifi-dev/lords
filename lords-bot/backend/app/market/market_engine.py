import time
from typing import Any

from backend.app.broker.samco_client import SamcoClient
from backend.config import settings


class MarketEngine:
    def __init__(self, samco_client: SamcoClient) -> None:
        self.samco_client = samco_client
        self.last_spot_price: float | None = None
        self.last_quote: dict[str, Any] = {}

    @staticmethod
    def _extract_price(quote: dict[str, Any]) -> float:
        for key in ("lastTradedPrice", "ltp", "last_traded_price", "close"):
            value = quote.get(key)
            if value is not None:
                return float(value)
        raise ValueError(f"Quote missing price field: {quote}")

    def poll_quote(self) -> dict[str, Any]:
        quote = self.samco_client.get_quote(
            symbol_name=settings.nifty_symbol,
            exchange=settings.nifty_exchange,
        )
        self.last_quote = quote
        self.last_spot_price = self._extract_price(quote)
        return quote

    def run_poll_loop(self, on_quote) -> None:
        while True:
            quote = self.poll_quote()
            on_quote(quote)
            time.sleep(settings.poll_seconds)

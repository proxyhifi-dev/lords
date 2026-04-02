from __future__ import annotations

import asyncio

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.event_bus import EventBus
from backend.app.utils.logger import get_logger
from backend.config import settings


class MarketEngine:
    def __init__(self, event_bus: EventBus, samco_client: SamcoClient) -> None:
        self.event_bus = event_bus
        self.samco_client = samco_client
        self.logger = get_logger("market_engine")

    async def run(self) -> None:
        while True:
            quote = await self.samco_client.get_quote(
                symbol_name=settings.nifty_symbol,
                exchange=settings.nifty_exchange,
            )
            await self.event_bus.publish("MARKET_QUOTE", {"quote": quote})
            await asyncio.sleep(settings.poll_seconds)

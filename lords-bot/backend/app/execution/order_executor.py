from __future__ import annotations

from datetime import UTC, datetime

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.event_bus import EventBus
from backend.app.strategy.option_selector import OptionSelector
from backend.app.utils.logger import get_logger
from backend.config import settings


class OrderExecutor:
    def __init__(self, event_bus: EventBus, broker: SamcoClient, option_selector: OptionSelector) -> None:
        self.event_bus = event_bus
        self.broker = broker
        self.option_selector = option_selector
        self.logger = get_logger("order_executor")

    async def run(self) -> None:
        queue = self.event_bus.subscribe("RISK_APPROVED")
        async for event in self.event_bus.iter_events(queue):
            signal = event.payload["signal"]
            spot = float(event.payload["spot_price"])
            symbol = self.option_selector.select_option_symbol(spot=spot, signal=signal)
            order = await self.broker.place_order(symbol=symbol, side="BUY", quantity=settings.order_qty)
            trade = {
                "symbol": symbol,
                "signal": signal,
                "entry_price": spot,
                "entry_time": datetime.now(UTC).isoformat(),
                "order": order,
                "status": "ENTERED",
            }
            self.logger.info("Order placed symbol=%s response=%s", symbol, order)
            await self.event_bus.publish("ORDER_PLACED", {"trade": trade})

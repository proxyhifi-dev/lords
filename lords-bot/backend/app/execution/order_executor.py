from typing import Any

from backend.app.broker.samco_client import SamcoClient
from backend.app.utils.logger import get_logger
from backend.config import settings


class OrderExecutor:
    def __init__(self, samco_client: SamcoClient) -> None:
        self.samco_client = samco_client
        self.logger = get_logger("order_executor")

    def place_entry(self, symbol: str) -> dict[str, Any]:
        order = self.samco_client.place_order(symbol=symbol, quantity=settings.quantity, side="BUY")
        self.logger.info("Entry order placed symbol=%s order=%s", symbol, order)
        return order

    def place_exit(self, symbol: str) -> dict[str, Any]:
        order = self.samco_client.place_order(symbol=symbol, quantity=settings.quantity, side="SELL")
        self.logger.info("Exit order placed symbol=%s order=%s", symbol, order)
        return order

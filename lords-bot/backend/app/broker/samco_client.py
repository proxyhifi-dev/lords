import time
from typing import Any, Callable

from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge

from backend.app.utils.logger import get_logger
from backend.config import settings


class SamcoClient:
    """Thin wrapper around official Samco Python SDK only."""

    def __init__(self) -> None:
        self.logger = get_logger("samco_client")
        self.samco = StocknoteAPIPythonBridge()

    def _with_retry(self, fn: Callable[[], Any], api_name: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                return fn()
            except Exception as exc:  # network/API edge handling
                last_error = exc
                self.logger.error("%s failed attempt=%s error=%s", api_name, attempt, exc)
                time.sleep(0.5 * attempt)
        raise RuntimeError(f"{api_name} failed after retries: {last_error}")

    def login(self) -> dict[str, Any]:
        def call() -> dict[str, Any]:
            return self.samco.login(
                body={
                    "userId": settings.samco_user_id,
                    "password": settings.samco_password,
                    "yob": settings.samco_yob,
                }
            )

        response = self._with_retry(call, "login")
        token = response.get("sessionToken")
        if not token:
            raise RuntimeError(f"Samco login missing sessionToken: {response}")
        self.samco.set_session_token(token)
        self.logger.info("Samco login successful")
        return response

    def get_quote(self, symbol_name: str, exchange: str) -> dict[str, Any]:
        return self._with_retry(
            lambda: self.samco.get_quote(symbol_name=symbol_name, exchange=exchange),
            "get_quote",
        )

    def get_option_chain(self, symbol_name: str, exchange: str, strike_price: int, expiry_date: str) -> dict[str, Any]:
        return self._with_retry(
            lambda: self.samco.get_option_chain(
                symbol_name=symbol_name,
                exchange=exchange,
                strike_price=strike_price,
                expiry_date=expiry_date,
            ),
            "get_option_chain",
        )

    def place_order(self, symbol: str, quantity: int, side: str = "BUY") -> dict[str, Any]:
        transaction_type = (
            self.samco.TRANSACTION_TYPE_BUY if side.upper() == "BUY" else self.samco.TRANSACTION_TYPE_SELL
        )

        return self._with_retry(
            lambda: self.samco.place_order(
                body={
                    "symbolName": symbol,
                    "exchange": self.samco.EXCHANGE_NFO,
                    "transactionType": transaction_type,
                    "orderType": self.samco.ORDER_TYPE_MARKET,
                    "quantity": quantity,
                    "productType": self.samco.PRODUCT_MIS,
                }
            ),
            "place_order",
        )

    def get_positions(self) -> dict[str, Any]:
        return self._with_retry(lambda: self.samco.get_positions(), "get_positions")

    def get_orders(self) -> dict[str, Any]:
        if hasattr(self.samco, "get_orders"):
            return self._with_retry(lambda: self.samco.get_orders(), "get_orders")
        return self._with_retry(lambda: self.samco.get_order_book(), "get_order_book")

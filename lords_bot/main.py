import asyncio
import logging
import os

from app.account_service import AccountService
from app.auth import AuthService
from app.fyers_client import FyersClient
from app.order_service import OrderService
from app.utils import configure_logging


async def bootstrap() -> None:
    configure_logging()
    logger = logging.getLogger("lords_bot")

    auth_code = os.getenv("FYERS_AUTH_CODE")
    if not auth_code:
        raise RuntimeError("Set FYERS_AUTH_CODE in environment for first-time login.")

    auth = AuthService()
    await auth.validate_auth_code(auth_code)

    client = FyersClient(auth)
    account_service = AccountService(client)
    order_service = OrderService(client)

    profile = await account_service.get_profile()
    funds = await account_service.get_funds()

    logger.info("Profile response: %s", profile)
    logger.info("Funds response: %s", funds)

    sample_order = {
        "symbol": "NSE:SBIN-EQ",
        "qty": 1,
        "type": 2,
        "side": 1,
        "productType": "INTRADAY",
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
        "stopLoss": 0,
        "takeProfit": 0,
        "orderTag": "lords-bot",
    }

    # Uncomment after validating credentials and live trading restrictions.
    # order_response = await order_service.place_order(sample_order)
    # logger.info("Order response: %s", order_response)
    _ = order_service
    _ = sample_order


if __name__ == "__main__":
    asyncio.run(bootstrap())

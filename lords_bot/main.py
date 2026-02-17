import asyncio
import logging

from app.account_service import AccountService
from app.auth import AuthService
from app.fyers_client import FyersClient
from app.order_service import OrderService
from app.utils import configure_logging


async def bootstrap() -> None:
    configure_logging()
    logger = logging.getLogger("lords_bot")

    logger.info("Starting Lords Bot...")

    # -------------------------
    # Autonomous Login System
    # -------------------------
    auth = AuthService()
    await auth.auto_login()

    logger.info("Authentication successful")

    # -------------------------
    # Initialize Client
    # -------------------------
    client = FyersClient(auth)
    account_service = AccountService(client)
    order_service = OrderService(client)

    # -------------------------
    # Fetch Account Data
    # -------------------------
    profile = await account_service.get_profile()
    funds = await account_service.get_funds()

    logger.info("Profile response: %s", profile)
    logger.info("Funds response: %s", funds)

    # -------------------------
    # Sample Order Template
    # -------------------------
    sample_order = {
        "symbol": "NSE:SBIN-EQ",
        "qty": 1,
        "type": 2,  # Market Order
        "side": 1,  # Buy
        "productType": "INTRADAY",
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
        "stopLoss": 0,
        "takeProfit": 0,
        "orderTag": "lordsbot",
        "isSliceOrder": False,
    }

    logger.info("Sample order prepared but not executed.")

    # Uncomment only when ready for live trading
    # order_response = await order_service.place_order(sample_order)
    # logger.info("Order response: %s", order_response)

    logger.info("Lords Bot initialized successfully.")


if __name__ == "__main__":
    asyncio.run(bootstrap())

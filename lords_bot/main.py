import asyncio
import os
import uvicorn

from app.auth import AuthService
from app.fyers_client import FyersClient
from app.order_service import OrderService
from ui.server import create_ui_app


async def bootstrap():

    auth = AuthService()
    await auth.auto_login()

    client = FyersClient(auth)
    order_service = OrderService(client)

    trading_mode = os.getenv("TRADING_MODE", "PAPER")

    ui_app = create_ui_app(client, order_service, trading_mode)

    config = uvicorn.Config(
        ui_app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
    )

    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(bootstrap())

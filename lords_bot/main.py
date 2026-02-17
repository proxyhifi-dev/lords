from __future__ import annotations

import asyncio
import os

import uvicorn

from lords_bot.app.auth import AuthService
from lords_bot.app.order_service import OrderService
from lords_bot.app.fyers_client import FyersClient
from lords_bot.app.utils import configure_logging
from lords_bot.ui.server import create_ui_app


async def bootstrap() -> None:
    configure_logging()

    auth = AuthService()
    await auth.auto_login()

    client = FyersClient(auth)
    order_service = OrderService(client)
    trading_mode = os.getenv("TRADING_MODE", "PAPER").upper()

    ui_app = create_ui_app(client=client, order_service=order_service, trading_mode=trading_mode)

    config = uvicorn.Config(ui_app, host="127.0.0.1", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    asyncio.run(bootstrap())


if __name__ == "__main__":
    main()

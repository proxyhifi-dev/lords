from __future__ import annotations
import asyncio
import contextlib
import logging
import uvicorn

from lords_bot.app.auth import AuthService
from lords_bot.app.fyers_client import FyersClient
from lords_bot.app.order_service import OrderService
from lords_bot.app.risk_engine import RiskEngine
from lords_bot.app.polling_service import PollingService
from lords_bot.app.utils import configure_logging
from lords_bot.strategies.orb_strategy import ORBStrategy
from lords_bot.ui.server import create_ui_app


logger = logging.getLogger("lords_bot.main")


async def bootstrap() -> None:
    configure_logging()

    # 🔐 AUTH
    auth = AuthService()
    await auth.auto_login()

    # 🔗 CLIENT
    client = FyersClient(auth)

    # 🧠 SERVICES
    order_service = OrderService(client)
    strategy = ORBStrategy(client)
    risk_engine = RiskEngine(client)

    await risk_engine.reconcile_positions_on_startup()

    # 🔁 REST POLLING (1 second)
    polling = PollingService(client)

    async def handle_tick(ltp: float) -> None:
        await strategy.on_new_tick(ltp)
        signal = await strategy.check_breakout()
        if signal:
            logger.info("Breakout signal: %s", signal)

    await polling.start(
        symbol="NSE:NIFTY50-INDEX",
        interval=1.0,
        on_tick=handle_tick,
    )

    # 🌐 UI
    ui_app = create_ui_app(
        client=client,
        order_service=order_service,
        trading_mode=client.settings.trading_mode,
    )

    config = uvicorn.Config(ui_app, host="127.0.0.1", port=8080, log_level="info")
    server = uvicorn.Server(config)

    try:
        logger.info("Starting UI server...")
        await server.serve()
    finally:
        logger.info("Shutting down services...")
        await polling.stop()
        with contextlib.suppress(Exception):
            await risk_engine.square_off_and_shutdown()


def main() -> None:
    asyncio.run(bootstrap())


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import contextlib

import uvicorn

from lords_bot.app.auth import AuthService
from lords_bot.app.fyers_client import FyersClient
from lords_bot.app.order_service import OrderService
from lords_bot.app.risk_engine import RiskEngine
from lords_bot.app.utils import configure_logging
from lords_bot.app.websocket_service import WebsocketService
from lords_bot.strategies.orb_strategy import ORBStrategy
from lords_bot.ui.server import create_ui_app


async def bootstrap() -> None:
    configure_logging()

    auth = AuthService()
    await auth.auto_login()

    client = FyersClient(auth)
    order_service = OrderService(client)
    strategy = ORBStrategy(client)
    risk_engine = RiskEngine(client, order_service, trading_mode=client.settings.trading_mode)
    await risk_engine.reconcile_positions_on_startup()

    ws_service = WebsocketService(client)

    async def handle_tick(ltp: float) -> None:
        await strategy.on_new_tick(ltp)

    async def circuit_reset_scheduler() -> None:
        while True:
            await asyncio.sleep(300)
            if client.is_trading_paused() and client.trading_pause_remaining_seconds <= 0:
                client.reset_circuit_breaker()

    await ws_service.start(["NSE:NIFTY50-INDEX"], handle_tick)
    cb_task = asyncio.create_task(circuit_reset_scheduler())

    ui_app = create_ui_app(
        client=client,
        order_service=order_service,
        trading_mode=client.settings.trading_mode,
        strategy=strategy,
        risk_engine=risk_engine,
    )

    config = uvicorn.Config(ui_app, host="127.0.0.1", port=8000, log_level="info")
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        await ws_service.stop()
        with contextlib.suppress(Exception):
            await risk_engine.square_off_and_shutdown()
        cb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cb_task


def main() -> None:
    asyncio.run(bootstrap())


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import contextlib
import logging

import uvicorn

from lords_bot.app.auth import AuthService
from lords_bot.app.fyers_client import FyersClient
from lords_bot.app.order_service import OrderService
from lords_bot.app.risk_engine import RiskEngine
from lords_bot.app.utils import configure_logging
from lords_bot.app.websocket_service import WebsocketService
from lords_bot.strategies.orb_strategy import ORBStrategy
from lords_bot.ui.server import create_ui_app

logger = logging.getLogger("lords_bot.main")


async def bootstrap() -> None:
    """Initialize services and start WebSocket + UI server."""

    # Enable logging
    configure_logging()

    # ---------- AUTHENTICATION ----------
    auth = AuthService()
    await auth.auto_login()

    # ---------- CORE CLIENT ----------
    client = FyersClient(auth)

    # Order service
    order_service = OrderService(client)

    # Strategy
    strategy = ORBStrategy(client)

    # Risk engine
    risk_engine = RiskEngine(client)
    await risk_engine.reconcile_positions_on_startup()

    # ---------- WEBSOCKET ----------
    ws_service = WebsocketService(client)

    async def handle_tick(msg: dict[str, float]) -> None:
        """
        Called on each WebSocket tick.
        """
        try:
            await strategy.on_new_tick(msg)
        except Exception as exc:
            logger.error("ORBStrategy tick handler failed: %s", exc)

    # Background scheduler to reset circuit breaker
    async def circuit_reset_scheduler() -> None:
        while True:
            try:
                await asyncio.sleep(300)
                if client.is_trading_paused() and client.trading_pause_remaining_seconds <= 0:
                    client.reset_circuit_breaker()
                    logger.info("Circuit breaker auto reset.")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Circuit reset scheduler error: %s", exc)

    # Start background tasks
    ws_task = asyncio.create_task(
        ws_service.start(["NSE:NIFTY50-INDEX"], handle_tick)
    )
    cb_task = asyncio.create_task(circuit_reset_scheduler())

    # ---------- UI SERVER ----------
    ui_app = create_ui_app(
        client=client,
        order_service=order_service,
        trading_mode=client.settings.trading_mode,
    )

    # Make strategy + risk_engine available inside UI handlers
    ui_app.state.strategy = strategy
    ui_app.state.risk_engine = risk_engine

    # Run ASGI server
    config = uvicorn.Config(
        ui_app,
        host="127.0.0.1",
        port=8080,
        log_level="info",
    )
    server = uvicorn.Server(config)

    try:
        logger.info("Starting UI server...")
        await server.serve()
    finally:
        logger.info("Shutting down services...")

        # Stop websocket gracefully
        try:
            await ws_service.stop()
        except Exception as exc:
            logger.error("WebSocket stop error: %s", exc)

        # Cancel scheduler
        cb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cb_task

        # Optional strategy/risk cleanup (if implemented)
        with contextlib.suppress(Exception):
            await risk_engine.square_off_and_shutdown()

        # Cancel ws task if still running
        ws_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ws_task


def main() -> None:
    """Program entrypoint wrapper."""
    asyncio.run(bootstrap())


if __name__ == "__main__":
    main()

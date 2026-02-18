from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Callable, Awaitable

import certifi
from websockets import connect, WebSocketException

logger = logging.getLogger("lords_bot.websocket")


class WebsocketService:
    """
    Production-ready FYERS WebSocket manager.
    Handles:
    - SSL verification
    - Authentication
    - Auto-reconnect with exponential backoff
    - Clean shutdown
    """

    def __init__(self, client) -> None:
        self.client = client
        self.ws_url = "wss://api.fyers.in/socket/v2/data/"
        self._task: asyncio.Task | None = None
        self._running = False

        # Proper SSL context using certifi CA bundle
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    async def start(
        self,
        symbols: list[str],
        on_tick: Callable[[dict], Awaitable[None]],
    ) -> None:
        """
        Starts WebSocket background loop.
        """
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(
            self._connect_loop(symbols, on_tick)
        )

    async def stop(self) -> None:
        """
        Gracefully stop websocket.
        """
        self._running = False
        if self._task:
            self._task.cancel()
            with asyncio.suppress(asyncio.CancelledError):
                await self._task

    async def _connect_loop(
        self,
        symbols: list[str],
        on_tick: Callable[[dict], Awaitable[None]],
    ) -> None:
        """
        Main reconnect loop.
        """
        delay = 1.0

        while self._running:
            try:
                await self._connect_once(symbols, on_tick)
                delay = 1.0  # reset delay after successful connection
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("WebSocket disconnected: %s", exc)

            if not self._running:
                break

            logger.info("Reconnecting WebSocket in %.1fs...", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _connect_once(
        self,
        symbols: list[str],
        on_tick: Callable[[dict], Awaitable[None]],
    ) -> None:
        """
        Single WebSocket connection session.
        """

        if not self.client.auth.access_token:
            logger.error("Cannot start WebSocket: no access token.")
            return

        headers = {
            "Authorization": f"{self.client.settings.fyers_app_id}:{self.client.auth.access_token}"
        }

        async with connect(
            self.ws_url,
            ssl=self.ssl_context,
            extra_headers=headers,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            logger.info("WebSocket connected.")

            await self._subscribe(ws, symbols)

            async for message in ws:
                await self._handle_message(message, on_tick)

    async def _subscribe(self, ws, symbols: list[str]) -> None:
        """
        Subscribe to symbols.
        """
        payload = {
            "type": "symbolList",
            "symbol": symbols,
        }

        await ws.send(json.dumps(payload))
        logger.info("Subscribed to symbols: %s", symbols)

    async def _handle_message(
        self,
        raw_message: str,
        on_tick: Callable[[dict], Awaitable[None]],
    ) -> None:
        """
        Parse incoming WebSocket message.
        """
        try:
            data = json.loads(raw_message)

            # FYERS format: {"d": [{"v": {"lp": 22150.25}}]}
            if isinstance(data.get("d"), list) and data["d"]:
                value_block = data["d"][0].get("v", {})
                ltp = value_block.get("lp") or value_block.get("ltP")

                if ltp is not None:
                    await on_tick({"ltp": float(ltp)})

        except Exception as exc:
            logger.warning("Failed to parse WS message: %s", exc)

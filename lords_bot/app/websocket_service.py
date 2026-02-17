import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import websockets

from lords_bot.app.auth import AuthService

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict], Awaitable[None]]


class WebsocketService:
    """Basic FYERS data websocket client wrapper."""

    def __init__(self, auth_service: AuthService, url: str = "wss://api.fyers.in/socket/v3/data"):
        self.auth = auth_service
        self.url = url
        self._running = False

    async def connect_and_listen(
        self,
        symbols: list[str],
        message_handler: MessageHandler,
    ) -> None:
        if not self.auth.access_token:
            raise RuntimeError("Missing access token. Authenticate before websocket connection.")

        self._running = True
        headers = {"Authorization": self.auth.access_token}

        async with websockets.connect(self.url, additional_headers=headers) as ws:
            await ws.send(
                json.dumps(
                    {
                        "T": "SUB_DATA",
                        "L2LIST": symbols,
                        "SUB_T": 1,
                    }
                )
            )
            logger.info("Subscribed to websocket symbols: %s", symbols)

            while self._running:
                raw = await ws.recv()
                await message_handler(json.loads(raw))

    async def stop(self) -> None:
        self._running = False
        await asyncio.sleep(0)

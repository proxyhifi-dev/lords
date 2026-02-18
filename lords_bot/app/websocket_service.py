from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

import websockets

from lords_bot.app.fyers_client import FyersClient

logger = logging.getLogger("lords_bot.websocket")
MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class WebsocketService:
    """Live market-data websocket with reconnect, SSL controls, and graceful degrade mode."""

    def __init__(self, client: FyersClient) -> None:
        self.client = client
        self._running = False
        self._task: asyncio.Task | None = None

        self.max_retry_delay = float(getattr(self.client.settings, "fyers_ws_max_retry_delay", 60.0) or 60.0)
        self.ssl_verify = bool(getattr(self.client.settings, "fyers_ws_ssl_verify", True))
        self.stop_on_ssl_error = bool(getattr(self.client.settings, "fyers_ws_stop_on_ssl_error", False))

    def _ssl_context(self) -> ssl.SSLContext | bool | None:
        if self.ssl_verify:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _subscribe(self, ws: websockets.WebSocketClientProtocol, symbols: list[str]) -> None:
        payload = {"T": "SUB_DATA", "L2LIST": symbols, "SUB_T": 1}
        await ws.send(json.dumps(payload))

    async def connect_and_listen(self, symbols: list[str], message_handler: MessageHandler) -> None:
        """Reconnect with bounded backoff; optionally stop after unrecoverable SSL misconfiguration."""
        self._running = True
        retry_delay = 1.0

        while self._running:
            if self.client.is_trading_paused():
                await asyncio.sleep(1)
                continue

            try:
                headers = {"Authorization": f"{self.client.settings.fyers_app_id}:{self.client.auth.access_token}"}
                async with websockets.connect(
                    self.client.data_ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ssl=self._ssl_context(),
                ) as ws:
                    await self._subscribe(ws, symbols)
                    logger.info("Websocket connected and subscribed: %s", symbols)
                    retry_delay = 1.0

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning("Dropping non-JSON websocket payload")
                            continue
                        await message_handler(data)
            except ssl.SSLCertVerificationError as exc:
                logger.error(
                    "Websocket SSL verification failed: %s. "
                    "If you are behind a corporate proxy/self-signed chain, set FYERS_WS_SSL_VERIFY=false.",
                    exc,
                )
                if self.stop_on_ssl_error:
                    logger.warning("Stopping websocket service due to SSL error (FYERS_WS_STOP_ON_SSL_ERROR=true).")
                    break
                await asyncio.sleep(self.max_retry_delay)
            except Exception as exc:  # noqa: BLE001 - keep service alive.
                logger.warning("Websocket disconnected; retrying in %.1fs (%s)", retry_delay, exc)
                await asyncio.sleep(retry_delay)
                retry_delay = min(self.max_retry_delay, retry_delay * 2)

    async def start(self, symbols: list[str], message_handler: MessageHandler) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.connect_and_listen(symbols, message_handler))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

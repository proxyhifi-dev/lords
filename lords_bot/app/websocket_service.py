from __future__ import annotations
import contextlib
import asyncio
import json
import logging
import ssl
from typing import Callable, Awaitable, TYPE_CHECKING

import certifi
from websockets import connect, WebSocketException

if TYPE_CHECKING:
    from lords_bot.app.fyers_client import FyersClient

logger = logging.getLogger("lords_bot.websocket")


class WebsocketService:
    """
    Websocket manager for FYERS live market data.

    Handles:
    - SSL verification with certifi
    - Authenticated connection
    - Exponential reconnect backoff
    - Graceful shutdown
    - LTP extraction per tick
    """

    def __init__(
        self,
        client: "FyersClient",
        on_message: Callable[[dict], None] = None,
        on_error: Callable[[str], None] = None,
        on_close: Callable[[str], None] = None,
        on_open: Callable[[], None] = None,
    ) -> None:
        self.client = client
        self._task: asyncio.Task | None = None
        self._running = False

        # Build SSL context from certifi bundle:
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

        # WebSocket URL from client settings (official FYERS v3: always wss://api.fyers.in/socket/v2/data/)
        self.ws_url = self.client.data_ws_url

        # User callbacks
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self.on_open = on_open

    async def start(
        self,
        symbols: list[str],
        on_tick: Callable[[dict[str, float]], Awaitable[None]],
    ) -> None:
        """
        Begin the WebSocket loop.
        This method returns immediately; internal loop runs as task.
        """
        if self._running:
            logger.warning("WebSocket already running.")
            return

        self._running = True
        self._task = asyncio.create_task(
            self._connect_loop(symbols, on_tick)
        )

    async def stop(self) -> None:
        """
        Stop WebSocket gracefully.
        """
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            logger.info("WebSocket stopped.")

    async def _connect_loop(
        self,
        symbols: list[str],
        on_tick: Callable[[dict[str, float]], Awaitable[None]],
    ) -> None:
        """
        Main reconnect logic with exponential backoff.
        """
        retry_delay = 1.0

        while self._running:
            try:
                await self._session_connect(symbols, on_tick)
                retry_delay = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("WebSocket disconnected: %s", exc)

            if not self._running:
                break

            logger.info("Reconnecting WebSocket in %.1fs ...", retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)

    async def _session_connect(
        self,
        symbols: list[str],
        on_tick: Callable[[dict[str, float]], Awaitable[None]],
    ) -> None:
        """
        Official FYERS v3 WebSocket session — clean connect, auth JSON, subscribe JSON.
        """
        if not self.client.auth.access_token:
            raise RuntimeError("WebSocket: missing access token")

        connection_url = self.ws_url  # Always wss://api.fyers.in/socket/v2/data/
        logger.info("WebSocket connecting to %s", connection_url)

        try:
            async with connect(
                connection_url,
                ssl=self.ssl_context,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                logger.info("WebSocket connected to FYERS.")
                if self.on_open:
                    self.on_open()

                # Send authorization JSON (official SDK style)
                await ws.send(json.dumps({
                    "authorization": f"{self.client.settings.fyers_app_id} {self.client.auth.access_token}"
                }))

                # Send subscription JSON (official SDK style)
                await ws.send(json.dumps({
                    "symbol": symbols,
                    "type": "symbolUpdate"
                }))
                logger.info("WebSocket subscribed to symbols: %s", symbols)

                async for raw_message in ws:
                    await self._handle_message(raw_message, on_tick)

        except Exception as exc:
            logger.warning("WebSocket error: %s", exc)
            if self.on_error:
                self.on_error(str(exc))
            raise
        finally:
            if self.on_close:
                self.on_close("WebSocket connection closed")

    async def _subscribe(self, ws, symbols: list[str]) -> None:
        """
        Send subscription request per FYERS WebSocket spec.
        """
        subscribe_payload = {
            "type": "symbolList",
            "symbol": symbols,
        }
        await ws.send(json.dumps(subscribe_payload))
        logger.info("WebSocket subscribed to symbols: %s", symbols)

    async def _handle_message(
        self,
        raw_message: str | bytes,
        on_tick: Callable[[dict[str, float]], Awaitable[None]],
    ) -> None:
        """
        Parse incoming WebSocket message and extract Last Traded Price.
        """
        try:
            # SAFETY FIX: Ensure we decode binary data if FYERS sends it
            if isinstance(raw_message, bytes):
                raw_message = raw_message.decode('utf-8')
                
            data = json.loads(raw_message)

            # FYERS d-array format
            # Example: {"d":[{"v":{"lp":12344.0,...}}],...}
            ltp: float | None = None

            if isinstance(data.get("d"), list) and data["d"]:
                v = data["d"][0].get("v", {})
                if isinstance(v, dict):
                    # 'lp' stands for last price
                    raw_ltp = v.get("lp") or v.get("ltP")
                    if raw_ltp is not None:
                        ltp = float(raw_ltp)

            if ltp is not None:
                await on_tick({"ltp": ltp})

            if self.on_message:
                self.on_message(data)

        except Exception as exc:
            logger.warning("WebSocket message parse failure: %s", exc)
            if self.on_error:
                self.on_error(f"Parse error: {exc}")
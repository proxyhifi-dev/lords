from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
from typing import Awaitable, Callable

import certifi
from websockets import connect

logger = logging.getLogger("lords_bot.websocket")


class WebsocketService:
    """Crash-safe FYERS data websocket with exponential reconnect."""

    def __init__(self, client) -> None:
        self.client = client
        self.ws_url = "wss://api.fyers.in/socket/v2/data/"
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self, symbols: list[str], on_tick: Callable[[float], Awaitable[None]]) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._connect_loop(symbols, on_tick))

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _connect_loop(self, symbols: list[str], on_tick: Callable[[float], Awaitable[None]]) -> None:
        delay = 1
        while self._running:
            try:
                await self._connect_once(symbols, on_tick)
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("WebSocket disconnected: %s", exc)

            if not self._running:
                break

            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _connect_once(self, symbols: list[str], on_tick: Callable[[float], Awaitable[None]]) -> None:
        token = getattr(self.client.auth, "access_token", None)
        if not token:
            logger.error("WebSocket start skipped: access token unavailable")
            return

        headers = {"Authorization": f"{self.client.settings.fyers_app_id}:{token}"}

        async with connect(
            self.ws_url,
            ssl=self.ssl_context,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            logger.info("WebSocket connected.")
            await ws.send(json.dumps({"type": "symbolList", "symbol": symbols}))

            async for raw_message in ws:
                ltp = self._extract_ltp(raw_message)
                if ltp is not None:
                    await on_tick(ltp)

    @staticmethod
    def _extract_ltp(raw_message: str) -> float | None:
        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            return None

        if not isinstance(data, dict):
            return None

        entries = data.get("d")
        if not isinstance(entries, list):
            return None

        for entry in entries:
            value = entry.get("v", {}) if isinstance(entry, dict) else {}
            raw_ltp = value.get("lp", value.get("ltP"))
            if raw_ltp is None:
                continue
            try:
                return float(raw_ltp)
            except (TypeError, ValueError):
                continue

        return None

from __future__ import annotations

import time as wall_time
from typing import Any

import httpx

from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.utils.logger import get_logger

logger = get_logger("telegram_notifier")
settings = get_settings()


CRITICAL_EVENTS: frozenset[str] = frozenset(
    {
        "SELL_FAILED_CRITICAL",
        "EXECUTION_UNCERTAIN",
        "ORDER_UNCERTAIN",
        "T1_PARTIAL_FILL_UNRECOVERED",
        "T2_PARTIAL_FILL_UNRECOVERED",
        "IC_QUOTE_DEGRADED_CRITICAL",
        "DIRECTIONAL_QUOTE_DEGRADED_CRITICAL",
    }
)

INFO_EVENTS: frozenset[str] = frozenset(
    {
        "TRADE_OPENED",
        "TRADE_CLOSED",
    }
)

DEDUP_WINDOW_SECONDS = 30
SEND_TIMEOUT_SECONDS = 10.0


class TelegramNotifier:
    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self.bot_token = str(getattr(settings, "telegram_bot_token", "") or "").strip()
        self.chat_id = str(getattr(settings, "telegram_chat_id", "") or "").strip()
        self._last_sent: dict[str, float] = {}
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def run(self) -> None:
        if not self.enabled:
            logger.info("Telegram notifier disabled (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set)")
            return

        logger.info("Telegram notifier started chat=%s", self._mask(self.chat_id))
        self._client = httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS)

        queue = self.event_bus.subscribe()

        try:
            await self._send(
                "[INFO] LORDS bot started\n"
                f"  mode: {str(getattr(settings, 'mode', 'paper')).upper()}\n"
                f"  strategy: {str(getattr(settings, 'strategy_type', '')).upper()}"
            )

            async for event in self.event_bus.iter_events(queue):
                await self._handle_event(event)
        finally:
            if self._client is not None:
                try:
                    await self._client.aclose()
                except Exception as exc:
                    logger.warning("Telegram client close failed: %s", exc)
            self._client = None

    async def _handle_event(self, event: Any) -> None:
        event_type = str(getattr(event, "type", "") or "")
        if not event_type:
            return

        is_critical = event_type in CRITICAL_EVENTS
        is_info = event_type in INFO_EVENTS
        if not (is_critical or is_info):
            return

        now = wall_time.time()
        last = self._last_sent.get(event_type, 0.0)
        if now - last < DEDUP_WINDOW_SECONDS:
            return
        self._last_sent[event_type] = now

        prefix = "[CRITICAL]" if is_critical else "[INFO]"
        await self._send(self._format_message(prefix, event_type, getattr(event, "payload", None) or {}))

    @staticmethod
    def _format_message(prefix: str, event_type: str, payload: dict[str, Any]) -> str:
        lines = [f"{prefix} {event_type}"]
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                continue
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)

    async def _send(self, text: str) -> None:
        if self._client is None:
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            resp = await self._client.post(
                url,
                json={"chat_id": self.chat_id, "text": text},
            )
            if resp.status_code != 200:
                logger.warning(
                    "Telegram send failed status=%d body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception as exc:
            logger.warning("Telegram send error: %s", exc)

    @staticmethod
    def _mask(value: str) -> str:
        if len(value) <= 4:
            return "***"
        return value[:2] + "***" + value[-2:]

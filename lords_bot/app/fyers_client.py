from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx

from lords_bot.app.config import get_settings
from lords_bot.app.auth import AuthService

api_logger = logging.getLogger("lords_bot.api")
retry_logger = logging.getLogger("lords_bot.retry")
circuit_logger = logging.getLogger("lords_bot.circuit")


# ==============================
# Custom Exception
# ==============================

class FyersAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ==============================
# Circuit Breaker State
# ==============================

@dataclass(slots=True)
class CircuitState:
    opened_until: float = 0.0
    failures: int = 0


# ==============================
# Fyers Client
# ==============================

class FyersClient:
    """
    Production-safe FYERS v3 Client

    ✔ Correct REST routing
    ✔ Retry with exponential backoff
    ✔ Circuit breaker protection
    ✔ Auto re-login on 401
    ✔ Clean WebSocket URLs
    """

    def __init__(self, auth_service: AuthService) -> None:
        self.settings = get_settings()
        self.auth = auth_service

        # Retry config
        self.retry_statuses = {429, 500, 502, 503, 504}
        self.max_retries = getattr(self.settings, "fyers_max_retries", 3)
        self.base_backoff = getattr(self.settings, "fyers_retry_backoff_seconds", 0.5)
        self.max_backoff = getattr(self.settings, "fyers_max_backoff_seconds", 8.0)

        # Circuit breaker config
        self.failure_threshold = getattr(self.settings, "api_failure_threshold", 5)
        self.failure_window = getattr(self.settings, "api_failure_window_seconds", 60)
        self.pause_seconds = getattr(self.settings, "api_pause_seconds", 120)

        self._state = CircuitState()
        self._failure_timestamps: deque[float] = deque()

    # =====================================
    # FIXED PRODUCTION REST ROUTING
    # =====================================

    def _resolve_base_url(self, endpoint: str) -> str:
        """
        Route endpoints correctly for FYERS v3 production.
        """
        ep = endpoint.lstrip("/")

        # Market data endpoints
        if ep.startswith(("history", "quotes", "optionchain", "symbol_master", "market_depth")):
            return "https://api.fyers.in/data-rest/v3"

        # Trading endpoints
        return "https://api.fyers.in/api/v3"

    # =====================================
    # WebSocket Endpoints (Production)
    # =====================================

    @property
    def data_ws_url(self) -> str:
        return "wss://api.fyers.in/socket/v2/data/"

    @property
    def order_ws_url(self) -> str:
        return "wss://api.fyers.in/socket/v2/order/"

    @property
    def position_ws_url(self) -> str:
        return "wss://api.fyers.in/socket/v2/position/"

    @property
    def trade_ws_url(self) -> str:
        return "wss://api.fyers.in/socket/v2/trade/"

    # =====================================
    # Auth Header
    # =====================================

    def _auth_header(self) -> dict[str, str]:
        if not self.auth.access_token:
            raise FyersAPIError("Access token missing. Please login first.")

        return {
            "Authorization": f"{self.settings.fyers_app_id}:{self.auth.access_token}",
            "Content-Type": "application/json",
        }

    # =====================================
    # Circuit Breaker Logic
    # =====================================

    def is_trading_paused(self) -> bool:
        return time.time() < self._state.opened_until

    @property
    def trading_pause_remaining_seconds(self) -> int:
        return max(0, int(self._state.opened_until - time.time()))

    def _record_success(self) -> None:
        self._state.failures = 0
        self._failure_timestamps.clear()

    def _record_failure(self) -> None:
        now = time.time()
        self._failure_timestamps.append(now)

        while self._failure_timestamps and now - self._failure_timestamps[0] > self.failure_window:
            self._failure_timestamps.popleft()

        self._state.failures = len(self._failure_timestamps)

        if self._state.failures >= self.failure_threshold:
            self._state.opened_until = now + self.pause_seconds
            self._failure_timestamps.clear()
            circuit_logger.error(
                "Circuit opened for %ss after %s failures.",
                self.pause_seconds,
                self.failure_threshold,
            )

    # =====================================
    # Auto Login Fallback
    # =====================================

    async def _refresh_token(self) -> None:
        try:
            await self.auth.auto_login()
            api_logger.info("Access token refreshed via auto_login.")
        except Exception as exc:
            api_logger.error("Auto login failed: %s", exc)
            raise

    # =====================================
    # Safe JSON Parsing
    # =====================================

    @staticmethod
    def _safe_parse_json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else {"raw": payload}
        except Exception:
            return {
                "s": "error",
                "message": "Non-JSON response",
                "raw": response.text[:300],
                "status": response.status_code,
            }

    # =====================================
    # Main REST Request Method
    # =====================================

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:

        if self.is_trading_paused():
            raise FyersAPIError(
                f"Circuit open for {self.trading_pause_remaining_seconds}s"
            )

        base_url = self._resolve_base_url(endpoint)
        url = f"{base_url}/{endpoint.lstrip('/')}"

        refresh_attempted = False

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    response = await client.request(
                        method=method.upper(),
                        url=url,
                        headers=self._auth_header(),
                        params=params,
                        json=data,
                    )
                except httpx.HTTPError as exc:
                    self._record_failure()
                    retry_logger.warning("Network error: %s", exc)
                else:
                    payload = self._safe_parse_json(response)

                    # Handle 401
                    if response.status_code == 401 and not refresh_attempted:
                        refresh_attempted = True
                        await self._refresh_token()
                        continue

                    # Retryable
                    if response.status_code in self.retry_statuses and attempt < self.max_retries:
                        self._record_failure()
                        delay = min(self.max_backoff, self.base_backoff * (2 ** attempt))
                        delay += random.uniform(0, 0.2 * delay)
                        retry_logger.warning("Retrying in %.2fs", delay)
                        await asyncio.sleep(delay)
                        continue

                    # Error
                    if response.status_code >= 400 or payload.get("s") == "error":
                        self._record_failure()
                        raise FyersAPIError(
                            payload.get("message", "FYERS error"),
                            status_code=response.status_code,
                        )

                    # Success
                    self._record_success()
                    return payload

                # Fallback backoff
                if attempt < self.max_retries:
                    backoff = min(self.max_backoff, self.base_backoff * (2 ** attempt))
                    await asyncio.sleep(backoff)

        raise FyersAPIError("FYERS request failed after retries")

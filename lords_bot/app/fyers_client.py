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


class FyersAPIError(RuntimeError):
    """Raised when FYERS API cannot serve a valid payload."""
    def __init__(self, message: str, *, status_code: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(slots=True)
class CircuitState:
    """Holds circuit breaker state."""
    opened_until: float = 0.0
    failures: int = 0


class FyersClient:
    """Centralized FYERS client with retries, circuit breaker, and auth fallback."""

    def __init__(self, auth_service: AuthService) -> None:
        self.settings = get_settings()
        self.auth = auth_service

        # Retry parameters
        self.retry_statuses = {429, 500, 502, 503, 504}
        self.max_retries = self.settings.fyers_max_retries or 3
        self.base_backoff = self.settings.fyers_retry_backoff_seconds or 0.5
        self.max_backoff = self.settings.fyers_max_backoff_seconds or 8.0

        # Circuit breaker
        self.failure_threshold = self.settings.api_failure_threshold or 5
        self.failure_window = self.settings.api_failure_window_seconds or 60
        self.pause_seconds = self.settings.api_pause_seconds or 120

        self._state = CircuitState()
        self._failure_timestamps: deque[float] = deque()

    #
    # WebSocket endpoint properties
    #

    @property
    def data_ws_url(self) -> str:
        """WebSocket endpoint for market data (official FYERS v3, always production endpoint)."""
        return "wss://api.fyers.in/socket/v2/data/"

    @property
    def order_ws_url(self) -> str:
        """WebSocket endpoint for order events."""
        return str(self.settings.fyers_order_ws_url)

    @property
    def position_ws_url(self) -> str:
        """WebSocket endpoint for position updates."""
        return str(self.settings.fyers_position_ws_url)

    @property
    def trade_ws_url(self) -> str:
        """WebSocket endpoint for trade updates."""
        return str(self.settings.fyers_trade_ws_url)

    #
    # Internal client helpers
    #

    def _resolve_base_url(self, endpoint: str) -> str:
        """
        Decide whether this is a data or trade endpoint.
        Data endpoints (history, quotes) go to data URL.
        Others go to trading URL.
        """
        ep = endpoint.lstrip("/")
        data_prefixes = ("history", "quotes", "optionchain", "symbol_master", "market_depth")
        base = self.settings.fyers_data_url if ep.startswith(data_prefixes) else self.settings.fyers_base_url
        return str(base).rstrip("/")

    def _auth_header(self) -> dict[str, str]:
        if not self.auth.access_token:
            raise FyersAPIError("Access token missing. Complete login first.")
        return {
            "Authorization": f"{self.settings.fyers_app_id}:{self.auth.access_token}",
            "Content-Type": "application/json",
        }

    #
    # Circuit Breaker
    #

    def is_trading_paused(self) -> bool:
        return time.time() < self._state.opened_until

    @property
    def trading_pause_remaining_seconds(self) -> int:
        return max(0, int(self._state.opened_until - time.time()))

    def reset_circuit_breaker(self) -> None:
        self._state = CircuitState()
        self._failure_timestamps.clear()
        circuit_logger.info("Circuit breaker manually reset.")

    def _record_success(self) -> None:
        self._state.failures = 0
        now = time.time()
        while self._failure_timestamps and now - self._failure_timestamps[0] > self.failure_window:
            self._failure_timestamps.popleft()

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
                "Circuit opened for %ss after %s failures in %ss.",
                self.pause_seconds,
                self.failure_threshold,
                self.failure_window,
            )

    #
    # Auth refresh fallback
    #

    async def _refresh_token(self) -> None:
        """
        Refresh token fallback after a 401.
        This will attempt auto_login on AuthService.
        """
        try:
            await self.auth.auto_login()
            api_logger.info("Access token restored via auto_login.")
        except Exception as exc:
            api_logger.warning("Auth auto_login failed: %s", exc)
            # If auto_login fails, let request fail later

    #
    # JSON parsing helper
    #

    @staticmethod
    def _safe_parse_json(response: httpx.Response) -> dict[str, Any]:
        """
        Do not trust response.json() — if HTML or malformatted, return safe dict.
        """
        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else {"raw": payload}
        except Exception:
            return {
                "s": "error",
                "message": "Non-JSON response from FYERS",
                "raw": response.text[:300],
                "status": response.status_code,
            }

    #
    # Perform HTTP request
    #

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """
        Perform request with:
        - retries for transient failures
        - auth fallback on 401
        - circuit breaker protection
        """
        if self.is_trading_paused():
            raise FyersAPIError(
                f"Circuit open; skip request for {self.trading_pause_remaining_seconds}s"
            )

        # Build URL for REST call
        url = f"{self._resolve_base_url(endpoint)}/{endpoint.lstrip('/')}"
        refresh_attempted = False
        last_error: FyersAPIError | None = None

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
                    last_error = FyersAPIError(f"Network transport error: {exc}")
                    retry_logger.warning("%s %s transport error: %s", method, endpoint, exc)
                else:
                    payload = self._safe_parse_json(response)

                    # On 401 unauthorized → auth fallback once
                    if response.status_code == 401 and not refresh_attempted:
                        refresh_attempted = True
                        await self._refresh_token()
                        continue

                    # Retryable HTTP statuses
                    if response.status_code in self.retry_statuses and attempt < self.max_retries:
                        self._record_failure()
                        delay = min(self.max_backoff, self.base_backoff * (2**attempt))
                        delay += random.uniform(0, 0.2 * delay)
                        retry_logger.warning(
                            "%s %s status=%s, retry in %.2f (attempt %s/%s)",
                            method.upper(),
                            endpoint,
                            response.status_code,
                            delay,
                            attempt + 1,
                            self.max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue

                    # If error
                    if response.status_code >= 400 or payload.get("s") == "error":
                        self._record_failure()
                        message = payload.get("message", response.text[:300])
                        raise FyersAPIError(message, status_code=response.status_code)

                    # Success
                    self._record_success()
                    api_logger.debug("FYERS %s %s success", method.upper(), endpoint)
                    return payload

                # fallback delay before next retry
                if attempt < self.max_retries:
                    backoff = min(self.max_backoff, self.base_backoff * (2**attempt))
                    await asyncio.sleep(backoff)

        # If all retries exhausted
        raise last_error or FyersAPIError("FYERS request failed after retries")

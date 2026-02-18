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

api_logger = logging.getLogger("lords_bot.api")
retry_logger = logging.getLogger("lords_bot.retry")
circuit_logger = logging.getLogger("lords_bot.circuit")


class FyersAPIError(RuntimeError):
    """Raised when FYERS API cannot serve a valid success payload."""

    def __init__(self, message: str, *, status_code: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(slots=True)
class CircuitState:
    """In-memory circuit breaker state to prevent endless retries on broker outages."""

    opened_until: float = 0.0
    failures: int = 0


class FyersClient:
    """
    Unified FYERS client for trading and market-data APIs.

    Why this exists:
    - Centralize auth headers, URL routing, retry, and error normalization.
    - Protect the bot from broker instability (503s, malformed payloads, token expiry).
    - Offer explicit websocket endpoint helpers used by websocket_service.
    """

    def __init__(self, auth_service: Any) -> None:
        self.settings = get_settings()
        self.auth = auth_service

        # Retry policy protects against short-lived broker/network issues.
        self.retry_statuses = {429, 500, 502, 503, 504}
        self.max_retries = int(getattr(self.settings, "fyers_max_retries", 3) or 3)
        self.base_backoff_seconds = float(getattr(self.settings, "fyers_retry_backoff_seconds", 0.5) or 0.5)
        self.max_backoff_seconds = float(getattr(self.settings, "fyers_max_backoff_seconds", 8.0) or 8.0)

        # Circuit breaker protects the rest of the system from repeated upstream failures.
        self.failure_threshold = int(getattr(self.settings, "api_failure_threshold", 5) or 5)
        self.failure_window_seconds = int(getattr(self.settings, "api_failure_window_seconds", 60) or 60)
        self.pause_seconds = int(getattr(self.settings, "api_pause_seconds", 120) or 120)
        self._state = CircuitState()
        self._failure_timestamps: deque[float] = deque()

    @property
    def data_ws_url(self) -> str:
        return str(getattr(self.settings, "fyers_data_ws_url", "wss://api.fyers.in/socket/v2/data/"))

    @property
    def order_ws_url(self) -> str:
        return str(getattr(self.settings, "fyers_order_ws_url", "wss://api.fyers.in/socket/v2/order/"))

    @property
    def position_ws_url(self) -> str:
        return str(getattr(self.settings, "fyers_position_ws_url", "wss://api.fyers.in/socket/v2/position/"))

    @property
    def trade_ws_url(self) -> str:
        return str(getattr(self.settings, "fyers_trade_ws_url", "wss://api.fyers.in/socket/v2/trade/"))

    def _resolve_base_url(self, endpoint: str) -> str:
        """Route endpoint to FYERS trade or data host."""
        ep = endpoint.lstrip("/")
        data_prefixes = ("quotes", "history", "optionchain", "symbol_master", "market_depth")
        base = self.settings.fyers_data_url if ep.startswith(data_prefixes) else self.settings.fyers_base_url
        return str(base).rstrip("/")

    def _auth_header(self) -> dict[str, str]:
        if not self.auth.access_token:
            raise FyersAPIError("Access token missing. Complete login first.")
        return {
            "Authorization": f"{self.settings.fyers_app_id}:{self.auth.access_token}",
            "Content-Type": "application/json",
        }

    def is_trading_paused(self) -> bool:
        return time.time() < self._state.opened_until

    @property
    def trading_pause_remaining_seconds(self) -> int:
        return max(0, int(self._state.opened_until - time.time()))

    def reset_circuit_breaker(self) -> None:
        self._state = CircuitState()
        self._failure_timestamps.clear()
        circuit_logger.info("Circuit breaker reset manually.")

    def _record_success(self) -> None:
        self._state.failures = 0
        now = time.time()
        while self._failure_timestamps and now - self._failure_timestamps[0] > self.failure_window_seconds:
            self._failure_timestamps.popleft()

    def _record_failure(self) -> None:
        now = time.time()
        self._failure_timestamps.append(now)
        while self._failure_timestamps and now - self._failure_timestamps[0] > self.failure_window_seconds:
            self._failure_timestamps.popleft()

        self._state.failures = len(self._failure_timestamps)
        if self._state.failures >= self.failure_threshold:
            self._state.opened_until = now + self.pause_seconds
            self._failure_timestamps.clear()
            circuit_logger.error(
                "Circuit opened for %ss after %s failures in %ss.",
                self.pause_seconds,
                self.failure_threshold,
                self.failure_window_seconds,
            )

    async def _refresh_token(self) -> None:
        """Refresh expired token once; raises if auth service cannot refresh."""
        if not hasattr(self.auth, "refresh_access_token"):
            raise FyersAPIError("Auth service does not support token refresh")
        await self.auth.refresh_access_token()
        api_logger.info("Access token refreshed after 401.")

    @staticmethod
    def _safe_parse_json(response: httpx.Response) -> dict[str, Any]:
        """Never trust upstream payload shape; HTML/plain-text should not crash the app."""
        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else {"raw": payload}
        except (ValueError, json.JSONDecodeError):
            return {
                "s": "error",
                "message": "Non-JSON response from FYERS",
                "raw": response.text[:500],
                "status": response.status_code,
            }

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | list[dict[str, Any]] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """
        Perform one protected request with finite retries and circuit breaker checks.

        Recovery behaviors:
        - 401 triggers one token refresh then request replay.
        - retryable statuses/network errors get exponential backoff with jitter.
        - bad JSON returns normalized error payload and raises FyersAPIError.
        """
        if self.is_trading_paused():
            raise FyersAPIError(f"Circuit open; retry in {self.trading_pause_remaining_seconds}s")

        url = f"{self._resolve_base_url(endpoint)}/{endpoint.lstrip('/')}"
        last_error: FyersAPIError | None = None
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
                    last_error = FyersAPIError(f"Network error: {exc}")
                    retry_logger.warning("HTTP transport error on %s %s: %s", method.upper(), endpoint, exc)
                else:
                    payload = self._safe_parse_json(response)

                    if response.status_code == 401 and not refresh_attempted:
                        refresh_attempted = True
                        try:
                            await self._refresh_token()
                        except Exception as exc:  # noqa: BLE001 - normalize to API error.
                            self._record_failure()
                            raise FyersAPIError(f"Token refresh failed: {exc}", status_code=401) from exc
                        continue

                    if response.status_code in self.retry_statuses and attempt < self.max_retries:
                        self._record_failure()
                        delay = min(self.max_backoff_seconds, self.base_backoff_seconds * (2**attempt))
                        delay += random.uniform(0, 0.2 * delay)
                        retry_logger.warning(
                            "Retrying %s %s for status=%s in %.2fs (%s/%s)",
                            method.upper(),
                            endpoint,
                            response.status_code,
                            delay,
                            attempt + 1,
                            self.max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue

                    if response.status_code >= 400 or payload.get("s") == "error":
                        self._record_failure()
                        message = payload.get("message", f"FYERS error status={response.status_code}")
                        raise FyersAPIError(message, status_code=response.status_code, code=payload.get("code"))

                    self._record_success()
                    api_logger.debug("FYERS %s %s success", method.upper(), endpoint)
                    return payload

                if attempt < self.max_retries:
                    delay = min(self.max_backoff_seconds, self.base_backoff_seconds * (2**attempt))
                    await asyncio.sleep(delay)

        raise last_error or FyersAPIError("FYERS request failed after retries")

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

import httpx

from lords_bot.app.config import get_settings

api_logger = logging.getLogger("lords_bot.api")


class FyersAPIError(RuntimeError):
    """Raised when FYERS API returns an error payload or non-2xx status."""

    def __init__(self, message: str, *, status_code: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class FyersClient:
    """FYERS client with endpoint routing, retries, and circuit breaker safety."""

    def __init__(self, auth_service: Any) -> None:
        self.settings = get_settings()
        self.auth = auth_service
        self.retry_statuses = {502, 503, 504}
        self.max_retries = int(getattr(self.settings, "fyers_max_retries", 3) or 3)
        self.base_backoff_seconds = float(getattr(self.settings, "fyers_retry_backoff_seconds", 0.5) or 0.5)

        self._failure_window_seconds = int(getattr(self.settings, "api_failure_window_seconds", 60) or 60)
        self._failure_threshold = int(getattr(self.settings, "api_failure_threshold", 5) or 5)
        self._pause_seconds = int(getattr(self.settings, "api_pause_seconds", 120) or 120)
        self._failures: deque[float] = deque()
        self._trading_paused_until: float = 0.0

    def _resolve_base_url(self, endpoint: str) -> str:
        endpoint = endpoint.lstrip("/")

        if endpoint.startswith(("history", "options", "option-chain")):
            return str(self.settings.fyers_data_url).rstrip("/")

        if endpoint.startswith(("quotes", "orders", "positions", "funds", "profile", "tradebook")):
            return str(self.settings.fyers_trading_url).rstrip("/")

        return str(self.settings.fyers_trading_url).rstrip("/")

    def is_trading_paused(self) -> bool:
        return time.time() < self._trading_paused_until

    @property
    def trading_pause_remaining_seconds(self) -> int:
        return max(0, int(self._trading_paused_until - time.time()))

    def _record_api_failure(self) -> None:
        now = time.time()
        self._failures.append(now)
        while self._failures and (now - self._failures[0]) > self._failure_window_seconds:
            self._failures.popleft()

        if len(self._failures) >= self._failure_threshold:
            self._trading_paused_until = now + self._pause_seconds
            self._failures.clear()
            api_logger.error(
                "Circuit breaker activated for %s seconds after repeated API failures.",
                self._pause_seconds,
            )

    def _record_api_success(self) -> None:
        now = time.time()
        while self._failures and (now - self._failures[0]) > self._failure_window_seconds:
            self._failures.popleft()

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.auth.access_token:
            raise FyersAPIError("Access token missing. Complete login first.")

        if self.is_trading_paused():
            raise FyersAPIError(
                f"Trading paused by circuit breaker. Retry in {self.trading_pause_remaining_seconds}s"
            )

        base_url = self._resolve_base_url(endpoint)
        url = f"{base_url}/{endpoint.lstrip('/')}"

        headers = {
            "Authorization": f"{self.settings.fyers_app_id}:{self.auth.access_token}",
            "Content-Type": "application/json",
        }

        last_error: FyersAPIError | None = None

        async with httpx.AsyncClient(timeout=30) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    response = await client.request(
                        method=method.upper(),
                        url=url,
                        headers=headers,
                        params=params,
                        json=data,
                    )
                except httpx.HTTPError as exc:
                    self._record_api_failure()
                    last_error = FyersAPIError(f"Network error while calling FYERS: {exc}")
                    api_logger.warning("FYERS network failure (%s %s): %s", method.upper(), endpoint, exc)
                else:
                    if response.status_code in self.retry_statuses and attempt < self.max_retries:
                        self._record_api_failure()
                        delay = self.base_backoff_seconds * (2**attempt)
                        api_logger.warning(
                            "FYERS %s %s returned %s; retrying in %.2fs (attempt %s/%s)",
                            method.upper(),
                            endpoint,
                            response.status_code,
                            delay,
                            attempt + 1,
                            self.max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue

                    try:
                        payload = response.json()
                    except ValueError:
                        self._record_api_failure()
                        api_logger.error(
                            "Non-JSON FYERS response (%s %s, status %s): %s",
                            method.upper(),
                            endpoint,
                            response.status_code,
                            response.text[:300],
                        )
                        raise FyersAPIError(
                            f"Non-JSON FYERS response (status={response.status_code})",
                            status_code=response.status_code,
                        )

                    if response.status_code >= 400:
                        self._record_api_failure()
                        raise FyersAPIError(
                            payload.get("message", "FYERS request failed"),
                            status_code=response.status_code,
                            code=payload.get("code"),
                        )

                    if payload.get("s") == "error":
                        self._record_api_failure()
                        raise FyersAPIError(
                            payload.get("message", "FYERS returned error"),
                            status_code=response.status_code,
                            code=payload.get("code"),
                        )

                    self._record_api_success()
                    api_logger.debug("FYERS %s %s OK", method.upper(), endpoint)
                    return payload

                if attempt < self.max_retries:
                    delay = self.base_backoff_seconds * (2**attempt)
                    await asyncio.sleep(delay)

        raise last_error or FyersAPIError("FYERS request failed after retries")

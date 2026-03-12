from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from lords_bot.app.fyers_client import FyersAPIError

if TYPE_CHECKING:
    from lords_bot.app.fyers_client import FyersClient

logger = logging.getLogger("lords_bot.risk")


class RiskEngine:
    """Risk policy engine with compatibility helpers for legacy callers/tests."""

    def __init__(
        self,
        client: "FyersClient",
        order_service: object | None = None,
        trading_mode: str = "PAPER",
        daily_loss_limit_pct: float = 0.03,
    ) -> None:
        self.client = client
        self.order_service = order_service
        self.trading_mode = trading_mode

        self.initial_capital: float = 100000.0
        self.max_daily_loss: float = self.initial_capital * daily_loss_limit_pct

        self.daily_loss = 0.0
        self.daily_loss_limit = daily_loss_limit_pct

        self.last_trade_date: dt.date | None = None
        self.daily_realized_pnl: float = 0.0

    async def reconcile_positions_on_startup(self) -> None:
        try:
            response = await self.client.request("GET", "/positions")
            logger.info("Reconciled positions at startup: %s", response)
        except FyersAPIError as exc:
            logger.warning("Startup position reconciliation skipped: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error during position reconciliation: %s", exc)

    def record_loss(self, loss_amount: float) -> None:
        if loss_amount < 0:
            self.daily_loss += abs(loss_amount)
            self.daily_realized_pnl += loss_amount
        logger.debug("Recording loss=%s daily_loss=%s", loss_amount, self.daily_loss)

    def check_loss_limit(self) -> bool:
        return self.daily_loss >= self.daily_loss_limit

    def can_trade_now(self) -> tuple[bool, str]:
        if self.client.is_trading_paused():
            return False, "circuit_open"

        if self.last_trade_date == dt.date.today() and self.daily_realized_pnl <= -self.max_daily_loss:
            return False, "max_daily_loss_hit"

        return True, "ok"

    async def square_off_and_shutdown(self) -> None:
        """Best-effort shutdown hook used by lords_bot.main."""
        try:
            await self.client.request("POST", "/exit_positions", data={"exit_all": 1})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Square-off skipped/failed: %s", exc)

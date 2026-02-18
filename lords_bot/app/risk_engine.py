from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from lords_bot.app.fyers_client import FyersAPIError

if TYPE_CHECKING:
    # Only import type for type checkers (no runtime import cycle)
    from lords_bot.app.fyers_client import FyersClient

logger = logging.getLogger("lords_bot.risk")


class RiskEngine:
    """
    Responsible for enforcing risk policies such as:
    - Position reconciliation at startup
    - Daily loss limits
    - Circuit breaker integration
    """

    def __init__(self, client: "FyersClient", daily_loss_limit_pct: float = 0.03) -> None:
        self.client = client

        # tracking daily P&L drawdown
        self.daily_loss = 0.0
        self.daily_loss_limit = daily_loss_limit_pct

    async def reconcile_positions_on_startup(self) -> None:
        """
        Try loading open positions when the bot starts.
        If unauthorized or other errors occur, handle gracefully.
        """
        try:
            response = await self.client.request("GET", "/positions")
            logger.info("Reconciled positions at startup: %s", response)
        except FyersAPIError as e:
            logger.warning("Startup position reconciliation skipped: %s", e)
        except Exception as e:
            logger.error("Unexpected error during position reconciliation: %s", e)

    def record_loss(self, loss_amount: float) -> None:
        """
        Track losses and trigger risk actions if daily loss limit is hit.

        Args:
            loss_amount: Negative P&L amount (loss) to record.
        """
        # Only count positive loss values
        if loss_amount < 0:
            self.daily_loss += abs(loss_amount)
        else:
            return

        pct = self.daily_loss
        limit = self.daily_loss_limit

        logger.debug("Recording loss: %.4f (daily total: %.4f)", loss_amount, self.daily_loss)

        if pct >= limit:
            logger.error("Daily loss threshold reached: %.4f >= %.4f", pct, limit)
            self._trigger_daily_loss_block()

    def _trigger_daily_loss_block(self) -> None:
        """
        Risk action on daily loss limit hit.
        This can be expanded such as:
        - disable trading temporarily
        - notify operator
        - persist risk state
        """
        # Pause trading by engaging circuit breaker
        try:
            self.client.reset_circuit_breaker()
            logger.error("Trading paused due to daily loss limit.")
        except Exception as exc:
            logger.error("Failed to trigger circuit breaker on loss limit: %s", exc)

    def check_loss_limit(self) -> bool:
        """
        Returns True if daily loss limit has been hit.

        Use this in position entry logic to block new orders.
        """
        return self.daily_loss >= self.daily_loss_limit

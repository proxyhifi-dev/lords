from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any

from backend.app.core.config_loader import get_settings
from backend.app.utils.logger import get_logger

logger = get_logger("execution_manager")
settings = get_settings()


class OrderState(str, Enum):
    NEW = "NEW"
    PLACING = "PLACING"
    PLACED = "PLACED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    FAILED = "FAILED"
    RETRY = "RETRY"
    ABORTED = "ABORTED"
    ORDER_UNCERTAIN = "ORDER_UNCERTAIN"


class FillState(str, Enum):
    FILLED = "FILLED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ExecutionResult:
    order_id: str | None
    filled_qty: int
    avg_price: float | None
    state: OrderState
    is_uncertain: bool = False


class ExecutionManager:
    def __init__(
        self,
        broker,
        state_manager,
        event_bus,
        reconciliation=None,
        max_retries: int = 3,
        base_backoff: float = 1.0,
    ):
        self.broker = broker
        self.state_manager = state_manager
        self.event_bus = event_bus
        self.reconciliation = reconciliation
        self.max_retries = max_retries
        self.base_backoff = base_backoff

    def _is_paper_mode(self) -> bool:
        return str(getattr(settings, "mode", "paper")).strip().lower() == "paper"

    def _paper_mode_use_broker(self) -> bool:
        return bool(getattr(settings, "paper_mode_use_broker", False))

    async def _simulate_paper_order(
        self,
        symbol: str,
        side: str,
        qty: int,
    ) -> ExecutionResult:
        avg_price = None

        if self.broker and self._paper_mode_use_broker():
            try:
                quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
                if hasattr(self.broker, "parse_ltp"):
                    avg_price = self.broker.parse_ltp(quote)
            except Exception as exc:
                logger.warning(
                    "Paper mode quote fetch failed for simulated order symbol=%s side=%s err=%s",
                    symbol, side, exc,
                )

        if avg_price is None:
            avg_price = 0.0

        paper_order_id = f"PAPER-{side}-{symbol}-{qty}"
        logger.info(
            "PAPER MODE: simulated order symbol=%s side=%s qty=%d avg_price=%.2f",
            symbol, side, qty, avg_price,
        )
        return ExecutionResult(
            order_id=paper_order_id,
            filled_qty=qty,
            avg_price=avg_price,
            state=OrderState.FILLED,
            is_uncertain=False,
        )

    async def execute_order(self, request: dict[str, Any]) -> ExecutionResult:
        signal = str(request.get("signal") or "")
        symbol = str(request.get("symbol") or "")
        ts = str(request.get("timestamp") or "")
        qty = int(request.get("quantity") or 0)
        side = str(request.get("side") or "").upper()

        key = self._idempotency_key(signal, symbol, ts)
        if await self.state_manager.has_idempotency_key(key):
            logger.warning("Idempotent skip key=%s symbol=%s side=%s", key, symbol, side)
            return ExecutionResult(
                order_id=None,
                filled_qty=0,
                avg_price=None,
                state=OrderState.ABORTED,
            )

        await self.state_manager.add_idempotency_key(key)

        if qty <= 0 or not symbol or side not in {"BUY", "SELL"}:
            logger.error(
                "Invalid order request symbol=%s side=%s qty=%s",
                symbol, side, qty,
            )
            return ExecutionResult(
                order_id=None,
                filled_qty=0,
                avg_price=None,
                state=OrderState.ABORTED,
            )

        if self._is_paper_mode():
            return await self._simulate_paper_order(symbol=symbol, side=side, qty=qty)

        exec_ctx = {
            "state": OrderState.NEW,
            "order_id": None,
            "filled_qty": 0,
            "avg_price": None,
        }

        for attempt in range(1, self.max_retries + 1):
            self._transition(exec_ctx, OrderState.PLACING)
            try:
                resp = await self.broker.place_order(
                    symbol=symbol,
                    side=side,
                    quantity=qty,
                )
                oid = resp.get("orderNumber") or resp.get("orderId") or resp.get("order_id")
                if not oid:
                    self._transition(exec_ctx, OrderState.FAILED)
                else:
                    exec_ctx["order_id"] = oid
                    self._transition(exec_ctx, OrderState.PLACED)
                    fill_state, fqty, avg = await self.broker.confirm_fill(oid)
                    exec_ctx["filled_qty"] = fqty
                    exec_ctx["avg_price"] = avg
                    if fill_state == "FILLED":
                        self._transition(exec_ctx, OrderState.FILLED)
                        return ExecutionResult(oid, fqty, avg, OrderState.FILLED, False)
                    if fill_state == "PARTIAL_FILL":
                        self._transition(exec_ctx, OrderState.PARTIAL_FILL)
                        await self._mark_uncertain(exec_ctx, reason="partial_fill_not_resolved")
                        return ExecutionResult(
                            oid,
                            fqty,
                            avg,
                            OrderState.ORDER_UNCERTAIN,
                            True,
                        )
                    if fill_state == "FAILED":
                        self._transition(exec_ctx, OrderState.FAILED)
                    else:
                        await self._mark_uncertain(exec_ctx, reason="fill_unknown")
                        return ExecutionResult(
                            oid,
                            fqty,
                            avg,
                            OrderState.ORDER_UNCERTAIN,
                            True,
                        )
            except Exception as exc:
                logger.error(
                    "execute_order broker exception attempt=%d/%d symbol=%s side=%s err=%s",
                    attempt, self.max_retries, symbol, side, exc,
                )
                await self._mark_uncertain(exec_ctx, reason="broker_api_error")
                return ExecutionResult(
                    exec_ctx.get("order_id"),
                    exec_ctx["filled_qty"],
                    exec_ctx["avg_price"],
                    OrderState.ORDER_UNCERTAIN,
                    True,
                )

            if exec_ctx["state"] == OrderState.FAILED and attempt < self.max_retries:
                self._transition(exec_ctx, OrderState.RETRY)
                await asyncio.sleep(self.base_backoff * (2 ** (attempt - 1)))
            else:
                break

        self._transition(exec_ctx, OrderState.ABORTED)
        return ExecutionResult(
            exec_ctx.get("order_id"),
            exec_ctx["filled_qty"],
            exec_ctx["avg_price"],
            OrderState.ABORTED,
            False,
        )

    async def _mark_uncertain(self, exec_ctx: dict, reason: str) -> None:
        exec_ctx["state"] = OrderState.ORDER_UNCERTAIN
        await self.state_manager.record_uncertain_order(
            {
                "order_id": exec_ctx.get("order_id"),
                "state": OrderState.ORDER_UNCERTAIN.value,
                "filled_qty": exec_ctx.get("filled_qty", 0),
                "avg_price": exec_ctx.get("avg_price"),
                "reason": reason,
            }
        )
        await self.state_manager.update(
            trading_enabled=False,
            last_order_failed=True,
            last_risk_breach="execution_uncertain",
        )
        await self.event_bus.publish(
            "EXECUTION_UNCERTAIN",
            {"order_id": exec_ctx.get("order_id"), "reason": reason},
        )
        if self.reconciliation:
            await self.reconciliation.run_once()

    def _transition(self, exec_ctx: dict, new_state: OrderState) -> None:
        old = exec_ctx.get("state")
        exec_ctx["state"] = new_state
        logger.info(
            "Order state transition %s -> %s order=%s",
            old,
            new_state,
            exec_ctx.get("order_id"),
        )

    @staticmethod
    def _idempotency_key(signal: str, symbol: str, timestamp: str) -> str:
        raw = f"{signal}|{symbol}|{timestamp}".encode()
        return hashlib.sha256(raw).hexdigest()
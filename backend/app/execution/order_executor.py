# backend/app/execution/order_executor.py

from __future__ import annotations
import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from dataclasses import dataclass

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.app.utils.logger import get_logger

settings = get_settings()
logger = get_logger("order_executor")

@dataclass
class OrderState:
    """Order execution state tracking."""
    order_id: str
    symbol: str
    side: str
    quantity: int
    status: str  # PENDING, PARTIAL, FILLED, FAILED, CANCELLED
    filled_quantity: int = 0
    average_price: float = 0.0
    created_at: datetime = None
    updated_at: datetime = None
    retry_count: int = 0
    idempotency_key: str = ""
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = datetime.now()
        if not self.idempotency_key:
            self.idempotency_key = str(uuid.uuid4())

class OrderExecutor:
    """
    Production-grade order execution with retry logic, idempotency,
    and comprehensive state tracking.
    """
    
    def __init__(self, event_bus: EventBus, state_manager: StateManager, broker: SamcoClient):
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.broker = broker
        self._active_orders: Dict[str, OrderState] = {}
        self._idempotency_cache: Dict[str, str] = {}  # key -> order_id
        self._circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=120)
        
    async def run(self) -> None:
        """Main order execution loop."""
        await asyncio.gather(
            self._listen_for_orders(),
            self._monitor_active_orders(),
            self._cleanup_expired_orders()
        )
    
    async def _listen_for_orders(self) -> None:
        """Listen for RISK_APPROVED events and execute orders."""
        queue = self.event_bus.subscribe("RISK_APPROVED")
        async for event in self.event_bus.iter_events(queue):
            await self._execute_order(event.payload)
    
    async def _execute_order(self, payload: Dict[str, Any]) -> None:
        """Execute order with full error handling and retry logic."""
        trace_id = payload.get("trace_id", str(uuid.uuid4()))
        
        try:
            # Generate idempotency key
            idempotency_key = self._generate_idempotency_key(payload)
            
            # Check for duplicate orders
            if idempotency_key in self._idempotency_cache:
                existing_order_id = self._idempotency_cache[idempotency_key]
                logger.warning("Duplicate order detected", extra={
                    "idempotency_key": idempotency_key,
                    "existing_order_id": existing_order_id,
                    "trace_id": trace_id
                })
                return
            
            # Create order state
            order_state = OrderState(
                order_id="",  # Will be set after placement
                symbol=payload["symbol"],
                side=payload["side"],
                quantity=payload["quantity"],
                status="PENDING",
                idempotency_key=idempotency_key
            )
            
            self._active_orders[order_state.idempotency_key] = order_state
            
            # Execute with retry
            await self._execute_with_retry(order_state, payload, trace_id)
            
        except Exception as exc:
            logger.error(f"Order execution failed: {exc}", extra={
                "payload": payload,
                "trace_id": trace_id
            }, exc_info=True)
            
            await self.event_bus.publish("ORDER_FAILED", {
                "payload": payload,
                "error": str(exc),
                "trace_id": trace_id
            })
    
    async def _execute_with_retry(self, order_state: OrderState, payload: Dict[str, Any], trace_id: str) -> None:
        """Execute order with exponential backoff retry."""
        max_retries = 3
        base_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                async with asyncio.timeout(10.0):  # 10 second timeout
                    order_result = await self._circuit_breaker.call(
                        self.broker.place_order,
                        symbol=order_state.symbol,
                        side=order_state.side,
                        quantity=order_state.quantity
                    )
                
                # Success - update state
                order_state.order_id = order_result.get("orderNumber") or order_result.get("orderId")
                order_state.status = "PENDING"
                order_state.updated_at = datetime.now()
                order_state.retry_count = attempt
                
                self._idempotency_cache[order_state.idempotency_key] = order_state.order_id
                self._active_orders[order_state.idempotency_key] = order_state
                
                logger.info("Order placed successfully", extra={
                    "order_id": order_state.order_id,
                    "symbol": order_state.symbol,
                    "quantity": order_state.quantity,
                    "attempt": attempt + 1,
                    "trace_id": trace_id
                })
                
                await self.event_bus.publish("ORDER_PLACED", {
                    "order_id": order_state.order_id,
                    "payload": payload,
                    "trace_id": trace_id
                })
                
                return
                
            except asyncio.TimeoutError:
                logger.warning(f"Order timeout on attempt {attempt + 1}", extra={
                    "symbol": order_state.symbol,
                    "attempt": attempt + 1,
                    "trace_id": trace_id
                })
                
            except Exception as exc:
                logger.error(f"Order placement failed on attempt {attempt + 1}: {exc}", extra={
                    "symbol": order_state.symbol,
                    "attempt": attempt + 1,
                    "trace_id": trace_id
                })
            
            # Exponential backoff
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                await asyncio.sleep(delay)
        
        # All retries failed
        order_state.status = "FAILED"
        order_state.updated_at = datetime.now()
        
        logger.error("Order execution failed after all retries", extra={
            "symbol": order_state.symbol,
            "quantity": order_state.quantity,
            "max_retries": max_retries,
            "trace_id": trace_id
        })
        
        await self.event_bus.publish("ORDER_FAILED_FINAL", {
            "order_state": order_state.__dict__,
            "payload": payload,
            "trace_id": trace_id
        })
    
    async def _monitor_active_orders(self) -> None:
        """Monitor active orders for fills and updates."""
        while True:
            try:
                await asyncio.sleep(2)  # Check every 2 seconds
                
                if not self._active_orders:
                    continue
                
                # Get order updates from broker
                async with asyncio.timeout(5.0):
                    orders = await self.broker.get_orders()
                
                # Update local state
                for order in orders:
                    order_id = order.get("orderNumber") or order.get("orderId")
                    if not order_id:
                        continue
                    
                    # Find matching local order
                    local_order = None
                    for key, state in self._active_orders.items():
                        if state.order_id == order_id:
                            local_order = state
                            break
                    
                    if not local_order:
                        continue
                    
                    # Update status
                    broker_status = order.get("orderStatus", "").upper()
                    filled_qty = order.get("filledShares") or order.get("tradedQty") or 0
                    avg_price = order.get("averagePrice") or 0
                    
                    # Map broker status to local status
                    if broker_status in ["COMPLETE", "FILLED", "TRADED"]:
                        local_order.status = "FILLED"
                    elif broker_status in ["PARTIAL"]:
                        local_order.status = "PARTIAL"
                    elif broker_status in ["CANCELLED", "REJECTED"]:
                        local_order.status = "FAILED"
                    
                    local_order.filled_quantity = filled_qty
                    local_order.average_price = avg_price
                    local_order.updated_at = datetime.now()
                    
                    # Publish fill events
                    if local_order.status in ["PARTIAL", "FILLED"]:
                        await self.event_bus.publish("ORDER_FILL_UPDATE", {
                            "order_id": order_id,
                            "status": local_order.status,
                            "filled_quantity": filled_qty,
                            "average_price": avg_price,
                            "trace_id": local_order.idempotency_key
                        })
                    
                    # Clean up completed orders
                    if local_order.status in ["FILLED", "FAILED"]:
                        del self._active_orders[local_order.idempotency_key]
                        
            except Exception as exc:
                logger.error(f"Order monitoring failed: {exc}", exc_info=True)
                await asyncio.sleep(5)  # Back off on errors
    
    async def _cleanup_expired_orders(self) -> None:
        """Clean up orders that have been pending too long."""
        while True:
            await asyncio.sleep(300)  # Check every 5 minutes
            
            now = datetime.now()
            expired_keys = []
            
            for key, order in self._active_orders.items():
                if (now - order.created_at).total_seconds() > 1800:  # 30 minutes
                    expired_keys.append(key)
                    logger.warning("Order expired", extra={
                        "order_id": order.order_id,
                        "symbol": order.symbol,
                        "created_at": order.created_at.isoformat()
                    })
            
            for key in expired_keys:
                del self._active_orders[key]
    
    def _generate_idempotency_key(self, payload: Dict[str, Any]) -> str:
        """Generate idempotency key for order deduplication."""
        key_parts = [
            payload.get("symbol", ""),
            payload.get("side", ""),
            str(payload.get("quantity", 0)),
            payload.get("trace_id", "")
        ]
        return "|".join(key_parts)
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        try:
            async with asyncio.timeout(5.0):
                result = await self.broker.cancel_order(order_id)
            
            logger.info("Order cancelled", extra={"order_id": order_id})
            
            # Update local state
            for key, order in self._active_orders.items():
                if order.order_id == order_id:
                    order.status = "CANCELLED"
                    order.updated_at = datetime.now()
                    break
            
            return True
            
        except Exception as exc:
            logger.error(f"Order cancellation failed: {exc}", extra={"order_id": order_id})
            return False
    
    def get_active_orders(self) -> Dict[str, OrderState]:
        """Get all active orders."""
        return self._active_orders.copy()


class CircuitBreaker:
    """Circuit breaker for broker calls."""
    
    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 120):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"
    
    async def call(self, func, *args, **kwargs):
        if self.state == "OPEN":
            if datetime.now().timestamp() - (self.last_failure_time or 0) > self.recovery_timeout:
                self.state = "HALF_OPEN"
            else:
                raise CircuitBreakerOpen("Circuit breaker is OPEN")
        
        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure()
            raise exc
    
    def _on_success(self):
        self.failure_count = 0
        self.state = "CLOSED"
    
    def _on_failure(self):
        self.failure_count += 1
        self.last_failure_time = datetime.now().timestamp()
        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"


class CircuitBreakerOpen(Exception):
    pass

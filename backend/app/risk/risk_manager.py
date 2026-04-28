# backend/app/risk/risk_manager.py

from __future__ import annotations
from datetime import datetime, time
from typing import Dict, Any
import asyncio

from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.app.broker.samco_client import SamcoClient
from backend.app.utils.logger import get_logger

settings = get_settings()
logger = get_logger("risk_manager")

class RiskManager:
    """
    Production-grade risk management with broker reconciliation,
    portfolio-level controls, and capital protection.
    """
    
    def __init__(self, event_bus: EventBus, state_manager: StateManager, broker: SamcoClient):
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.broker = broker
        self._circuit_breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
        
    async def run(self) -> None:
        """Main risk evaluation loop with broker reconciliation."""
        queue = self.event_bus.subscribe("SIGNAL")
        async for event in self.event_bus.iter_events(queue):
            await self._evaluate_with_reconciliation(event)
    
    async def _evaluate_with_reconciliation(self, event) -> None:
        """Evaluate signal with full broker reconciliation."""
        
        async def block(reason: str, details: Dict[str, Any] = None):
            logger.warning(f"RISK_BLOCKED: {reason}", extra=details or {})
            await self.state_manager.update(signal=None, signal_meta=None)
            await self.event_bus.publish("RISK_BLOCKED", {
                "reason": reason, 
                "details": details,
                "timestamp": datetime.now().isoformat()
            })
        
        try:
            # Step 1: Broker reconciliation check
            if not await self._reconcile_broker_state():
                await block("broker_reconciliation_failed")
                return
            
            # Step 2: Comprehensive risk evaluation
            state = await self.state_manager.snapshot()
            payload = event.payload or {}

            # Core guards
            if not await self._check_core_guards(state, payload):
                return

            # Portfolio-level risk
            if not await self._check_portfolio_risk(state, payload):
                return

            # Trade-specific risk
            if not await self._check_trade_risk(state, payload):
                return

            # Market conditions
            if not await self._check_market_conditions(payload):
                return

            # Liquidity validation
            if not await self._check_liquidity(payload):
                return

            equity = self._equity(state)
            # ✅ APPROVED - Log with full context
            logger.info("RISK_APPROVED", extra={
                "signal": payload.get("signal"),
                "qty": payload.get("qty", settings.order_qty),
                "equity": equity,
                "exposure_pct": (payload.get("notional", 0) / equity) * 100 if equity else 0.0,
                "trace_id": payload.get("trace_id")
            })

            await self.state_manager.update(signal=None, signal_meta=None)
            await self.event_bus.publish("RISK_APPROVED", payload)
                
        except Exception as exc:
            logger.error(f"Risk evaluation failed: {exc}", exc_info=True)
            await block("risk_evaluation_error", {"error": str(exc)})
    
    async def _reconcile_broker_state(self) -> bool:
        """Reconcile local state with broker before any decisions."""
        try:
            async with asyncio.timeout(5.0):
                positions = await self.broker.get_positions()
                orders = await self.broker.get_orders()
                
            # Check for unexpected positions
            nifty_positions = [p for p in positions if "NIFTY" in str(p.get("tradingSymbol", ""))]
            if nifty_positions:
                logger.warning("Unexpected broker positions found", extra={
                    "positions": nifty_positions
                })
                return False
            
            # Check for pending orders
            pending_orders = [o for o in orders if o.get("orderStatus") in ["PENDING", "OPEN"]]
            if pending_orders:
                logger.warning("Pending orders found", extra={
                    "orders": pending_orders
                })
                return False
                
            return True
            
        except asyncio.TimeoutError:
            logger.error("Broker reconciliation timeout")
            return False
        except Exception as exc:
            logger.error(f"Broker reconciliation failed: {exc}")
            return False
    
    async def _check_core_guards(self, state, payload) -> bool:
        """Core safety guards."""
        if state.active_trade:
            await self._block("active_trade_open")
            return False
        
        if state.trade_count >= settings.max_trades:
            await self._block(f"max_trades_exceeded_{settings.max_trades}")
            return False
        
        if state.daily_pnl <= -abs(settings.max_daily_loss):
            await self.state_manager.update(trading_enabled=False)
            await self._block(f"max_daily_loss_{settings.max_daily_loss}")
            return False
        
        if not state.trading_enabled:
            await self._block("trading_disabled")
            return False
        
        return True
    
    async def _check_portfolio_risk(self, state, payload) -> bool:
        """Portfolio-level risk controls."""
        equity = self._equity(state)
        # Equity check with buffer
        equity_buffer = 0.05  # 5% buffer
        if equity < settings.capital * (1 - equity_buffer):
            await self.state_manager.update(trading_enabled=False)
            await self._block("insufficient_equity")
            return False
        
        # Max drawdown protection
        max_drawdown_pct = getattr(settings, "max_drawdown_pct", 0.10)
        if state.peak_equity > 0:
            drawdown = (state.peak_equity - equity) / state.peak_equity
            if drawdown > max_drawdown_pct:
                await self.state_manager.update(trading_enabled=False)
                await self._block(f"max_drawdown_exceeded_{drawdown:.1%}")
                return False
        
        # Portfolio exposure limits
        max_exposure_pct = getattr(settings, "max_portfolio_exposure_pct", 0.50)
        current_exposure = sum(state.positions.values()) if hasattr(state, 'positions') else 0
        trade_exposure = payload.get("notional", 0)
        
        if equity <= 0:
            await self._block("equity_not_positive")
            return False
        if (current_exposure + trade_exposure) / equity > max_exposure_pct:
            await self._block("portfolio_exposure_exceeded")
            return False
        
        return True
    
    async def _check_trade_risk(self, state, payload) -> bool:
        """Trade-specific risk controls."""
        equity = self._equity(state)
        qty = payload.get("qty", settings.order_qty)
        max_qty = getattr(settings, "max_qty", settings.order_qty * 5)
        
        if qty > max_qty:
            await self._block(f"position_size_exceeded_{qty}_{max_qty}")
            return False
        
        # Per-trade risk limit
        max_trade_risk_pct = getattr(settings, "max_trade_risk_pct", 0.02)  # 2%
        trade_risk = payload.get("notional", 0) * payload.get("risk_pct", 0.10)  # Assume 10% risk
        
        if equity <= 0:
            await self._block("equity_not_positive")
            return False
        if trade_risk / equity > max_trade_risk_pct:
            await self._block("trade_risk_exceeded")
            return False
        
        return True
    
    async def _check_market_conditions(self, payload) -> bool:
        """Market condition validation."""
        now = datetime.now().time()
        h, m = map(int, str(settings.no_entry_after).split(":"))
        no_entry_after = time(h, m)
        if now > no_entry_after:
            await self._block(f"late_entry_{settings.no_entry_after}")
            return False
        
        if payload.get("price", 0) <= 0:
            await self._block("invalid_price")
            return False
        
        # Volatility filter
        min_atr = getattr(settings, "min_atr", 0)
        if min_atr > 0 and payload.get("atr", 0) < min_atr:
            await self._block("insufficient_volatility")
            return False
        
        return True
    
    async def _check_liquidity(self, payload) -> bool:
        """Liquidity and slippage validation."""
        volume = payload.get("volume", 0)
        min_volume = getattr(settings, "min_option_volume", 1000)
        
        if volume < min_volume:
            await self._block(f"insufficient_liquidity_{volume}")
            return False
        
        # Spread check
        spread_pct = payload.get("spread_pct", 0)
        max_spread_pct = getattr(settings, "max_spread_pct", 0.05)  # 5%
        
        if spread_pct > max_spread_pct:
            await self._block(f"excessive_spread_{spread_pct:.1%}")
            return False
        
        return True
    
    async def _block(self, reason: str, details: Dict = None):
        """Helper for blocking with logging."""
        logger.warning(f"RISK_BLOCKED: {reason}", extra=details or {})
        await self.state_manager.update(signal=None, signal_meta=None)
        await self.event_bus.publish("RISK_BLOCKED", {
            "reason": reason,
            "details": details,
            "timestamp": datetime.now().isoformat()
        })

    @staticmethod
    def _equity(state) -> float:
        return settings.capital + float(getattr(state, "realized_pnl", 0.0)) + float(getattr(state, "unrealized_pnl", 0.0))


class CircuitBreaker:
    """Circuit breaker for external service calls."""
    
    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
    
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

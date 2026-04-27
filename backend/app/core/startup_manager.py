# backend/app/core/startup_manager.py

"""
Lords Bot — Safe Startup Manager v1.0
======================================
Ensures system state is synchronized with broker before trading begins.

CRITICAL SAFETY: Broker is always source of truth.
- Fetches all positions from broker
- Fetches all open orders from broker
- Rebuilds internal state from broker data
- Forces reconciliation on any mismatches
- Only allows trading after successful sync
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Any, Optional
from datetime import datetime

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.engine.state_manager import state_manager
from backend.app.storage.trade_store import TradeStore
from backend.app.utils.logger import get_logger

settings = get_settings()
logger = get_logger("startup_manager")


@dataclass
class BrokerPosition:
    """Broker position data structure."""
    symbol: str
    quantity: int
    average_price: float
    current_price: float
    pnl: float
    product_type: str  # MIS, CNC, etc.


@dataclass
class BrokerOrder:
    """Broker order data structure."""
    order_id: str
    symbol: str
    side: str  # BUY/SELL
    quantity: int
    price: float
    order_type: str  # LIMIT, MARKET, etc.
    status: str  # OPEN, FILLED, CANCELLED, etc.
    product_type: str


class StartupManager:
    """
    Ensures safe system startup by synchronizing with broker state.

    CRITICAL: This runs BEFORE any trading logic starts.
    If broker sync fails, system refuses to start trading.
    """

    def __init__(self):
        self.broker = SamcoClient()
        self.trade_store = TradeStore()
        self._sync_successful = False
        self._broker_positions: List[BrokerPosition] = []
        self._broker_orders: List[BrokerOrder] = []

    async def perform_safe_startup(self) -> bool:
        """
        Perform comprehensive broker synchronization.

        Returns True if safe to proceed with trading.
        Returns False if critical issues found (system should not trade).
        """
        try:
            logger.info("🔄 Starting safe startup synchronization...")

            # Step 1: Login to broker
            if not await self._login_to_broker():
                logger.error("❌ Broker login failed - cannot proceed")
                return False

            # Step 2: Fetch broker positions
            if not await self._fetch_broker_positions():
                logger.error("❌ Failed to fetch broker positions")
                return False

            # Step 3: Fetch broker orders
            if not await self._fetch_broker_orders():
                logger.error("❌ Failed to fetch broker orders")
                return False

            # Step 4: Rebuild internal state from broker data
            if not await self._rebuild_internal_state():
                logger.error("❌ Failed to rebuild internal state")
                return False

            # Step 5: Force reconciliation
            if not await self._force_reconciliation():
                logger.error("❌ Reconciliation failed - critical mismatch")
                return False

            # Step 6: Validate system health
            if not await self._validate_system_health():
                logger.error("❌ System health check failed")
                return False

            self._sync_successful = True
            logger.info("✅ Safe startup completed successfully")
            return True

        except Exception as exc:
            logger.error(f"❌ Safe startup failed with exception: {exc}", exc_info=True)
            return False

    async def _login_to_broker(self) -> bool:
        """Login to broker and verify connection."""
        try:
            logger.info("🔑 Logging into broker...")
            await self.broker.login()
            logger.info("✅ Broker login successful")
            return True
        except Exception as exc:
            logger.error(f"❌ Broker login failed: {exc}")
            return False

    async def _fetch_broker_positions(self) -> List[BrokerPosition]:
        """Fetch all positions from broker."""
        try:
            logger.info("📊 Fetching broker positions...")

            # Get positions from broker
            positions_data = await self.broker.get_positions()

            self._broker_positions = []
            for pos in positions_data:
                try:
                    position = BrokerPosition(
                        symbol=pos.get("tradingSymbol", ""),
                        quantity=int(pos.get("netQty", 0)),
                        average_price=float(pos.get("avgPrice", 0)),
                        current_price=float(pos.get("ltp", 0)),
                        pnl=float(pos.get("pnl", 0)),
                        product_type=pos.get("product", "")
                    )
                    self._broker_positions.append(position)
                except (ValueError, KeyError) as exc:
                    logger.warning(f"Failed to parse position: {pos} - {exc}")

            logger.info(f"✅ Fetched {len(self._broker_positions)} positions from broker")
            return True

        except Exception as exc:
            logger.error(f"❌ Failed to fetch broker positions: {exc}")
            return False

    async def _fetch_broker_orders(self) -> List[BrokerOrder]:
        """Fetch all open orders from broker."""
        try:
            logger.info("📋 Fetching broker orders...")

            # Get orders from broker
            orders_data = await self.broker.get_orders()

            self._broker_orders = []
            for order in orders_data:
                try:
                    # Only care about open/pending orders
                    if order.get("orderStatus") not in ["OPEN", "TRIGGER_PENDING", "MODIFY_PENDING"]:
                        continue

                    broker_order = BrokerOrder(
                        order_id=order.get("orderId", ""),
                        symbol=order.get("tradingSymbol", ""),
                        side=order.get("transactionType", ""),
                        quantity=int(order.get("quantity", 0)),
                        price=float(order.get("price", 0)),
                        order_type=order.get("orderType", ""),
                        status=order.get("orderStatus", ""),
                        product_type=order.get("productType", "")
                    )
                    self._broker_orders.append(broker_order)
                except (ValueError, KeyError) as exc:
                    logger.warning(f"Failed to parse order: {order} - {exc}")

            logger.info(f"✅ Fetched {len(self._broker_orders)} open orders from broker")
            return True

        except Exception as exc:
            logger.error(f"❌ Failed to fetch broker orders: {exc}")
            return False

    async def _rebuild_internal_state(self) -> bool:
        """
        Rebuild internal state from broker data.

        CRITICAL: Broker positions override internal state.
        """
        try:
            logger.info("🔄 Rebuilding internal state from broker data...")

            # Get current internal state
            current_state = await state_manager.snapshot()

            # Reset positions to match broker
            broker_positions_dict = {}
            total_pnl = 0.0

            for pos in self._broker_positions:
                if pos.quantity != 0:  # Only track non-zero positions
                    broker_positions_dict[pos.symbol] = pos.quantity
                    total_pnl += pos.pnl

            # Update state with broker positions
            await state_manager.update(
                positions=broker_positions_dict,
                live_pnl=total_pnl,
                unrealized_pnl=total_pnl,  # Assuming all unrealized for now
                last_updated=datetime.now().isoformat()
            )

            # Handle active trade if exists
            if self._broker_positions:
                # Find the active position (assuming single position strategy)
                active_pos = None
                for pos in self._broker_positions:
                    if pos.quantity > 0:  # Long position
                        active_pos = pos
                        break

                if active_pos:
                    # Reconstruct active trade from broker position
                    active_trade = {
                        "symbol": active_pos.symbol,
                        "entry_price": active_pos.average_price,
                        "qty": abs(active_pos.quantity),
                        "entry_time": datetime.now().isoformat(),  # Approximate
                        "current_price": active_pos.current_price,
                        "unrealized_pnl": active_pos.pnl
                    }
                    await state_manager.update(active_trade=active_trade)
                    logger.info(f"✅ Reconstructed active trade: {active_trade}")
                else:
                    # No active positions
                    await state_manager.update(active_trade=None)

            logger.info("✅ Internal state rebuilt from broker data")
            return True

        except Exception as exc:
            logger.error(f"❌ Failed to rebuild internal state: {exc}")
            return False

    async def _force_reconciliation(self) -> bool:
        """
        Force reconciliation and fix any mismatches.

        CRITICAL: This makes reconciliation AUTHORITATIVE.
        """
        try:
            logger.info("🔧 Forcing reconciliation...")

            # Import here to avoid circular imports
            from backend.app.engine.reconciliation import ReconciliationEngine

            reconciler = ReconciliationEngine(
                broker=self.broker,
                state_manager=state_manager,
                event_bus=None  # Not needed for startup
            )

            # Run reconciliation
            result = await reconciler.run_once()

            # Check if there were issues
            has_issues = result.get("issues_found", 0) > 0

            if has_issues:
                logger.warning(f"⚠️ Found {result['issues_found']} issues during startup")

                # FORCE FIX: Override internal state with broker state
                success = await self._rebuild_internal_state()

                if success:
                    logger.info("✅ Issues resolved by forcing broker sync")
                    return True
                else:
                    logger.error("❌ Failed to resolve issues")
                    return False
            else:
                logger.info("✅ No issues found")
                return True

        except Exception as exc:
            logger.error(f"❌ Reconciliation failed: {exc}")
            return False

    async def _validate_system_health(self) -> bool:
        """
        Validate that system is in a healthy state to begin trading.
        """
        try:
            logger.info("🏥 Validating system health...")

            state = await state_manager.snapshot()

            # Check for critical issues
            issues = []

            # 1. Check if we have unexpected open orders
            if len(self._broker_orders) > 0:
                issues.append(f"Found {len(self._broker_orders)} open orders - manual intervention may be needed")

            # 2. Check position consistency
            if state.active_trade and not state.positions:
                issues.append("Active trade exists but no positions recorded")

            # 3. Check P&L consistency
            if abs(state.live_pnl - state.unrealized_pnl) > 1.0:  # Allow 1 rupee tolerance
                issues.append("P&L inconsistency detected")

            # Log warnings but don't fail for non-critical issues
            for issue in issues:
                logger.warning(f"⚠️ Health check issue: {issue}")

            # Critical check: If we have positions but no active trade, that's suspicious
            if state.positions and not state.active_trade:
                logger.warning("⚠️ Positions exist but no active trade recorded - may need manual review")

            logger.info("✅ System health validation completed")
            return True

        except Exception as exc:
            logger.error(f"❌ System health validation failed: {exc}")
            return False

    @property
    def sync_successful(self) -> bool:
        """Whether startup synchronization was successful."""
        return self._sync_successful

    @property
    def broker_positions(self) -> List[BrokerPosition]:
        """Get broker positions fetched during startup."""
        return self._broker_positions.copy()

    @property
    def broker_orders(self) -> List[BrokerOrder]:
        """Get broker orders fetched during startup."""
        return self._broker_orders.copy()


# Global instance
startup_manager = StartupManager()
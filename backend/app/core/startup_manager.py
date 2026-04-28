"""
Lords Bot — Startup Manager (FIXED WITH STRATEGY INTEGRATION)
==============================================================

✅ CRITICAL FIX v2.0:
   1. Initialize OrbStrategyFinalProduction
   2. Pass strategy to TradingEngine
   3. Ensure strategy.set_already_traded_today() can be called

This file handles safe startup synchronization and component initialization.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.app.engine.trading_engine import TradingEngine
from backend.app.storage.trade_store import TradeStore
from backend.app.strategy.orb_strategy import OrbStrategyFinalProduction  # ✅ FIXED: Use FinalProduction
from backend.app.utils.logger import get_logger

settings = get_settings()
logger = get_logger("startup_manager")


class StartupManager:
    """
    Manages safe startup synchronization and component initialization.
    
    ✅ Ensures proper initialization order:
       1. EventBus
       2. StateManager
       3. Broker (login)
       4. Strategy (OrbStrategyFinalProduction)
       5. TradingEngine (with strategy reference)
       6. TradeStore
    """

    def __init__(self):
        self.sync_successful = False
        self.broker_positions = []
        self.broker_orders = []
        
        # Components
        self.event_bus = None
        self.state_manager = None
        self.broker = None
        self.strategy = None        # ✅ NEW: Store strategy reference
        self.trading_engine = None
        self.trade_store = None

    async def perform_safe_startup(self) -> bool:
        """
        Perform safe startup synchronization with broker.
        Returns True if successful, False otherwise.
        """
        try:
            logger.info("🔄 Starting safe startup synchronization...")

            # ─────────────────────────────────────────────────────────────
            # STEP 1: Initialize core components
            # ─────────────────────────────────────────────────────────────
            logger.info("📦 Initializing core components...")
            self.event_bus = EventBus()
            self.state_manager = StateManager(self.event_bus)
            self.trade_store = TradeStore()
            logger.info("✅ Core components initialized")

            # ─────────────────────────────────────────────────────────────
            # STEP 2: Initialize broker and login
            # ─────────────────────────────────────────────────────────────
            logger.info("🔐 Initializing broker...")
            self.broker = SamcoClient(
                user_id=settings.user_id,
                password=settings.password,
                yob=settings.yob,
                event_bus=self.event_bus
            )

            logger.info("🔑 Logging in to broker...")
            await self.broker.login()
            logger.info("✅ Broker login successful")

            # ─────────────────────────────────────────────────────────────
            # STEP 3: Fetch broker positions and orders (safety check)
            # ─────────────────────────────────────────────────────────────
            logger.info("📊 Fetching broker positions and open orders...")
            try:
                self.broker_positions = await self.broker.get_positions()
                self.broker_orders = await self.broker.get_open_orders()
                
                logger.info(
                    "📊 Broker sync: positions=%d orders=%d",
                    len(self.broker_positions),
                    len(self.broker_orders)
                )

                if self.broker_positions:
                    logger.warning(
                        "⚠️  Found %d open positions at startup — "
                        "ensure they are intentional or closed",
                        len(self.broker_positions)
                    )

                if self.broker_orders:
                    logger.warning(
                        "⚠️  Found %d open orders at startup — "
                        "ensure they are intentional or cancelled",
                        len(self.broker_orders)
                    )
            except Exception as exc:
                logger.warning("⚠️  Could not sync positions/orders: %s (non-critical)", exc)

            # ─────────────────────────────────────────────────────────────
            # STEP 4: Initialize Strategy (OrbStrategyFinalProduction)
            # ─────────────────────────────────────────────────────────────
            logger.info("📊 Initializing strategy...")
            self.strategy = OrbStrategyFinalProduction(self.event_bus, self.state_manager)
            logger.info("✅ Strategy initialized: OrbStrategyFinalProduction")

            # ─────────────────────────────────────────────────────────────
            # STEP 5: Initialize TradingEngine with strategy reference
            # ─────────────────────────────────────────────────────────────
            logger.info("🎯 Initializing trading engine...")
            self.trading_engine = TradingEngine(
                event_bus=self.event_bus,
                state_manager=self.state_manager,
                trade_store=self.trade_store,
                broker=self.broker,
                strategy=self.strategy  # ✅ CRITICAL: Pass strategy here
            )
            logger.info("✅ TradingEngine initialized with strategy reference")

            # ─────────────────────────────────────────────────────────────
            # STEP 6: Update state manager with spot price
            # ─────────────────────────────────────────────────────────────
            logger.info("💰 Fetching initial spot price...")
            spot = await self.broker.get_spot_price()
            if spot:
                await self.state_manager.update(spot_price=spot)
                logger.info("💰 Initial spot price: ₹%.2f", spot)

            # ─────────────────────────────────────────────────────────────
            # SUCCESS
            # ─────────────────────────────────────────────────────────────
            self.sync_successful = True
            logger.info("✅ Safe startup synchronization COMPLETE")
            logger.info("🚀 Bot is ready to trade")

            return True

        except Exception as exc:
            logger.error("❌ Safe startup failed: %s", exc, exc_info=True)
            self.sync_successful = False
            return False

    async def cleanup(self) -> None:
        """Clean shutdown of all components."""
        try:
            logger.info("🛑 Cleaning up components...")
            if self.broker:
                await self.broker.logout()
                logger.info("✅ Broker logged out")
        except Exception as exc:
            logger.error("Error during cleanup: %s", exc)


# Global instance
startup_manager = StartupManager()
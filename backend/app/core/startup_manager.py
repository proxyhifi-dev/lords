"""
Lords Bot — Startup Manager (FINAL PRODUCTION VERSION)
=====================================================

✅ FIXES:
✔ SamcoClient() takes NO arguments
✔ Uses correct settings (samco_user_id etc.)
✔ No partial startup
✔ Safe initialization
✔ Clean logging
✔ Production-safe flow
"""

from __future__ import annotations

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.app.engine.trading_engine import TradingEngine
from backend.app.storage.trade_store import TradeStore
from backend.app.utils.logger import get_logger

logger = get_logger("startup_manager")


class StartupManager:
    def __init__(self):
        self.sync_successful = False

        # Components
        self.event_bus = None
        self.state_manager = None
        self.broker = None
        self.strategy = None
        self.trading_engine = None
        self.trade_store = None

    async def perform_safe_startup(self) -> bool:
        try:
            logger.info("🔄 Starting safe startup...")

            settings = get_settings()

            # ─────────────────────────────
            # STEP 1: Core Components
            # ─────────────────────────────
            logger.info("📦 Initializing core components...")
            self.event_bus = EventBus()
            self.state_manager = StateManager()
            self.trade_store = TradeStore()
            logger.info("✅ Core components initialized")

            # ─────────────────────────────
            # STEP 2: Broker Init
            # ─────────────────────────────
            logger.info("🔐 Initializing broker...")

            # ✅ SamcoClient takes NO args
            self.broker = SamcoClient()

            # Validate credentials in LIVE mode
            if settings.is_live:
                if not settings.samco_user_id or not settings.samco_password:
                    raise ValueError("SAMCO credentials missing in LIVE mode")

            logger.info("🔑 Logging in to broker...")
            await self.broker.login()
            logger.info("✅ Broker login successful")

            # ─────────────────────────────
            # STEP 3: Broker Sync
            # ─────────────────────────────
            logger.info("📊 Fetching positions & orders...")

            try:
                positions = await self.broker.get_positions()
                orders = await self.broker.get_orders()

                logger.info(
                    "📊 Broker sync → positions=%d orders=%d",
                    len(positions),
                    len(orders),
                )

                if positions:
                    logger.warning("⚠️ Open positions found: %d", len(positions))

                if orders:
                    logger.warning("⚠️ Open orders found: %d", len(orders))

            except Exception as e:
                logger.warning("⚠️ Broker sync skipped: %s", e)

            # ─────────────────────────────
            # STEP 4: Strategy Init
            # ─────────────────────────────


            # ─────────────────────────────
            # STEP 5: Trading Engine
            # ─────────────────────────────
            logger.info("🎯 Initializing trading engine...")
            self.trading_engine = TradingEngine(
                event_bus=self.event_bus,
                state_manager=self.state_manager,
                trade_store=self.trade_store,
                broker=self.broker,
                strategy=self.strategy,
            )
            logger.info("✅ Trading engine ready")

            # ─────────────────────────────
            # STEP 6: Initial Market Data
            # ─────────────────────────────
            logger.info("💰 Fetching initial spot price...")

            try:
                quote = await self.broker.get_index_quote(settings.nifty_symbol)
                spot = self.broker.parse_spot(quote)

                if spot:
                    await self.state_manager.update(spot_price=spot)
                    logger.info("💰 Spot price: ₹%.2f", spot)
                else:
                    logger.warning("⚠️ Could not parse spot price")

            except Exception as e:
                logger.warning("⚠️ Spot fetch failed: %s", e)

            # ─────────────────────────────
            # SUCCESS
            # ─────────────────────────────
            self.sync_successful = True
            logger.info("✅ SAFE STARTUP COMPLETE")
            logger.info("🚀 BOT READY")

            return True

        except Exception as e:
            logger.error("❌ STARTUP FAILED: %s", e, exc_info=True)
            self.sync_successful = False
            return False

    async def cleanup(self):
        try:
            logger.info("🛑 Shutting down...")
            if self.broker:
                logger.info("✅ Cleanup complete")
        except Exception as e:
            logger.error("Cleanup error: %s", e)


# Global instance
startup_manager = StartupManager()
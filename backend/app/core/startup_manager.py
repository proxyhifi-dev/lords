"""
Lords Bot — Startup Manager
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
        self.event_bus = None
        self.state_manager = None
        self.broker = None
        self.strategy = None
        self.trading_engine = None
        self.trade_store = None
        self.broker_positions: list[dict] = []
        self.broker_orders: list[dict] = []

    async def perform_safe_startup(self) -> bool:
        try:
            logger.info("Starting safe startup...")

            settings = get_settings()
            mode = str(getattr(settings, "mode", "paper")).strip().lower()
            is_live = bool(getattr(settings, "is_live", mode == "live"))
            paper_mode_use_broker = bool(getattr(settings, "paper_mode_use_broker", True))

            logger.info(
                "Startup mode resolved | mode=%s is_live=%s paper_mode_use_broker=%s",
                mode,
                is_live,
                paper_mode_use_broker,
            )

            logger.info("Initializing core components...")
            self.event_bus = EventBus()
            self.state_manager = StateManager()
            self.trade_store = TradeStore()
            logger.info("Core components initialized")

            use_broker = is_live or paper_mode_use_broker
            if use_broker:
                logger.info("Initializing broker...")
                self.broker = SamcoClient()

                if is_live and (not settings.samco_user_id or not settings.samco_password):
                    raise ValueError("SAMCO credentials missing in LIVE mode")

                logger.info("Logging in to broker...")
                await self.broker.login()
                logger.info("Broker login successful")
            else:
                self.broker = None
                logger.info("Broker disabled for this mode; skipping broker login")

            logger.info("Initializing trading engine...")
            self.trading_engine = TradingEngine(
                event_bus=self.event_bus,
                state_manager=self.state_manager,
                trade_store=self.trade_store,
                broker=self.broker,
                strategy=self.strategy,
            )
            logger.info("Trading engine ready")

            if self.broker is not None:
                logger.info("Fetching positions & orders...")
                try:
                    positions = await self.broker.get_positions()
                    orders = await self.broker.get_orders()
                    state = await self.state_manager.snapshot()

                    self.broker_positions = positions or []
                    self.broker_orders = orders or []

                    logger.info(
                        "Broker sync | positions=%d orders=%d",
                        len(self.broker_positions),
                        len(self.broker_orders),
                    )

                    if positions:
                        logger.warning("Open positions found: %d", len(positions))
                    if orders:
                        logger.warning("Open orders found: %d", len(orders))

                    has_broker_position = any(
                        self._extract_net_qty(position) != 0
                        for position in self.broker_positions
                    )
                    has_local_position = bool(getattr(state, "active_trade", None))

                    if is_live and has_broker_position != has_local_position:
                        logger.critical(
                            "STARTUP_RECONCILIATION_MISMATCH broker_open=%s local_open=%s",
                            has_broker_position,
                            has_local_position,
                        )
                        await self.state_manager.update(
                            trading_enabled=False,
                            last_risk_breach="startup_reconciliation_mismatch",
                        )
                        return False

                    if not is_live and has_broker_position != has_local_position:
                        logger.warning(
                            "Paper mode startup mismatch broker_open=%s local_open=%s",
                            has_broker_position,
                            has_local_position,
                        )

                except Exception as exc:
                    if is_live:
                        logger.error("Broker sync failed in live mode: %s", exc, exc_info=True)
                        return False
                    logger.warning("Broker sync skipped in paper mode: %s", exc)
            else:
                logger.info("Skipping broker sync because broker is disabled")

            if self.broker is not None:
                logger.info("Fetching initial spot price...")
                try:
                    quote = await self.broker.get_index_quote(settings.nifty_symbol)
                    spot = self.broker.parse_spot(quote)
                    if spot:
                        await self.state_manager.update(spot_price=spot)
                        logger.info("Spot price: %.2f", spot)
                    else:
                        logger.warning("Could not parse spot price")
                except Exception as exc:
                    if is_live:
                        logger.error("Initial spot fetch failed in live mode: %s", exc, exc_info=True)
                        return False
                    logger.warning("Spot fetch failed in paper mode: %s", exc)
            else:
                logger.info("Skipping initial spot fetch because broker is disabled")

            await self._apply_startup_trading_mode(is_live=is_live)

            self.sync_successful = True
            logger.info("SAFE STARTUP COMPLETE")
            logger.info("BOT READY")
            return True

        except Exception as exc:
            logger.error("STARTUP FAILED: %s", exc, exc_info=True)
            self.sync_successful = False
            return False

    async def _apply_startup_trading_mode(self, is_live: bool) -> None:
        state = await self.state_manager.snapshot()

        if is_live:
            logger.info(
                "Startup trading state preserved for LIVE mode | trading_enabled=%s",
                state.trading_enabled,
            )
            return

        if not state.trading_enabled:
            await self.state_manager.update(trading_enabled=True)
            logger.info("Paper mode startup: trading auto-resumed")
        else:
            logger.info("Paper mode startup: trading already enabled")

    async def cleanup(self):
        try:
            logger.info("Shutting down...")
            logger.info("Cleanup complete")
        except Exception as exc:
            logger.error("Cleanup error: %s", exc)

    @staticmethod
    def _extract_net_qty(position: dict) -> int:
        for key in ("netQty", "netQuantity", "net_qty", "quantity"):
            try:
                return int(float(str(position.get(key, 0)).replace(",", "").strip()))
            except (TypeError, ValueError):
                continue
        return 0


startup_manager = StartupManager()
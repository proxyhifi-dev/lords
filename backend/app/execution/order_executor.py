from __future__ import annotations

from datetime import UTC, datetime

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.strategy.option_selector import OptionSelector
from backend.app.utils.logger import get_logger

settings = get_settings()


class OrderExecutor:
    """
    Optional order-executor that listens to RISK_APPROVED events.
    Wire into scheduler if you want the RiskManager → OrderExecutor
    pipeline instead of TradingEngine's built-in execution.
    """

    def __init__(
        self,
        event_bus: EventBus,
        broker: SamcoClient,
        option_selector: OptionSelector,
    ) -> None:
        self.event_bus = event_bus
        self.broker = broker
        self.option_selector = option_selector
        self.logger = get_logger("order_executor")

    async def run(self) -> None:
        queue = self.event_bus.subscribe("RISK_APPROVED")

        async for event in self.event_bus.iter_events(queue):

            signal = event.payload["signal"]
            spot = float(event.payload["spot_price"])

            expiry = OptionSelector.get_expiry()
            strike = OptionSelector.get_otm_strike(spot, signal)
            option_type = OptionSelector.get_option_type(signal)

            self.logger.info(
                "OrderExecutor: signal=%s spot=%.2f strike=%s expiry=%s type=%s",
                signal, spot, strike, expiry, option_type,
            )

            # Build symbol from chain
            chain = await self.broker.get_option_chain(
                search_symbol_name="NIFTY",
                exchange="NFO",
                expiry_date=expiry,
                strike_price=str(strike),
                option_type=option_type,
            )
            rows = chain.get("optionChainDetails") or chain.get("data") or []
            symbol = rows[0].get("tradingSymbol") if rows else None

            if not symbol:
                self.logger.error("OrderExecutor: symbol not found")
                continue

            order = await self.broker.place_order(
                symbol=symbol,
                side="BUY",
                quantity=settings.order_qty,
            )

            trade = {
                "symbol": symbol,
                "signal": signal,
                "entry_price": spot,
                "entry_time": datetime.now(UTC).isoformat(),
                "order": order,
                "status": "ENTERED",
            }

            self.logger.info("Order placed symbol=%s response=%s", symbol, order)
            await self.event_bus.publish("ORDER_PLACED", {"trade": trade})

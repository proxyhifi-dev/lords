from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.app.storage.trade_store import TradeStore
from backend.app.utils.logger import get_logger
from backend.app.strategy.option_selector import OptionSelector


class TradingEngine:

    def __init__(
        self,
        event_bus: EventBus,
        state_manager: StateManager,
        trade_store: TradeStore,
        broker: SamcoClient,
    ) -> None:

        self.event_bus = event_bus
        self.state_manager = state_manager
        self.trade_store = trade_store
        self.broker = broker

        self.logger = get_logger("trading_engine")

    # ------------------------------------------------
    # MAIN ENGINE
    # ------------------------------------------------

    async def run(self) -> None:

        await asyncio.gather(

            self._on_tick(),

            self._on_orb_updated(),

            self._on_signal(),

            self._on_order_placed(),

            self._on_square_off(),

            self._reconcile_loop(),

            self._health_monitor_loop(),
        )

    # ------------------------------------------------
    # TICK EVENT
    # ------------------------------------------------

    async def _on_tick(self) -> None:

        queue = self.event_bus.subscribe("TICK")

        async for event in self.event_bus.iter_events(queue):

            price = float(event.payload.get("price", 0))

            await self.state_manager.update(spot_price=price)

    # ------------------------------------------------
    # ORB UPDATE
    # ------------------------------------------------

    async def _on_orb_updated(self) -> None:

        queue = self.event_bus.subscribe("ORB_UPDATED")

        async for event in self.event_bus.iter_events(queue):

            await self.state_manager.update(

                orb_high=event.payload.get("orb_high"),

                orb_low=event.payload.get("orb_low"),
            )

    # ------------------------------------------------
    # SIGNAL HANDLER
    # ------------------------------------------------

    async def _on_signal(self) -> None:

        queue = self.event_bus.subscribe("SIGNAL")

        async for event in self.event_bus.iter_events(queue):

            signal = event.payload.get("signal")

            await self.state_manager.update(signal=signal)

            state = await self.state_manager.snapshot()

            # avoid multiple trades

            if state.active_trade:
                continue

            try:

                symbol, strike = OptionSelector.select(

                    state.spot_price,

                    signal,
                )

                qty = 50

                side = "BUY"

                order = await self.broker.place_order(

                    symbol=symbol,

                    side=side,

                    quantity=qty
                )

                trade = {

                    "symbol": symbol,

                    "side": side,

                    "entry_time": datetime.now(UTC).isoformat(),

                    "entry_price": state.spot_price,

                    "order": order,

                    "status": "OPEN",
                }

                await self.event_bus.publish(

                    "ORDER_PLACED",

                    {"trade": trade},
                )

            except Exception as exc:

                self.logger.error(

                    "order_execution_failed=%s",

                    exc,
                )

    # ------------------------------------------------
    # ORDER PLACED
    # ------------------------------------------------

    async def _on_order_placed(self) -> None:

        queue = self.event_bus.subscribe("ORDER_PLACED")

        async for event in self.event_bus.iter_events(queue):

            trade = event.payload["trade"]

            state = await self.state_manager.snapshot()

            await self.state_manager.update(

                active_trade=trade,

                trade_count=state.trade_count + 1,
            )

            self.trade_store.append_trade(trade)

    # ------------------------------------------------
    # SQUARE OFF
    # ------------------------------------------------

    async def _on_square_off(self) -> None:

        queue = self.event_bus.subscribe("SCHEDULE_SQUARE_OFF")

        async for _event in self.event_bus.iter_events(queue):

            state = await self.state_manager.snapshot()

            if not state.active_trade:
                continue

            symbol = state.active_trade["symbol"]

            quantity = state.active_trade["order"].get("quantity", 0)

            exit_order = await self.broker.place_order(

                symbol=symbol,

                side="SELL",

                quantity=quantity,
            )

            pnl = self._calculate_pnl(state)

            closed_trade = {

                **state.active_trade,

                "exit_time": datetime.now(UTC).isoformat(),

                "status": "EXITED",

                "exit_order": exit_order,

                "pnl": pnl,
            }

            self.trade_store.append_trade(closed_trade)

            await self.state_manager.update(

                active_trade=None,

                daily_pnl=state.daily_pnl + pnl,
            )

    # ------------------------------------------------
    # RECONCILIATION LOOP
    # ------------------------------------------------

    async def _reconcile_loop(self) -> None:

        while True:

            await asyncio.sleep(30)

            try:

                broker_orders = await self.broker.get_orders()

                broker_positions = await self.broker.get_positions()

                await self.event_bus.publish(

                    "RECONCILIATION",

                    {

                        "orders": broker_orders,

                        "positions": broker_positions,
                    },
                )

                await self._repair_state_if_needed(

                    broker_orders,

                    broker_positions,
                )

            except Exception as exc:

                self.logger.error(

                    "reconciliation_error=%s",

                    exc,
                )

    # ------------------------------------------------
    # BROKER HEALTH
    # ------------------------------------------------

    async def _health_monitor_loop(self) -> None:

        while True:

            await asyncio.sleep(15)

            healthy = await self.broker.healthcheck()

            await self.event_bus.publish(

                "BROKER_HEALTH",

                {"healthy": healthy},
            )

    # ------------------------------------------------
    # STATE REPAIR
    # ------------------------------------------------

    async def _repair_state_if_needed(

        self,

        orders: dict[str, Any],

        positions: dict[str, Any],

    ) -> None:

        state = await self.state_manager.snapshot()

        active_trade = state.active_trade

        if not active_trade:
            return

        orders_blob = str(orders).upper()

        if "REJECT" in orders_blob:

            await self.state_manager.update(active_trade=None)

        elif "CANCEL" in orders_blob:

            await self.state_manager.update(active_trade=None)

        elif "PARTIAL" in orders_blob:

            trade = {**active_trade, "status": "PARTIAL_FILL"}

            await self.state_manager.update(active_trade=trade)

        elif "COMPLETE" in orders_blob or "FILLED" in orders_blob:

            trade = {**active_trade, "status": "FILLED"}

            await self.state_manager.update(active_trade=trade)

    # ------------------------------------------------
    # PNL CALCULATION
    # ------------------------------------------------

    @staticmethod
    def _calculate_pnl(state: Any) -> float:

        if not state.active_trade or state.spot_price is None:
            return 0.0

        entry = float(state.active_trade.get("entry_price") or 0)

        return float(state.spot_price) - entry
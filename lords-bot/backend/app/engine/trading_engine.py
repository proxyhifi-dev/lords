from __future__ import annotations

from datetime import datetime
from typing import Any

from backend.app.engine.state_manager import StateManager
from backend.app.execution.order_executor import OrderExecutor
from backend.app.market.tick_engine import TickEngine
from backend.app.options.option_selector import OptionSelector
from backend.app.risk.risk_manager import RiskManager
from backend.app.storage.trade_store import TradeStore
from backend.app.strategy.orb_strategy import OrbStrategy
from backend.app.utils.logger import get_logger


class TradingEngine:
    def __init__(
        self,
        tick_engine: TickEngine,
        strategy: OrbStrategy,
        risk_manager: RiskManager,
        option_selector: OptionSelector,
        order_executor: OrderExecutor,
        trade_store: TradeStore,
    ) -> None:
        self.tick_engine = tick_engine
        self.strategy = strategy
        self.risk_manager = risk_manager
        self.option_selector = option_selector
        self.order_executor = order_executor
        self.trade_store = trade_store
        self.logger = get_logger("trading_engine")
        self.state_manager = StateManager(active_trade=self.trade_store.last_open_trade())

    def on_quote(self, quote: dict[str, Any]) -> None:
        tick = self.tick_engine.on_quote(quote)
        if not tick:
            return

        self.state_manager.set_spot(float(tick["price"]))
        self.strategy.update_orb(tick)
        signal = self.strategy.check_breakout(tick)
        if signal:
            self.state_manager.set_signal(signal)

        state = self.state_manager.snapshot()
        if signal and state.active_trade is None and self.risk_manager.can_trade():
            self._enter_trade(signal, tick)

        state = self.state_manager.snapshot()
        if state.active_trade is not None:
            self._manage_trade(tick, state.active_trade)

    def _enter_trade(self, signal: str, tick: dict[str, Any]) -> None:
        option_symbol = self.option_selector.select_option_symbol(tick["price"], signal)
        self.order_executor.place_entry(option_symbol)
        self.risk_manager.register_entry()

        trade = {
            "symbol": option_symbol,
            "entry_price": float(tick["price"]),
            "exit_price": None,
            "pnl": 0.0,
            "timestamp": datetime.utcnow().isoformat(),
            "state": "ENTERED",
        }
        self.state_manager.set_active_trade(trade)
        self.trade_store.append_trade(trade)
        self.logger.info("Trade entered %s", trade)

    def _manage_trade(self, tick: dict[str, Any], trade: dict[str, Any]) -> None:
        entry = float(trade["entry_price"])
        now = float(tick["price"])
        pnl = now - entry
        trade["pnl"] = pnl
        self.state_manager.update_pnl(self.risk_manager.snapshot.realized_pnl + pnl)

        next_state = None
        if self.risk_manager.should_stoploss(entry, now):
            next_state = "STOPLOSS_HIT"
        elif self.risk_manager.should_target(entry, now):
            next_state = "TARGET_HIT"

        if next_state:
            self.order_executor.place_exit(trade["symbol"])
            trade["exit_price"] = now
            trade["state"] = next_state
            self.trade_store.append_trade({**trade, "state": "EXITED"})
            self.risk_manager.register_exit(pnl)
            self.state_manager.set_active_trade(None)
            self.logger.info("Trade exited state=%s trade=%s", next_state, trade)

    def square_off(self) -> None:
        state = self.state_manager.snapshot()
        if not state.active_trade:
            return

        trade = state.active_trade
        self.order_executor.place_exit(trade["symbol"])
        trade["state"] = "EXITED"
        self.trade_store.append_trade(trade)
        self.state_manager.set_active_trade(None)

    def dashboard_data(self) -> dict[str, Any]:
        state = self.state_manager.snapshot()
        return {
            "spot": state.spot,
            "orb_high": self.strategy.state.orb_high,
            "orb_low": self.strategy.state.orb_low,
            "signal": state.signal,
            "active_trade": state.active_trade,
            "pnl": state.pnl,
        }

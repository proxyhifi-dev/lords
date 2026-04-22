from __future__ import annotations

from datetime import datetime

import asyncio
import pytest

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from backend.app.broker.samco_client import SamcoClient
from backend.app.core.event_bus import EventBus
from backend.app.risk.risk_manager import RiskManager
from backend.app.scheduler.market_scheduler import MarketScheduler
from backend.app.strategy.option_selector import OptionSelector


def test_option_selector_call_put_mapping() -> None:
    assert OptionSelector.get_option_type("CALL") == "CE"
    assert OptionSelector.get_option_type("PUT") == "PE"


def test_option_selector_otm_strike() -> None:
    assert OptionSelector.get_otm_strike(24463.0, "CALL", distance=1, step=50) == 24500
    assert OptionSelector.get_otm_strike(24463.0, "PUT", distance=1, step=50) == 24400


def test_parse_spot_nested_index_details() -> None:
    quote = {"indexDetails": [{"spotPrice": "24,450.35"}]}
    assert SamcoClient.parse_spot(quote) == pytest.approx(24450.35)


def test_parse_ltp_from_quote_details() -> None:
    quote = {"quoteDetails": {"lastTradedPrice": "123.45"}}
    assert SamcoClient.parse_ltp(quote) == pytest.approx(123.45)


def test_event_bus_publish_subscribe() -> None:
    async def _run() -> None:
        bus = EventBus()
        await bus.start()
        q = bus.subscribe("PING")
        await bus.publish("PING", {"x": 1})
        ev = await q.get()
        await bus.stop()
        assert ev.type == "PING"
        assert ev.payload["x"] == 1

    asyncio.run(_run())


class _DummyState:
    def __init__(self, *, active_trade=None, trade_count=0, daily_pnl=0.0, trading_enabled=True):
        self.active_trade = active_trade
        self.trade_count = trade_count
        self.daily_pnl = daily_pnl
        self.trading_enabled = trading_enabled


class _DummyStateManager:
    def __init__(self, state: _DummyState):
        self._state = state

    async def snapshot(self):
        return self._state

    async def update(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self._state, k, v)


class _CaptureBus:
    def __init__(self):
        self.events = []

    async def publish(self, event_type, payload):
        self.events.append((event_type, payload))


def test_risk_manager_blocks_when_active_trade() -> None:
    async def _run() -> None:
        bus = _CaptureBus()
        sm = _DummyStateManager(_DummyState(active_trade={"symbol": "X"}))
        rm = RiskManager(bus, sm)

        class E:
            payload = {"signal": "CALL", "size_label": "FULL"}

        await rm._evaluate(E())
        assert bus.events[-1][0] == "RISK_BLOCKED"

    asyncio.run(_run())


def test_risk_manager_approves_happy_path() -> None:
    async def _run() -> None:
        bus = _CaptureBus()
        sm = _DummyStateManager(_DummyState())
        rm = RiskManager(bus, sm)

        class E:
            payload = {"signal": "PUT", "size_label": "FULL"}

        await rm._evaluate(E())
        assert bus.events[-1][0] == "RISK_APPROVED"

    asyncio.run(_run())


def test_scheduler_candle_completion() -> None:
    s = MarketScheduler()
    t1 = datetime(2026, 4, 22, 9, 30, 1)
    t2 = datetime(2026, 4, 22, 9, 31, 1)

    assert s._update_candle(100.0, t1) is None
    candle = s._update_candle(101.0, t2)
    assert candle is not None
    assert candle["open"] == 100.0
    assert candle["close"] == 100.0

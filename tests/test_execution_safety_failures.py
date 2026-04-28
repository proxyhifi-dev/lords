from __future__ import annotations

import asyncio

from backend.app.engine.trading_engine import TradingEngine


class DummyBus:
    async def publish(self, *_args, **_kwargs):
        return None


class DummyTradeStore:
    def append_trade(self, *_args, **_kwargs):
        return None


class DummyState:
    def __init__(self, *, active_trade=None, trading_enabled=True):
        self.active_trade = active_trade
        self.trading_enabled = trading_enabled


class DummyStateManager:
    def __init__(self, state: DummyState):
        self._state = state

    async def snapshot(self):
        return self._state

    async def update(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self._state, k, v)


class SequenceBroker:
    def __init__(self, responses=None):
        self.responses = responses or []
        self.place_calls = 0
        self.cancelled = []
        self.position_qty = 0
        self.health_ok = True

    async def place_order_and_wait_fill(self, **_kwargs):
        self.place_calls += 1
        if not self.responses:
            return None, None
        return self.responses.pop(0)

    async def get_order_status(self, _order_id):
        return {"orderStatus": "COMPLETE", "filledQty": self.position_qty}

    async def get_actual_fill_price(self, _order_id):
        return 100.0

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"status": "Success"}

    async def get_positions(self):
        return [{"tradingSymbol": "NIFTY", "netQty": self.position_qty}]

    async def healthcheck(self):
        return self.health_ok

    async def login(self):
        return {"ok": True}



def _make_engine(broker, *, active_trade=None):
    sm = DummyStateManager(DummyState(active_trade=active_trade))
    return TradingEngine(
        event_bus=DummyBus(),
        state_manager=sm,
        trade_store=DummyTradeStore(),
        broker=broker,
        strategy=None,
    )


def test_failure_sim_network_drop_during_active_trade_triggers_disable() -> None:
    async def _run() -> None:
        broker = SequenceBroker()
        broker.health_ok = False
        engine = _make_engine(broker, active_trade={"symbol": "NIFTY", "qty": 10})
        await engine._handle_fatal_exception("network_drop", RuntimeError("timeout"))
        state = await engine.state_manager.snapshot()
        assert state.trading_enabled is False

    asyncio.run(_run())


def test_failure_sim_partial_fill_is_accepted_and_cancelled() -> None:
    async def _run() -> None:
        broker = SequenceBroker(responses=[("OID-1", 101.5)])
        broker.position_qty = 3
        engine = _make_engine(broker)

        async def partial_fill(*_args, **_kwargs):
            return {"orderStatus": "PARTIAL", "filledQty": 3}, 3, 101.5

        engine._await_fill_confirmation = partial_fill
        order_id, fill, qty = await engine._buy_with_retry("NIFTY", 5)
        assert order_id == "OID-1"
        assert fill == 101.5
        assert qty == 3
        assert broker.cancelled == ["OID-1"]

    asyncio.run(_run())


def test_failure_sim_rejected_then_retry_then_fill() -> None:
    async def _run() -> None:
        broker = SequenceBroker(responses=[("OID-R", 100.0), ("OID-OK", 100.2)])
        engine = _make_engine(broker)

        calls = {"count": 0}

        async def status_seq(order_id, requested_qty, side):
            calls["count"] += 1
            if order_id == "OID-R":
                return {"orderStatus": "REJECTED"}, 0, None
            return {"orderStatus": "COMPLETE"}, requested_qty, 100.2

        engine._await_fill_confirmation = status_seq
        order_id, _fill, qty = await engine._buy_with_retry("NIFTY", 5)
        assert order_id == "OID-OK"
        assert qty == 5
        assert broker.place_calls == 2

    asyncio.run(_run())


def test_failure_sim_delayed_fill_timeout_returns_failure() -> None:
    async def _run() -> None:
        broker = SequenceBroker(responses=[("OID-1", None), ("OID-2", None), ("OID-3", None)])
        engine = _make_engine(broker)

        async def never_fill(*_args, **_kwargs):
            return {"orderStatus": "OPEN"}, 0, None

        engine._await_fill_confirmation = never_fill
        order_id, fill, qty = await engine._buy_with_retry("NIFTY", 5)
        assert order_id is None
        assert fill is None
        assert qty == 0

    asyncio.run(_run())


def test_failure_sim_exit_validation_retries_until_flat() -> None:
    async def _run() -> None:
        broker = SequenceBroker()
        broker.position_qty = 4
        engine = _make_engine(broker)
        retries = {"count": 0}

        async def fake_sell(symbol, qty, reason):
            retries["count"] += 1
            broker.position_qty = max(0, broker.position_qty - qty)
            return "SELL-1", 99.0

        engine._sell_with_retry = fake_sell
        closed = await engine._ensure_position_closed("NIFTY", "FAST_MARKET", fallback_qty=4)
        assert closed is True
        assert retries["count"] >= 1

    asyncio.run(_run())


def test_failure_sim_position_api_unavailable_fails_closed() -> None:
    async def _run() -> None:
        broker = SequenceBroker()
        engine = _make_engine(broker)

        async def unavailable(_symbol):
            return -1

        engine._get_open_position_qty = unavailable
        closed = await engine._ensure_position_closed("NIFTY", "API_DOWN", fallback_qty=2)
        assert closed is False

    asyncio.run(_run())


def test_failure_sim_fast_market_dynamic_spread_tightens() -> None:
    broker = SequenceBroker()
    engine = _make_engine(broker)
    for px in [100, 110, 90, 112, 88, 115]:
        limit = engine._compute_dynamic_spread_limit("NIFTY", px)
    assert 0.04 <= limit <= 0.12


def test_failure_sim_execution_validation_mismatch_raises() -> None:
    async def _run() -> None:
        broker = SequenceBroker()
        broker.position_qty = 1
        engine = _make_engine(broker)
        try:
            await engine._validate_post_order_position("NIFTY", expected_qty=5, context="ENTRY")
            raise AssertionError("Expected mismatch exception")
        except RuntimeError as exc:
            assert "position mismatch" in str(exc)

    asyncio.run(_run())

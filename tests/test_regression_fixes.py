from backend.app.storage.trade_store import TradeStore
from backend.app.broker.samco_client import SamcoClient
from backend.app.core.event_bus import EventBus
from backend.app.scheduler import market_scheduler as market_scheduler_module
from backend.app.scheduler.market_scheduler import MarketScheduler
from backend.app.engine import reconciliation as reconciliation_module
from backend.app.engine import trading_engine as trading_engine_module
from backend.app.engine.reconciliation import ReconciliationEngine
from backend.app.engine.trading_engine import TradingEngine
from backend.app.engine.order_execution import OrderExecutionSequence
from backend.app.strategy.iron_condor_strategy import IronCondorStrategy

import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest


def test_trade_store_accepts_float_charges(tmp_path):
    f = tmp_path / 'trades.csv'
    store = TradeStore(str(f))
    store.append_trade({"symbol": "NIFTY", "pnl": 10.0, "charges": 12.5}, daily_pnl=10.0)
    rows = store.get_all_trades()
    assert rows[-1]["total_charges"] in ("12.5", "12.50")


def test_samco_healthcheck_bool_on_exception(monkeypatch):
    c = SamcoClient()
    async def bad(_):
        raise RuntimeError('x')
    monkeypatch.setattr(c, 'get_index_quote', bad)
    out = asyncio.run(c.healthcheck())
    assert out is False


def test_samco_detects_auth_error_shapes():
    c = SamcoClient()
    assert c._looks_like_auth_error({"status": "SessionExpired"})
    assert c._looks_like_auth_error({"statusMessage": "Invalid or mismatched access token"})
    assert c._looks_like_auth_error(RuntimeError("Unauthorized request"))
    assert c._looks_like_auth_error("token expired")
    assert not c._looks_like_auth_error({"status": "Success"})


def test_samco_call_sdk_relogs_on_auth_response(monkeypatch):
    c = SamcoClient()
    c._session_live = True

    calls = {"count": 0}
    logins = {"count": 0}

    def fake_fn():
        calls["count"] += 1
        if calls["count"] == 1:
            return {"status": "Failed", "statusMessage": "Invalid or mismatched access token"}
        return {"status": "Success", "quoteDetails": {"lastTradedPrice": "100.00"}}

    async def fake_login():
        logins["count"] += 1
        c._session_live = True
        return {"status": "Success", "sessionToken": "X"}

    monkeypatch.setattr(c, "login", fake_login)

    out = asyncio.run(c._call_sdk(fake_fn, "get_quote"))
    assert out["status"] == "Success"
    assert calls["count"] == 2
    assert logins["count"] == 1


def test_short_put_uses_short_put_premium():
    class B: pass
    class S: order_qty=1; ic_margin_required=1
    logs=[]
    class L:
        def info(self,*a,**k): logs.append(str(a[0]) if a else '')
        def error(self,*a,**k): pass
    seq = OrderExecutionSequence(B(), S(), L())

    calls=[]
    async def fake(**kwargs):
        calls.append(kwargs)
        return {'success': True, 'order_id': '1', 'filled_price': kwargs.get('price') or 0}
    seq._place_order_with_retry = fake

    asyncio.run(seq.enter_iron_condor_sequence(
        {'short_call':1,'long_call':2,'short_put':3,'long_put':4},
        {'short_call':10,'long_call':11,'short_put':12,'long_put':13}
    ))
    assert calls[3]['price'] == 12


def test_ic_target_is_charges_aware():
    strategy = IronCondorStrategy()
    metrics = strategy.calculate_target_metrics(40.0, 50)
    pnl = strategy.compute_pnl(40.0, metrics["target_close_premium"], 50)
    assert pnl["net_pnl"] >= metrics["target_net_profit"] - 1.0
    assert "required_gross_profit" in metrics
    assert "estimated_charges" in metrics


def test_ic_target_exit_blocked_when_not_net_positive_after_charges():
    strategy = IronCondorStrategy()
    strategy.min_net_target_profit = 500.0
    strategy.min_gross_target_profit = 600.0
    strategy.charges_buffer_multiplier = 3.0

    metrics = strategy.calculate_target_metrics(12.0, 25)
    assert metrics["target_possible"] == 0.0

    entry_time = datetime(2026, 5, 14, 9, 45, tzinfo=ZoneInfo("Asia/Kolkata"))
    current_time = datetime(2026, 5, 14, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    reason = strategy.get_exit_reason(
        entry_time,
        current_time,
        entry_premium=12.0,
        current_premium=metrics["target_close_premium"],
        qty=25,
    )
    assert reason is None


def test_ic_target_exit_allowed_when_net_positive_after_charges_buffer():
    strategy = IronCondorStrategy()
    strategy.min_net_target_profit = 100.0
    strategy.min_gross_target_profit = 250.0
    strategy.charges_buffer_multiplier = 1.10

    metrics = strategy.calculate_target_metrics(40.0, 50)
    assert metrics["target_possible"] == 1.0

    entry_time = datetime(2026, 5, 14, 9, 45, tzinfo=ZoneInfo("Asia/Kolkata"))
    current_time = datetime(2026, 5, 14, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    reason = strategy.get_exit_reason(
        entry_time,
        current_time,
        entry_premium=40.0,
        current_premium=metrics["target_close_premium"],
        qty=50,
    )
    assert reason == "TARGET"


def test_iron_condor_strategy_has_entry_regime_filter():
    strategy = IronCondorStrategy()

    ok, reason, diag = strategy.evaluate_entry_regime(
        spot=23700.0,
        live_iv=None,
    )

    assert isinstance(ok, bool)
    assert isinstance(reason, str)
    assert isinstance(diag, dict)
    assert "effective_iv" in diag


def test_iron_condor_calculate_strikes_accepts_live_iv():
    strategy = IronCondorStrategy()

    strikes = strategy.calculate_strikes(
        23756.30,
        live_iv=0.15,
    )

    assert isinstance(strikes, dict)
    assert "short_call" in strikes
    assert "long_call" in strikes
    assert "short_put" in strikes
    assert "long_put" in strikes
    assert strikes["long_call"] > strikes["short_call"]
    assert strikes["long_put"] < strikes["short_put"]


def test_expiry_day_uses_next_week_expiry_when_enabled(monkeypatch):
    strategy = IronCondorStrategy()
    strategy.skip_expiry_day_entry = True
    strategy.skip_expiry_day_entry_use_next_week = True

    def fake_weekly_expiry(day):
        if day == date(2026, 5, 19):
            return date(2026, 5, 19)
        return date(2026, 5, 26)

    monkeypatch.setattr(
        "backend.app.broker.samco_client.get_weekly_expiry",
        fake_weekly_expiry,
    )

    resolved = strategy.resolve_entry_expiry(date(2026, 5, 19))

    assert resolved == date(2026, 5, 26)


def test_ic_entry_blocks_on_expiry_day():
    strategy = IronCondorStrategy()
    strategy.entry_window_start = datetime.strptime("09:20", "%H:%M").time()
    strategy.entry_window_end = datetime.strptime("10:30", "%H:%M").time()
    strategy.skip_expiry_day_entry = True
    strategy.skip_expiry_day_entry_use_next_week = False
    strategy.one_per_day = False

    class S:
        active_trade = None
        last_trade_date = None
        last_ic_trade_date = None
        iron_condor_trade_date = None
        last_iron_condor_month = None

    expiry_dt = datetime(2026, 5, 19, 9, 45, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert strategy.can_enter_cycle(expiry_dt, S()) is False


def test_ic_entry_allows_expiry_day_with_next_week_skip():
    strategy = IronCondorStrategy()
    strategy.entry_window_start = datetime.strptime("09:20", "%H:%M").time()
    strategy.entry_window_end = datetime.strptime("10:30", "%H:%M").time()
    strategy.skip_expiry_day_entry = False
    strategy.skip_expiry_day_entry_use_next_week = True
    strategy.one_per_day = False

    class S:
        active_trade = None
        last_trade_date = None
        last_ic_trade_date = None
        iron_condor_trade_date = None
        last_iron_condor_month = None

    expiry_dt = datetime(2026, 5, 19, 9, 45, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert strategy.can_enter_cycle(expiry_dt, S()) is True


def test_get_weekly_expiry_can_skip_today_expiry():
    from backend.app.broker.samco_client import get_weekly_expiry

    expiry_today = get_weekly_expiry(date(2026, 5, 19), skip_today_if_expiry=False)
    expiry_next_week = get_weekly_expiry(date(2026, 5, 19), skip_today_if_expiry=True)

    assert expiry_today == date(2026, 5, 19)
    assert expiry_next_week == date(2026, 5, 26)


def test_ic_entry_blocks_after_one_trade_today():
    strategy = IronCondorStrategy()
    strategy.entry_window_start = datetime.strptime("09:20", "%H:%M").time()
    strategy.entry_window_end = datetime.strptime("10:30", "%H:%M").time()
    strategy.skip_expiry_day_entry = False
    strategy.one_per_day = True

    class S:
        active_trade = None
        last_trade_date = None
        last_ic_trade_date = "2026-05-14"
        iron_condor_trade_date = None
        last_iron_condor_month = None

    normal_dt = datetime(2026, 5, 14, 9, 45, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert strategy.can_enter_cycle(normal_dt, S()) is False


def test_ic_entry_viability_enforces_min_gross_profit():
    strategy = IronCondorStrategy()
    strategy.min_gross_profit = 500.0
    strategy.min_entry_premium = 5.0
    ok, reason, diagnostics = strategy.is_entry_credit_viable(8.0, 25, spread_width=50)
    assert ok is False
    assert reason == "target_gross_profit_below_minimum"
    assert diagnostics["min_gross_profit"] == 500.0


def test_ic_regime_filter_blocks_low_iv_in_high_probability_mode():
    strategy = IronCondorStrategy()
    strategy.high_probability_mode = True
    strategy.require_live_iv = False
    strategy.min_live_iv = 0.12
    strategy.max_live_iv = 0.24

    ok, reason, diagnostics = strategy.evaluate_entry_regime(spot=22500.0, live_iv=0.09)
    assert ok is False
    assert reason == "iv_too_low_for_premium_selling"
    assert diagnostics["threshold"] == 0.12


def test_ic_regime_filter_blocks_high_iv_in_high_probability_mode():
    strategy = IronCondorStrategy()
    strategy.high_probability_mode = True
    strategy.require_live_iv = False
    strategy.min_live_iv = 0.12
    strategy.max_live_iv = 0.24

    ok, reason, diagnostics = strategy.evaluate_entry_regime(spot=22500.0, live_iv=0.28)
    assert ok is False
    assert reason == "iv_too_high_for_high_probability_entry"
    assert diagnostics["threshold"] == 0.24


def test_ic_expected_move_filter_requires_extra_safety_buffer():
    strategy = IronCondorStrategy()
    strategy.min_safety_buffer_points = 40.0

    ok, diagnostics = strategy.is_expected_move_safe(
        spot=22000.0,
        short_distance=180.0,
        live_iv=0.12,
        days=1,
    )
    assert ok is False
    assert diagnostics["actual_margin"] < diagnostics["min_safety_buffer_points"]


def test_ic_close_premium_uses_actual_exit_leg_fills(tmp_path):
    class _StateManager:
        async def snapshot(self):
            return type("S", (), {"active_trade": None, "trade_count": 0, "daily_pnl": 0.0})()

        async def update(self, **kwargs):
            return None

    store = TradeStore(str(tmp_path / "trades.csv"))
    engine = TradingEngine(EventBus(), _StateManager(), store, broker=None)
    premium = engine._calculate_iron_condor_close_premium(
        [
            {"side": "SELL", "exit_price": 12.0},
            {"side": "BUY", "exit_price": 4.0},
            {"side": "SELL", "exit_price": 10.0},
            {"side": "BUY", "exit_price": 3.0},
        ]
    )
    assert premium == 15.0


def test_trade_store_derives_missing_ic_exit_price_from_exit_legs(tmp_path):
    store = TradeStore(str(tmp_path / "trades.csv"))
    store.append_trade(
        {
            "strategy": "IRON_CONDOR",
            "signal": "IRON_CONDOR",
            "symbol": "NIFTY",
            "underlying": "NIFTY 50",
            "expiry": "2026-05-19",
            "strike": "23600/23150",
            "entry_price": 60.30,
            "qty": 65,
            "status": "CLOSED",
            "entry_time": "2026-05-13T04:00:00+00:00",
            "exit_time": "2026-05-13T09:30:00+00:00",
            "gross_pnl": 100.0,
            "net_pnl": 70.0,
            "total_charges": 30.0,
            "exit_legs": [
                {"side": "SELL", "exit_price": 149.70, "price_source": "broker_quote_snapshot"},
                {"side": "BUY", "exit_price": 115.30, "price_source": "broker_quote_snapshot"},
                {"side": "SELL", "exit_price": 129.65, "price_source": "broker_quote_snapshot"},
                {"side": "BUY", "exit_price": 103.75, "price_source": "broker_quote_snapshot"},
            ],
        },
        daily_pnl=70.0,
    )
    rows = store.get_all_trades()
    assert float(rows[-1]["exit_price"]) == 60.30
    assert float(rows[-1]["exit_premium"]) == 60.30


def test_trade_store_recomputes_ic_summary_from_broker_exit_legs(tmp_path):
    store = TradeStore(str(tmp_path / "trades.csv"))
    store.append_trade(
        {
            "strategy": "IRON_CONDOR",
            "signal": "IRON_CONDOR",
            "symbol": "NIFTY",
            "underlying": "NIFTY 50",
            "expiry": "2026-05-26",
            "strike": "24000/23450",
            "entry_price": 57.30,
            "exit_price": 40.11,
            "exit_premium": 40.11,
            "qty": 65,
            "status": "CLOSED",
            "entry_time": "2026-05-19T04:50:13+00:00",
            "exit_time": "2026-05-19T07:47:27+00:00",
            "gross_pnl": 1117.35,
            "net_pnl": 921.87,
            "total_charges": 195.48,
            "pricing_source": "model_fallback",
            "exit_legs": [
                {"name": "short_call", "side": "SELL", "entry_price": 110.90, "exit_price": 100.35, "price_source": "broker_quote_snapshot"},
                {"name": "long_call", "side": "BUY", "entry_price": 80.05, "exit_price": 70.10, "price_source": "broker_quote_snapshot"},
                {"name": "short_put", "side": "SELL", "entry_price": 131.50, "exit_price": 121.65, "price_source": "broker_quote_snapshot"},
                {"name": "long_put", "side": "BUY", "entry_price": 105.05, "exit_price": 94.75, "price_source": "broker_quote_snapshot"},
            ],
        },
        daily_pnl=921.87,
    )

    row = store.get_all_trades()[-1]
    assert float(row["entry_price"]) == 57.30
    assert float(row["exit_price"]) == 57.15
    assert float(row["exit_premium"]) == 57.15
    assert float(row["gross_pnl"]) == 9.75
    assert float(row["net_pnl"]) == -185.73
    assert row["pricing_source"] == "broker_quote_snapshot"


def test_ic_leg_premium_math_is_stable_for_10000_cases(tmp_path):
    store = TradeStore(str(tmp_path / "trades.csv"))

    for case in range(1, 10001):
        entry_base = 20.0 + (case % 700) / 20.0
        exit_shift = ((case % 41) - 20) / 20.0
        qty = 25 + (case % 4) * 25

        legs = [
            {
                "side": "SELL",
                "entry_price": round(entry_base + 8.0, 2),
                "exit_price": round(entry_base + 8.0 + exit_shift, 2),
                "price_source": "broker_quote_snapshot",
            },
            {
                "side": "BUY",
                "entry_price": round(entry_base + 2.5, 2),
                "exit_price": round(entry_base + 2.5 + exit_shift / 2, 2),
                "price_source": "broker_quote_snapshot",
            },
            {
                "side": "SELL",
                "entry_price": round(entry_base + 7.0, 2),
                "exit_price": round(entry_base + 7.0 - exit_shift / 3, 2),
                "price_source": "broker_quote_snapshot",
            },
            {
                "side": "BUY",
                "entry_price": round(entry_base + 1.5, 2),
                "exit_price": round(entry_base + 1.5 - exit_shift / 4, 2),
                "price_source": "broker_quote_snapshot",
            },
        ]

        entry = store._derive_entry_premium_from_legs(legs)
        exit_premium = store._derive_exit_premium_from_legs(legs)

        expected_entry = round(
            legs[0]["entry_price"]
            - legs[1]["entry_price"]
            + legs[2]["entry_price"]
            - legs[3]["entry_price"],
            2,
        )
        expected_exit = round(
            legs[0]["exit_price"]
            - legs[1]["exit_price"]
            + legs[2]["exit_price"]
            - legs[3]["exit_price"],
            2,
        )

        assert store._legs_have_market_exit_prices(legs) is True
        assert entry == expected_entry
        assert exit_premium == expected_exit
        assert round((entry - exit_premium) * qty, 2) == round(
            (expected_entry - expected_exit) * qty,
            2,
        )


@pytest.mark.parametrize(
    ("exit_legs", "expected_source", "expected_exit", "expected_gross", "expected_net"),
    [
        ([], "unverified_summary", 40.11, 1117.35, 921.87),
        (
            [
                {"side": "SELL", "entry_price": 110.90, "exit_price": 0.0, "price_source": "broker_quote_snapshot"},
                {"side": "BUY", "entry_price": 80.05, "exit_price": 70.10, "price_source": "broker_quote_snapshot"},
                {"side": "SELL", "entry_price": 131.50, "exit_price": 121.65, "price_source": "broker_quote_snapshot"},
                {"side": "BUY", "entry_price": 105.05, "exit_price": 94.75, "price_source": "broker_quote_snapshot"},
            ],
            "unverified_summary",
            40.11,
            1117.35,
            921.87,
        ),
        (
            [
                {"side": "SELL", "entry_price": 110.90, "exit_price": 100.35},
                {"side": "BUY", "entry_price": 80.05, "exit_price": 70.10},
                {"side": "SELL", "entry_price": 131.50, "exit_price": 121.65},
                {"side": "BUY", "entry_price": 105.05, "exit_price": 94.75},
            ],
            "unverified_summary",
            40.11,
            1117.35,
            921.87,
        ),
        (
            [
                {"side": "SELL", "entry_price": 110.90, "exit_price": 100.35, "price_source": "model_fallback"},
                {"side": "BUY", "entry_price": 80.05, "exit_price": 70.10, "price_source": "model_fallback"},
                {"side": "SELL", "entry_price": 131.50, "exit_price": 121.65, "price_source": "model_fallback"},
                {"side": "BUY", "entry_price": 105.05, "exit_price": 94.75, "price_source": "model_fallback"},
            ],
            "unverified_summary",
            40.11,
            1117.35,
            921.87,
        ),
        (
            [
                {"side": "SELL", "entry_price": 110.90, "exit_price": 100.35, "price_source": "broker_quote_snapshot_cached"},
                {"side": "BUY", "entry_price": 80.05, "exit_price": 70.10, "price_source": "broker_quote_snapshot_cached"},
                {"side": "SELL", "entry_price": 131.50, "exit_price": 121.65, "price_source": "broker_quote_snapshot_cached"},
                {"side": "BUY", "entry_price": 105.05, "exit_price": 94.75, "price_source": "broker_quote_snapshot_cached"},
            ],
            "broker_quote_snapshot",
            57.15,
            9.75,
            -185.73,
        ),
    ],
)
def test_ic_closed_summary_source_gate_pass_fail_cases(
    tmp_path,
    exit_legs,
    expected_source,
    expected_exit,
    expected_gross,
    expected_net,
):
    store = TradeStore(str(tmp_path / "trades.csv"))
    store.append_trade(
        {
            "strategy": "IRON_CONDOR",
            "signal": "IRON_CONDOR",
            "symbol": "NIFTY",
            "underlying": "NIFTY 50",
            "expiry": "2026-05-26",
            "strike": "24000/23450",
            "entry_price": 57.30,
            "exit_price": 40.11,
            "exit_premium": 40.11,
            "qty": 65,
            "status": "CLOSED",
            "entry_time": "2026-05-19T04:50:13+00:00",
            "exit_time": "2026-05-19T07:47:27+00:00",
            "gross_pnl": 1117.35,
            "net_pnl": 921.87,
            "total_charges": 195.48,
            "pricing_source": "model_fallback",
            "exit_legs": exit_legs,
        },
        daily_pnl=921.87,
    )

    row = store.get_all_trades()[-1]
    assert row["pricing_source"] == expected_source
    assert float(row["exit_price"]) == expected_exit
    assert float(row["gross_pnl"]) == expected_gross
    assert float(row["net_pnl"]) == expected_net


def test_ic_model_fallback_critical_disables_trading(tmp_path, monkeypatch):
    class _State:
        def __init__(self):
            self.active_trade = {
                "strategy": "IRON_CONDOR",
                "status": "OPEN",
                "symbol": "NIFTY",
                "entry_time": "2026-05-14T04:00:00+00:00",
                "entry_price": 40.0,
                "qty": 50,
                "current_legs": [],
            }
            self.trade_count = 1
            self.daily_pnl = 0.0
            self.trading_enabled = True
            self.circuit_breaker_open = False
            self.last_order_failed = False
            self.last_risk_breach = None
            self.live_pnl = 0.0

    class _StateManager:
        def __init__(self):
            self.state = _State()

        async def snapshot(self):
            return self.state

        async def update(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self.state, key, value)

    class _Bus:
        def __init__(self):
            self.events = []

        async def publish(self, event_type, payload):
            self.events.append((event_type, payload))

    store = TradeStore(str(tmp_path / "trades.csv"))
    sm = _StateManager()
    bus = _Bus()
    engine = TradingEngine(bus, sm, store, broker=None)

    monkeypatch.setattr(trading_engine_module, "_QUOTE_DEGRADED_WARN_TICKS", 1)
    monkeypatch.setattr(trading_engine_module, "_QUOTE_DEGRADED_CRITICAL_TICKS", 1)

    async def _broken_snapshot(trade):
        raise RuntimeError("quote down")

    monkeypatch.setattr(engine, "_get_live_iron_condor_close_snapshot", _broken_snapshot)
    monkeypatch.setattr(
        engine.expiry_safety,
        "should_force_exit",
        lambda current_time, entry_time: (False, None),
    )

    asyncio.run(engine._monitor_iron_condor_trade(sm.state.active_trade))

    assert sm.state.trading_enabled is False
    assert sm.state.circuit_breaker_open is True
    assert sm.state.last_order_failed is True
    assert sm.state.last_risk_breach == "ic_quote_degradation_critical"
    assert sm.state.active_trade["display_pnl_is_estimated"] is True


def test_reconciliation_pnl_mismatch_disables_trading_instead_of_overwriting(monkeypatch):
    class _State:
        def __init__(self):
            self.active_trade = None
            self.daily_pnl = 100.0
            self.live_pnl = 25.0
            self.trading_enabled = True
            self.circuit_breaker_open = False
            self.last_order_failed = False
            self.last_risk_breach = None

    class _StateManager:
        def __init__(self):
            self.state = _State()

        async def snapshot(self):
            return self.state

        async def update(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self.state, key, value)

    class _Broker:
        async def get_positions(self):
            return []

        async def get_trade_book(self):
            return [{"pnl": 900.0}]

    class _Bus:
        def __init__(self):
            self.events = []

        async def publish(self, event_type, payload):
            self.events.append((event_type, payload))

    bus = _Bus()
    sm = _StateManager()
    engine = ReconciliationEngine(_Broker(), sm, event_bus=bus)

    patched_settings = type("S", (), {"is_live": True})()
    monkeypatch.setattr(reconciliation_module, "settings", patched_settings)

    result = asyncio.run(engine.run_once())

    assert result["issues_found"] == 1
    assert sm.state.daily_pnl == 100.0
    assert sm.state.live_pnl == 25.0
    assert sm.state.trading_enabled is False
    assert sm.state.circuit_breaker_open is True
    assert sm.state.last_risk_breach == "reconciliation_pnl_mismatch"
    assert bus.events[-1][0] == "RECONCILIATION_PNL_MISMATCH"


def test_emergency_flatten_ic_succeeds_and_verifies_all_legs_flat(monkeypatch):
    class _State:
        def __init__(self):
            self.active_trade = {
                "strategy": "IRON_CONDOR",
                "symbol": "NIFTY",
                "underlying": "NIFTY 50",
                "qty": 50,
                "entry_price": 40.0,
                "current_premium": 32.0,
                "legs": [
                    {"symbol": "NIFTY 2026-05-19 23600 CE"},
                    {"symbol": "NIFTY 2026-05-19 23700 CE"},
                    {"symbol": "NIFTY 2026-05-19 23150 PE"},
                    {"symbol": "NIFTY 2026-05-19 23050 PE"},
                ],
            }
            self.trading_enabled = True
            self.circuit_breaker_open = False
            self.last_order_failed = False
            self.last_risk_breach = None
            self.manual_intervention_required = False
            self.emergency_flatten_verified = False

    class _StateManager:
        def __init__(self):
            self.state = _State()

        async def snapshot(self):
            return self.state

        async def update(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self.state, key, value)

    class _Broker:
        def __init__(self):
            self.position_calls = 0

        async def get_positions(self):
            self.position_calls += 1
            if self.position_calls == 1:
                return [
                    {"tradingSymbol": "NIFTY 2026-05-19 23600 CE", "netQty": -50},
                    {"tradingSymbol": "NIFTY 2026-05-19 23700 CE", "netQty": 50},
                ]
            return []

        async def place_order(self, symbol, side, quantity):
            return {"status": "Success", "orderNumber": f"{side}-{symbol}-{quantity}"}

        async def get_order_status(self, order_id):
            return {"orderDetails": {"orderStatus": "COMPLETE", "filledQty": 50, "orderNumber": order_id}}

        async def get_actual_fill_price(self, order_id):
            return 100.0

    class _Bus:
        async def publish(self, *_args, **_kwargs):
            return None

    class _Engine:
        async def _exit_iron_condor_trade(self, trade, **_kwargs):
            return {
                **trade,
                "exit_legs": [
                    {"symbol": "NIFTY 2026-05-19 23600 CE", "exit_order_id": "OID-1"},
                    {"symbol": "NIFTY 2026-05-19 23700 CE", "exit_order_id": "OID-2"},
                ],
            }

    patched_settings = type("S", (), {"is_paper": False, "nifty_symbol": "NIFTY 50"})()
    monkeypatch.setattr(market_scheduler_module, "settings", patched_settings)

    scheduler = object.__new__(MarketScheduler)
    scheduler.state = _StateManager()
    scheduler.broker = _Broker()
    scheduler.event_bus = _Bus()
    scheduler.engine = _Engine()
    scheduler._last_manual_flatten_time = 0.0
    scheduler._last_signal_time = 0.0

    result = asyncio.run(scheduler._flatten_iron_condor_trade(scheduler.state.state.active_trade))

    assert result["status"] == "flattened"
    assert result["emergency_flatten_verified"] is True
    assert scheduler.state.state.emergency_flatten_verified is True


def test_emergency_flatten_ic_partial_failure_disables_trading(monkeypatch):
    class _State:
        def __init__(self):
            self.active_trade = {
                "strategy": "IRON_CONDOR",
                "symbol": "NIFTY",
                "underlying": "NIFTY 50",
                "qty": 50,
                "entry_price": 40.0,
                "current_premium": 32.0,
                "legs": [
                    {"symbol": "NIFTY 2026-05-19 23600 CE"},
                    {"symbol": "NIFTY 2026-05-19 23700 CE"},
                ],
            }
            self.trading_enabled = True
            self.circuit_breaker_open = False
            self.last_order_failed = False
            self.last_risk_breach = None
            self.manual_intervention_required = False
            self.emergency_flatten_verified = False

    class _StateManager:
        def __init__(self):
            self.state = _State()

        async def snapshot(self):
            return self.state

        async def update(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self.state, key, value)

    class _Broker:
        async def get_positions(self):
            return [{"tradingSymbol": "NIFTY 2026-05-19 23600 CE", "netQty": -50}]

        async def place_order(self, symbol, side, quantity):
            return {"status": "Success", "orderNumber": f"{side}-{symbol}-{quantity}"}

        async def get_order_status(self, order_id):
            return {"orderDetails": {"orderStatus": "COMPLETE", "filledQty": 50, "orderNumber": order_id}}

        async def get_actual_fill_price(self, order_id):
            return 100.0

    class _Bus:
        def __init__(self):
            self.events = []

        async def publish(self, event_type, payload):
            self.events.append((event_type, payload))

    class _Engine:
        async def _exit_iron_condor_trade(self, trade, **_kwargs):
            return {
                **trade,
                "exit_legs": [
                    {"symbol": "NIFTY 2026-05-19 23600 CE", "exit_order_id": "OID-1"},
                ],
            }

    patched_settings = type("S", (), {"is_paper": False, "nifty_symbol": "NIFTY 50"})()
    monkeypatch.setattr(market_scheduler_module, "settings", patched_settings)

    scheduler = object.__new__(MarketScheduler)
    scheduler.state = _StateManager()
    scheduler.broker = _Broker()
    scheduler.event_bus = _Bus()
    scheduler.engine = _Engine()
    scheduler._last_manual_flatten_time = 0.0
    scheduler._last_signal_time = 0.0

    result = asyncio.run(scheduler._flatten_iron_condor_trade(scheduler.state.state.active_trade))

    assert result["status"] == "manual_intervention_required"
    assert scheduler.state.state.trading_enabled is False
    assert scheduler.state.state.manual_intervention_required is True
    assert scheduler.state.state.last_risk_breach == "emergency_flatten_unverified"
    assert scheduler.event_bus.events[-1][0] == "EMERGENCY_FLATTEN_UNVERIFIED"


def test_reconciliation_reconstructs_orphan_broker_ic_into_safe_state(monkeypatch):
    class _State:
        def __init__(self):
            self.active_trade = None
            self.daily_pnl = 0.0
            self.live_pnl = 0.0
            self.trading_enabled = True
            self.circuit_breaker_open = False
            self.last_order_failed = False
            self.last_risk_breach = None
            self.manual_intervention_required = False
            self.reconstructed_ic_status = None
            self.hedge_integrity_status = None
            self.broker_position_count = 0
            self.positions = {}
            self.unrealized_pnl = 0.0

    class _StateManager:
        def __init__(self):
            self.state = _State()

        async def snapshot(self):
            return self.state

        async def update(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self.state, key, value)

    class _Broker:
        async def get_positions(self):
            return [
                {"tradingSymbol": "NIFTY 2026-05-19 23600 CE", "netQty": -50, "averagePrice": 15},
                {"tradingSymbol": "NIFTY 2026-05-19 23700 CE", "netQty": 50, "averagePrice": 10},
                {"tradingSymbol": "NIFTY 2026-05-19 23150 PE", "netQty": -50, "averagePrice": 14},
                {"tradingSymbol": "NIFTY 2026-05-19 23050 PE", "netQty": 50, "averagePrice": 9},
            ]

        async def get_trade_book(self):
            return []

    patched_settings = type("S", (), {"is_live": True, "stop_loss_pct": 0.45, "t1_pct": 0.50, "t2_pct": 1.25})()
    monkeypatch.setattr(reconciliation_module, "settings", patched_settings)

    sm = _StateManager()
    engine = ReconciliationEngine(_Broker(), sm, event_bus=None)
    result = asyncio.run(engine.run_once())

    assert result["issues_found"] == 1
    assert sm.state.active_trade["strategy"] == "IRON_CONDOR"
    assert sm.state.trading_enabled is False
    assert sm.state.manual_intervention_required is True
    assert sm.state.reconstructed_ic_status == "reconstructed_ic"
    assert sm.state.hedge_integrity_status == "intact"
    assert sm.state.broker_position_count == 4
    assert engine.last_result is not None


def test_reconciliation_broken_hedge_detected_and_blocks_new_entries(monkeypatch):
    class _State:
        def __init__(self):
            self.active_trade = None
            self.daily_pnl = 0.0
            self.live_pnl = 0.0
            self.trading_enabled = True
            self.circuit_breaker_open = False
            self.last_order_failed = False
            self.last_risk_breach = None
            self.manual_intervention_required = False
            self.reconstructed_ic_status = None
            self.hedge_integrity_status = None
            self.broker_position_count = 0
            self.positions = {}
            self.unrealized_pnl = 0.0

    class _StateManager:
        def __init__(self):
            self.state = _State()

        async def snapshot(self):
            return self.state

        async def update(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self.state, key, value)

    class _Broker:
        async def get_positions(self):
            return [
                {"tradingSymbol": "NIFTY 2026-05-19 23600 CE", "netQty": -50, "averagePrice": 15},
                {"tradingSymbol": "NIFTY 2026-05-19 23550 CE", "netQty": 50, "averagePrice": 10},
                {"tradingSymbol": "NIFTY 2026-05-19 23150 PE", "netQty": -50, "averagePrice": 14},
                {"tradingSymbol": "NIFTY 2026-05-19 23200 PE", "netQty": 50, "averagePrice": 9},
            ]

        async def get_trade_book(self):
            return []

    patched_settings = type("S", (), {"is_live": True, "stop_loss_pct": 0.45, "t1_pct": 0.50, "t2_pct": 1.25})()
    monkeypatch.setattr(reconciliation_module, "settings", patched_settings)

    sm = _StateManager()
    engine = ReconciliationEngine(_Broker(), sm, event_bus=None)
    asyncio.run(engine.run_once())

    assert sm.state.active_trade is None
    assert sm.state.trading_enabled is False
    assert sm.state.manual_intervention_required is True
    assert sm.state.reconstructed_ic_status == "broken_hedge"
    assert sm.state.hedge_integrity_status == "broken"


def test_reconciliation_clears_local_trade_only_after_verified_flat(monkeypatch):
    class _State:
        def __init__(self):
            self.active_trade = {"symbol": "NIFTY", "qty": 50}
            self.daily_pnl = 0.0
            self.live_pnl = 5.0
            self.trading_enabled = True
            self.positions = {"NIFTY": 50}
            self.unrealized_pnl = 5.0

    class _StateManager:
        def __init__(self):
            self.state = _State()

        async def snapshot(self):
            return self.state

        async def update(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self.state, key, value)

    class _Broker:
        def __init__(self):
            self.calls = 0

        async def get_positions(self):
            self.calls += 1
            return []

        async def get_trade_book(self):
            return []

    patched_settings = type("S", (), {"is_live": True, "stop_loss_pct": 0.45, "t1_pct": 0.50, "t2_pct": 1.25})()
    monkeypatch.setattr(reconciliation_module, "settings", patched_settings)

    sm = _StateManager()
    engine = ReconciliationEngine(_Broker(), sm, event_bus=None)
    result = asyncio.run(engine.run_once())

    assert result["issues_found"] == 1
    assert sm.state.active_trade is None
    assert sm.state.positions == {}
    assert engine.last_result is not None

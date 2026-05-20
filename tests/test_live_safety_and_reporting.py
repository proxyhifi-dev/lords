from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timedelta, timezone

if "fastapi" not in sys.modules:
    fastapi_mod = types.ModuleType("fastapi")

    class _FastAPI:
        def __init__(self, *args, **kwargs):
            pass

        def add_middleware(self, *args, **kwargs):
            return None

        def mount(self, *args, **kwargs):
            return None

        def get(self, *args, **kwargs):
            return lambda fn: fn

        def post(self, *args, **kwargs):
            return lambda fn: fn

    class _JSONResponse:
        def __init__(self, content=None, status_code=200):
            self.status_code = status_code
            self.body = json.dumps(content or {}).encode("utf-8")

    class _FileResponse:
        def __init__(self, *args, **kwargs):
            self.status_code = 200
            self.body = b""

    class _StaticFiles:
        def __init__(self, *args, **kwargs):
            pass

    class _CORSMiddleware:
        pass

    fastapi_mod.FastAPI = _FastAPI
    responses_mod = types.ModuleType("fastapi.responses")
    responses_mod.JSONResponse = _JSONResponse
    responses_mod.FileResponse = _FileResponse
    staticfiles_mod = types.ModuleType("fastapi.staticfiles")
    staticfiles_mod.StaticFiles = _StaticFiles
    cors_mod = types.ModuleType("fastapi.middleware.cors")
    cors_mod.CORSMiddleware = _CORSMiddleware

    sys.modules["fastapi"] = fastapi_mod
    sys.modules["fastapi.responses"] = responses_mod
    sys.modules["fastapi.staticfiles"] = staticfiles_mod
    sys.modules["fastapi.middleware.cors"] = cors_mod

import backend.main as main_module
from backend.app.core.event_bus import EventBus
from backend.app.engine.reconciliation import ReconciliationEngine
from backend.app.engine.trading_engine import TradingEngine
from backend.app.scheduler import market_scheduler as market_scheduler_module
from backend.app.scheduler.market_scheduler import MarketScheduler
from backend.app.storage.trade_store import TradeStore
from backend.app.utils import strategy_validation as strategy_validation_module


def _decode_json_response(response):
    return json.loads(response.body.decode("utf-8"))


def test_cached_quote_fresh_allows_iron_condor_snapshot(tmp_path):
    class _Broker:
        async def get_quote(self, symbol_name, exchange="NFO"):
            return {"quoteDetails": {"bestBidPrice": "0", "bestAskPrice": "0", "lastTradedPrice": "0"}}

        @staticmethod
        def parse_ltp(quote):
            return 0.0

        @staticmethod
        def parse_bid_ask(quote):
            return 0.0, 0.0

    class _State:
        async def snapshot(self):
            return type("S", (), {"active_trade": None, "trade_count": 0, "daily_pnl": 0.0})()

        async def update(self, **kwargs):
            return None

    engine = TradingEngine(EventBus(), _State(), TradeStore(str(tmp_path / "trades.csv")), broker=_Broker())
    engine._remember_ic_quote("NIFTY19MAY2623600CE", {"quoteDetails": {}}, 149.7, 150.1, 150.0)

    premium, legs, source = asyncio.run(
        engine._get_live_iron_condor_close_snapshot(
            {
                "legs": [
                    {
                        "name": "short_call",
                        "symbol": "NIFTY19MAY2623600CE",
                        "side": "SELL",
                        "entry_price": 149.7,
                    }
                ]
            }
        )
    )

    assert source == "broker_quote_snapshot_cached"
    assert premium > 0
    assert legs[0]["quote_age_sec"] >= 0


def test_cached_quote_stale_rejected_for_iron_condor_snapshot(tmp_path):
    class _Broker:
        async def get_quote(self, symbol_name, exchange="NFO"):
            return {"quoteDetails": {"bestBidPrice": "0", "bestAskPrice": "0", "lastTradedPrice": "0"}}

        @staticmethod
        def parse_ltp(quote):
            return 0.0

        @staticmethod
        def parse_bid_ask(quote):
            return 0.0, 0.0

    class _State:
        async def snapshot(self):
            return type("S", (), {"active_trade": None, "trade_count": 0, "daily_pnl": 0.0})()

        async def update(self, **kwargs):
            return None

    engine = TradingEngine(EventBus(), _State(), TradeStore(str(tmp_path / "trades.csv")), broker=_Broker())
    engine._remember_ic_quote("NIFTY19MAY2623600CE", {"quoteDetails": {}}, 149.7, 150.1, 150.0)
    engine._ic_quote_cache["NIFTY19MAY2623600CE"]["timestamp"] = datetime.now(timezone.utc) - timedelta(seconds=10)

    try:
        asyncio.run(
            engine._get_live_iron_condor_close_snapshot(
                {
                    "legs": [
                        {
                            "name": "short_call",
                            "symbol": "NIFTY19MAY2623600CE",
                            "side": "SELL",
                            "entry_price": 149.7,
                        }
                    ]
                }
            )
        )
        raise AssertionError("expected stale quote rejection")
    except RuntimeError as exc:
        assert "Invalid IC close quote" in str(exc)


def test_scheduler_tick_handles_broker_quote_timeout(monkeypatch):
    class _Broker:
        async def get_index_quote(self, _symbol):
            raise asyncio.TimeoutError()

    scheduler = object.__new__(MarketScheduler)
    scheduler.broker = _Broker()
    scheduler._last_tick_time = 0.0
    scheduler._consecutive_quote_failures = 0
    scheduler._last_broker_error_time = 0.0

    fake_settings = type(
        "S",
        (),
        {
            "broker_quote_timeout_seconds": 1,
            "closed_log_interval_seconds": 60,
            "nifty_symbol": "NIFTY 50",
        },
    )()
    monkeypatch.setattr(market_scheduler_module, "settings", fake_settings)
    monkeypatch.setattr(market_scheduler_module, "setting_int", lambda name: getattr(fake_settings, name))

    asyncio.run(scheduler._tick())
    assert scheduler._consecutive_quote_failures == 1


def test_scheduler_hard_stall_triggers_fail_safe(monkeypatch):
    scheduler = object.__new__(MarketScheduler)
    scheduler._last_tick_time = 0.0
    scheduler._last_good_quote_time = 9999999999.0
    calls = {}

    async def fake_fail_safe(reason):
        calls["reason"] = reason

    fake_settings = type(
        "S",
        (),
        {
            "scheduler_stall_warn_seconds": 1.0,
            "scheduler_stall_hard_seconds": 2.0,
            "deadman_timeout": 999999,
        },
    )()
    monkeypatch.setattr(market_scheduler_module, "settings", fake_settings)
    monkeypatch.setattr(market_scheduler_module, "setting_float", lambda name: getattr(fake_settings, name))
    monkeypatch.setattr(scheduler, "_fail_safe_on_data_loss", fake_fail_safe)

    asyncio.run(scheduler._handle_open_market_cycle())
    assert str(calls["reason"]).startswith("scheduler_stall_")


def test_scheduler_hard_stall_updates_tick_time(monkeypatch):
    scheduler = object.__new__(MarketScheduler)
    now = market_scheduler_module.wall_time.time()
    scheduler._last_tick_time = now - 3.0
    scheduler._last_good_quote_time = 9999999999.0
    calls = {}

    async def fake_fail_safe(reason):
        calls["reason"] = reason

    fake_settings = type(
        "S",
        (),
        {
            "scheduler_stall_warn_seconds": 1.0,
            "scheduler_stall_hard_seconds": 2.0,
            "deadman_timeout": 999999,
        },
    )()
    monkeypatch.setattr(market_scheduler_module, "settings", fake_settings)
    monkeypatch.setattr(market_scheduler_module, "setting_float", lambda name: getattr(fake_settings, name))
    monkeypatch.setattr(scheduler, "_fail_safe_on_data_loss", fake_fail_safe)

    asyncio.run(scheduler._handle_open_market_cycle())

    assert str(calls["reason"]).startswith("scheduler_stall_")
    assert scheduler._last_tick_time >= now
    assert scheduler._last_good_quote_time >= now


def test_samco_call_sdk_relogs_on_auth_exception(monkeypatch):
    from backend.app.broker.samco_client import SamcoClient

    client = SamcoClient()
    client._session_live = True
    calls = {"count": 0}
    logins = {"count": 0}

    def fake_fn():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("Unauthorized request")
        return {"status": "Success"}

    async def fake_login():
        logins["count"] += 1
        client._session_live = True
        return {"status": "Success", "sessionToken": "ok"}

    monkeypatch.setattr(client, "login", fake_login)
    out = asyncio.run(client._call_sdk(fake_fn, "healthcheck"))
    assert out["status"] == "Success"
    assert logins["count"] == 1


def test_dashboard_safe_status_under_degraded_state(monkeypatch):
    class _State:
        bot_running = True
        trading_mode = "PAPER"
        trading_enabled = False
        spot_price = 23390.0
        current_iv = 0.18
        signal = None
        active_trade = None
        daily_pnl = -50.0
        live_pnl = 0.0
        trade_count = 0
        last_iron_condor_month = None
        last_ic_trade_date = None
        circuit_breaker_open = True
        last_risk_breach = "reconciliation_verification_uncertain"
        broker_position_count = 2
        reconstructed_ic_status = "broken_hedge"
        hedge_integrity_status = "broken"
        emergency_flatten_verified = False
        emergency_flatten_attempts = 2
        emergency_flatten_unclosed_symbols = ["NIFTY19MAY2623600CE"]
        emergency_flatten_last_error = "ambiguous_exit_order_status"
        emergency_flatten_order_proof = [{"symbol": "NIFTY19MAY2623600CE", "status": "OPEN"}]
        manual_intervention_required = True

    class _StateManager:
        async def snapshot(self):
            return _State()

    class _TradeStore:
        def get_all_trades(self):
            return []

    class _Scheduler:
        state = _StateManager()
        trade_store = _TradeStore()
        engine = type("E", (), {"iron_condor_strategy": None})()
        running = True
        _reconciler = type("R", (), {"last_result": {"status": "issues_found", "issues_found": 1}})()

        @staticmethod
        def get_status_summary():
            return {"running": True, "last_tick_age_sec": 99.0, "last_good_quote_age_sec": 99.0}

    fake_settings = type(
        "S",
        (),
        {
            "mode": "paper",
            "strategy_type": "iron_condor",
            "max_daily_loss": 3000.0,
            "ic_one_per_day": True,
            "ic_skip_expiry_day_entry": True,
        },
    )()
    monkeypatch.setattr(main_module, "settings", fake_settings)
    monkeypatch.setattr(market_scheduler_module, "scheduler", _Scheduler())

    payload = _decode_json_response(asyncio.run(main_module.dashboard()))
    assert payload["manual_intervention_required"] is True
    assert payload["emergency_flatten_verified"] is False
    assert payload["broker_position_count"] == 2
    assert payload["reconstructed_ic_status"] == "broken_hedge"
    assert payload["emergency_flatten_last_error"] == "ambiguous_exit_order_status"


def test_reconciliation_symbol_parser_handles_common_samco_variants():
    engine = ReconciliationEngine(None, None, event_bus=None)
    symbols = [
        "NIFTY19MAY2623700CE",
        "NIFTY 19MAY26 23700 CE",
        "NIFTY 2026-05-19 23700 CE",
        "NIFTY50 19MAY26 23700 CE",
    ]
    for symbol in symbols:
        parsed = engine._extract_option_metadata({"tradingSymbol": symbol, "netQty": -65, "averagePrice": 150})
        assert parsed is not None
        assert parsed["option_type"] == "CE"
        assert parsed["strike"] == 23700
        assert parsed["expiry"] == "2026-05-19"


def test_reconciliation_symbol_parser_invalid_symbol_fails_safely():
    engine = ReconciliationEngine(None, None, event_bus=None)
    assert engine._extract_option_metadata({"tradingSymbol": "NIFTY RANDOM SYMBOL", "netQty": 65}) is None


def test_reconstruction_mixed_symbol_structures_trigger_manual_review():
    engine = ReconciliationEngine(None, None, event_bus=None)
    reconstruction = engine._reconstruct_iron_condor_from_positions(
        [
            {"tradingSymbol": "NIFTY19MAY2623600CE", "netQty": -50, "averagePrice": 15},
            {"tradingSymbol": "NIFTY 19MAY26 23700 CE", "netQty": 50, "averagePrice": 10},
            {"tradingSymbol": "NIFTY 2026-05-19 23150 PE", "netQty": -50, "averagePrice": 14},
            {"tradingSymbol": "BROKEN", "netQty": 50, "averagePrice": 9},
        ]
    )
    assert reconstruction["trade"] is None
    assert reconstruction["manual_intervention_required"] is True


def test_strategy_validation_report_generator_writes_artifacts(tmp_path, monkeypatch):
    trades_file = tmp_path / "trades.csv"
    docs_dir = tmp_path / "docs"
    store = TradeStore(str(trades_file))
    store.append_trade(
        {
            "strategy": "IRON_CONDOR",
            "signal": "IRON_CONDOR",
            "symbol": "NIFTY",
            "underlying": "NIFTY 50",
            "status": "CLOSED",
            "entry_time": "2026-05-13T09:30:00+05:30",
            "exit_time": "2026-05-13T14:30:00+05:30",
            "qty": 65,
            "entry_price": 60.0,
            "exit_price": 50.0,
            "gross_pnl": 650.0,
            "net_pnl": 520.0,
            "total_charges": 130.0,
            "reason": "TARGET",
        },
        daily_pnl=520.0,
    )
    store.append_trade(
        {
            "strategy": "IRON_CONDOR",
            "signal": "IRON_CONDOR",
            "symbol": "NIFTY",
            "underlying": "NIFTY 50",
            "status": "CLOSED",
            "entry_time": "2026-05-14T09:30:00+05:30",
            "exit_time": "2026-05-14T14:30:00+05:30",
            "qty": 65,
            "entry_price": 40.0,
            "exit_price": 52.0,
            "gross_pnl": -780.0,
            "net_pnl": -910.0,
            "total_charges": 130.0,
            "reason": "STOP_LOSS",
        },
        daily_pnl=-390.0,
    )

    monkeypatch.setattr(strategy_validation_module, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(strategy_validation_module, "MARKDOWN_PATH", docs_dir / "strategy-validation-report.md")
    monkeypatch.setattr(strategy_validation_module, "SUMMARY_PATH", docs_dir / "strategy-validation-summary.json")
    monkeypatch.setattr(strategy_validation_module, "settings", type("S", (), {"trades_file": str(trades_file)})())

    report = strategy_validation_module.generate_strategy_validation_artifacts()
    summary = json.loads((docs_dir / "strategy-validation-summary.json").read_text(encoding="utf-8"))

    assert report.total_trades == 2
    assert summary["stop_loss_trades"] == 1
    assert (docs_dir / "strategy-validation-report.md").exists()

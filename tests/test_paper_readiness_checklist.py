from __future__ import annotations

import asyncio
import json
import sys
import types

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
from backend.app.scheduler import market_scheduler as market_scheduler_module
from backend.app.strategy.iron_condor_strategy import IronCondorStrategy


def _decode_json_response(response):
    return json.loads(response.body.decode("utf-8"))


class _FakeState:
    def __init__(self):
        self.bot_running = True
        self.trading_mode = "PAPER"
        self.trading_enabled = True
        self.spot_price = 23450.5
        self.current_iv = 0.16
        self.signal = "IRON_CONDOR"
        self.daily_pnl = 125.0
        self.live_pnl = 42.5
        self.trade_count = 1
        self.last_iron_condor_month = 5
        self.last_ic_trade_date = "2026-05-14"
        self.last_trade_date = "2026-05-14"
        self.circuit_breaker_open = False
        self.last_risk_breach = None
        self.broker_position_count = 0
        self.reconstructed_ic_status = "flat"
        self.hedge_integrity_status = "flat"
        self.emergency_flatten_verified = True
        self.emergency_flatten_attempts = 1
        self.emergency_flatten_unclosed_symbols = []
        self.emergency_flatten_last_error = None
        self.emergency_flatten_order_proof = []
        self.manual_intervention_required = False
        self.active_trade = {
            "strategy": "IRON_CONDOR",
            "signal": "IRON_CONDOR",
            "symbol": "NIFTY",
            "underlying": "NIFTY 50",
            "status": "OPEN",
            "entry_time": "2026-05-14T09:30:00+05:30",
            "expiry": "2026-05-20",
            "qty": 65,
            "entry_price": 60.3,
            "strike": "23600/23150",
            "strikes": {
                "short_call": 23600,
                "long_call": 23700,
                "short_put": 23150,
                "long_put": 23050,
            },
            "legs": [
                {"name": "short_call", "symbol": "NIFTY19MAY2623600CE", "side": "SELL", "option_type": "CE", "strike": 23600, "qty": 65, "entry_price": 149.7},
                {"name": "long_call", "symbol": "NIFTY19MAY2623700CE", "side": "BUY", "option_type": "CE", "strike": 23700, "qty": 65, "entry_price": 115.3},
                {"name": "short_put", "symbol": "NIFTY19MAY2623150PE", "side": "SELL", "option_type": "PE", "strike": 23150, "qty": 65, "entry_price": 129.65},
                {"name": "long_put", "symbol": "NIFTY19MAY2623050PE", "side": "BUY", "option_type": "PE", "strike": 23050, "qty": 65, "entry_price": 103.75},
            ],
            "current_legs": [],
        }


class _FakeStateManager:
    def __init__(self):
        self._state = _FakeState()

    async def snapshot(self):
        return self._state


class _FakeTradeStore:
    def get_all_trades(self):
        return [
            {
                "strategy": "IRON_CONDOR",
                "signal": "IRON_CONDOR",
                "symbol": "NIFTY",
                "underlying": "NIFTY 50",
                "status": "CLOSED",
                "entry_time": "2026-05-13T09:30:00+05:30",
                "exit_time": "2026-05-13T15:00:00+05:30",
                "expiry": "2026-05-19",
                "strike": "23600/23150",
                "qty": 65,
                "entry_price": 60.3,
                "exit_price": 50.5,
                "gross_pnl": 637.0,
                "total_charges": 120.0,
                "net_pnl": 517.0,
                "reason": "TARGET",
                "exit_legs_json": "[]",
            }
        ]


class _FakeBroker:
    def __init__(self):
        self.place_calls = []


class _FakeScheduler:
    def __init__(self):
        self.state = _FakeStateManager()
        self.trade_store = _FakeTradeStore()
        self.engine = type(
            "E",
            (),
            {
                "iron_condor_strategy": IronCondorStrategy(),
                "broker": _FakeBroker(),
            },
        )()
        self.running = True
        self._reconciler = type(
            "R",
            (),
            {
                "last_result": {
                    "status": "ok",
                    "issues_found": 0,
                    "actions_taken": [],
                }
            },
        )()

    def get_status_summary(self):
        return {
            "running": True,
            "last_tick_age_sec": 1.2,
            "last_good_quote_age_sec": 1.1,
            "consecutive_quote_failures": 0,
            "scheduler_stall_warn_seconds": 10,
            "scheduler_stall_hard_seconds": 60,
        }


def test_paper_readiness_checklist_fields_exist(monkeypatch):
    fake_scheduler = _FakeScheduler()
    fake_settings = type(
        "S",
        (),
        {
            "mode": "paper",
            "strategy_type": "iron_condor",
            "max_daily_loss": 3000.0,
            "ic_one_per_day": True,
            "ic_skip_expiry_day_entry": True,
            "nifty_symbol": "NIFTY 50",
        },
    )()

    async def fake_snapshot(_engine, trade):
        assert len(trade["legs"]) == 4
        return (
            55.2,
            [
                {"name": "short_call", "quote_age_sec": 1.2},
                {"name": "long_call", "quote_age_sec": 1.0},
                {"name": "short_put", "quote_age_sec": 0.8},
                {"name": "long_put", "quote_age_sec": 0.6},
            ],
            "broker_quote_snapshot",
        )

    monkeypatch.setattr(market_scheduler_module, "scheduler", fake_scheduler)
    monkeypatch.setattr(main_module, "settings", fake_settings)
    monkeypatch.setattr(main_module, "_get_live_iron_condor_snapshot", fake_snapshot)

    health_payload = _decode_json_response(asyncio.run(main_module.health()))
    status_payload = _decode_json_response(asyncio.run(main_module.status()))
    dashboard_payload = _decode_json_response(asyncio.run(main_module.dashboard()))
    ic_payload = _decode_json_response(asyncio.run(main_module.get_iron_condor_stats()))

    assert health_payload["mode"] == "PAPER"
    assert status_payload["mode"] == "PAPER"
    assert dashboard_payload["scheduler_status"]["running"] is True
    assert "reconciliation_status" in dashboard_payload
    assert "quote_age_sec" in ic_payload
    assert "target_possible" in ic_payload
    assert "manual_intervention_required" in dashboard_payload
    assert "broker_position_count" in dashboard_payload
    assert "reconstructed_ic_status" in dashboard_payload
    assert "hedge_integrity_status" in dashboard_payload
    assert "emergency_flatten_verified" in dashboard_payload
    assert "one_ic_per_day_enabled" in dashboard_payload
    assert "expiry_day_entry_blocked" in dashboard_payload
    assert len(dashboard_payload["active_trade"]["legs"]) == 4
    assert fake_scheduler.engine.broker.place_calls == []

    trade_row = dashboard_payload["trades"][0]
    assert "gross_pnl" in trade_row
    assert "total_charges" in trade_row
    assert "net_pnl" in trade_row
    assert "reason" in trade_row

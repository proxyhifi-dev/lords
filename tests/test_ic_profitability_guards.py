from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.core.event_bus import EventBus
from backend.app.engine.order_execution import ExpiryDaySafetyProtocol
from backend.app.engine.trading_engine import TradingEngine
from backend.app.storage.trade_store import TradeStore
from backend.app.strategy.iron_condor_strategy import IronCondorStrategy
from backend.app.utils.strategy_validation import build_strategy_report


IST = ZoneInfo("Asia/Kolkata")


def test_pre_expiry_late_entry_blocked() -> None:
    strategy = IronCondorStrategy()
    strategy.entry_window_start = datetime.strptime("09:00", "%H:%M").time()
    strategy.entry_window_end = datetime.strptime("12:00", "%H:%M").time()
    strategy.skip_one_day_before_expiry_after_time = datetime.strptime("11:15", "%H:%M").time()
    strategy.skip_expiry_day_entry = False
    strategy.one_per_day = False

    class State:
        active_trade = None

    assert strategy.can_enter_cycle(datetime(2026, 5, 18, 11, 20, tzinfo=IST), State()) is False


def test_eod_profit_lock_reason() -> None:
    strategy = IronCondorStrategy()
    strategy.eod_decision_time = datetime.strptime("14:35", "%H:%M").time()
    strategy.eod_min_net_profit = 10.0
    strategy.min_net_target_profit = 5000.0
    strategy.min_gross_target_profit = 5000.0

    reason = strategy.get_exit_reason(
        datetime(2026, 5, 14, 9, 45, tzinfo=IST),
        datetime(2026, 5, 14, 14, 36, tzinfo=IST),
        entry_premium=80.0,
        current_premium=70.0,
        qty=65,
    )

    assert reason == "EOD_PROFIT_LOCK"


def test_eod_no_positive_target_reason() -> None:
    strategy = IronCondorStrategy()
    strategy.eod_decision_time = datetime.strptime("14:35", "%H:%M").time()
    strategy.eod_min_net_profit = 9999.0
    strategy.min_net_target_profit = 5000.0
    strategy.min_gross_target_profit = 5000.0

    reason = strategy.get_exit_reason(
        datetime(2026, 5, 14, 9, 45, tzinfo=IST),
        datetime(2026, 5, 14, 14, 36, tzinfo=IST),
        entry_premium=12.0,
        current_premium=11.5,
        qty=25,
    )

    assert reason == "EOD_NO_POSITIVE_TARGET"


def test_expiry_safety_distinguishes_eod_and_actual_expiry() -> None:
    class Logger:
        def warning(self, *_args, **_kwargs):
            return None

    class Settings:
        ic_exit_time = "15:00"

    protocol = ExpiryDaySafetyProtocol(Settings(), Logger())
    eod_forced, eod_reason = protocol.should_force_exit(
        datetime(2026, 5, 14, 15, 1, tzinfo=IST),
        datetime(2026, 5, 14, 9, 45, tzinfo=IST),
        trade_expiry="2026-05-19",
    )
    expiry_forced, expiry_reason = protocol.should_force_exit(
        datetime(2026, 5, 19, 10, 0, tzinfo=IST),
        datetime(2026, 5, 14, 9, 45, tzinfo=IST),
        trade_expiry="2026-05-19",
    )

    assert eod_forced is True
    assert eod_reason.startswith("EOD_SAFETY_EXIT")
    assert expiry_forced is True
    assert expiry_reason.startswith("EXPIRY_DAY_HARD_EXIT")


def test_manual_flatten_does_not_double_close_same_trade(tmp_path) -> None:
    class State:
        def __init__(self):
            self.active_trade = {
                "strategy": "IRON_CONDOR",
                "status": "OPEN",
                "symbol": "NIFTY",
                "entry_time": "2026-05-14T09:45:00+05:30",
                "entry_price": 40.0,
                "qty": 65,
                "exit_in_progress": True,
            }
            self.trade_count = 1
            self.daily_pnl = 0.0
            self.consecutive_losses = 0

    class StateManager:
        def __init__(self):
            self.state = State()

        async def snapshot(self):
            return self.state

        async def update(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self.state, key, value)

    engine = TradingEngine(EventBus(), StateManager(), TradeStore(str(tmp_path / "trades.csv")), broker=None)
    result = asyncio.run(engine.emergency_exit_active_trade("MANUAL_FLATTEN"))

    assert result is False


def test_validation_reports_duplicate_and_malformed_rows() -> None:
    base = {
        "strategy": "IRON_CONDOR",
        "status": "CLOSED",
        "symbol": "NIFTY",
        "entry_time": "2026-05-14T09:45:00+05:30",
        "exit_time": "2026-05-14T14:45:00+05:30",
        "entry_price": "40",
        "qty": "65",
        "strike": "23600/23150",
        "gross_pnl": "100",
        "total_charges": "80",
        "net_pnl": "20",
        "reason": "MANUAL_FLATTEN",
    }

    report = build_strategy_report(
        [
            dict(base),
            dict(base, exit_time="2026-05-14T14:46:00+05:30"),
            dict(base, entry_time="2026-05-15T09:45:00+05:30", reason=""),
        ]
    )
    summary = report.to_summary()

    assert summary["raw_rows"] == 3
    assert summary["total_trades"] == 2
    assert summary["duplicate_rows"] == 1
    assert summary["malformed_rows"] == 1
    assert summary["generated_at"].endswith("+00:00")

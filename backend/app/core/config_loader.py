from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in ("1", "true", "yes", "on")


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _load_env() -> dict[str, str]:
    env = {}
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


@dataclass
class Settings:
    samco_user_id: str = ""
    samco_password: str = ""
    samco_yob: str = ""
    samco_access_token: str = ""
    mode: str = "paper"
    capital: float = 50000.0
    max_daily_loss: float = 5000.0
    max_trades: int = 1
    order_qty: int = 65
    stop_loss_pct: float = 0.45
    t1_pct: float = 0.50
    t2_pct: float = 1.25
    trailing_pct: float = 0.15
    breakeven_at_pct: float = 0.25
    min_entry_premium: float = 50.0
    min_option_volume: int = 500
    otm_distance: int = 1
    max_spread_pct: float = 0.04
    min_dte: int = 2
    max_dte: int = 7
    orb_duration_seconds: int = 900
    min_orb_range_pct: float = 0.0020
    max_orb_range_pct: float = 0.0080
    orb_atr_multiplier: float = 1.0
    breakout_buffer: float = 5.0
    max_breakout_extension_pct: float = 0.05
    max_option_spike_pct: float = 0.20
    max_consecutive_losses: int = 3
    max_drawdown_pct: float = 0.20
    min_orb_range: float = 50.0
    nifty_symbol: str = "NIFTY 50"
    nifty_exchange: str = "NSE"
    poll_seconds: int = 1
    signal_cooldown: int = 86400
    gap_threshold: float = 5.0
    trend_filter_enabled: bool = False
    skip_first_candle: bool = True
    no_entry_after: str = "13:30"
    square_off: str = "14:55"
    reconnect_max_attempts: int = 5
    reconnect_base_delay: float = 1.0
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: int = 30
    trades_file: str = "data/trades.csv"
    state_file: str = "data/runtime_state"
    log_file: str = "logs/bot.log"
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000
    frontend_dir: str = "frontend"
    deadman_timeout: int = 30

    @property
    def is_live(self) -> bool:
        return str(self.mode).strip().lower() == "live"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is not None:
        return _settings

    env = _load_env()
    combined = {**env, **os.environ}

    _settings = Settings(
        samco_user_id=str(combined.get("SAMCO_USER_ID", "")).strip(),
        samco_password=str(combined.get("SAMCO_PASSWORD", "")).strip(),
        samco_yob=str(combined.get("SAMCO_YOB", "")).strip(),
        samco_access_token=str(combined.get("SAMCO_ACCESS_TOKEN", "")).strip(),
        mode=str(combined.get("MODE", "paper")).strip(),
        capital=_parse_float(combined.get("CAPITAL", 50000.0), 50000.0),
        max_daily_loss=_parse_float(combined.get("MAX_DAILY_LOSS", 5000.0), 5000.0),
        max_trades=_parse_int(combined.get("MAX_TRADES", 1), 1),
        order_qty=_parse_int(combined.get("ORDER_QTY", 65), 65),
        stop_loss_pct=_parse_float(combined.get("STOP_LOSS_PCT", 0.45), 0.45),
        t1_pct=_parse_float(combined.get("T1_PCT", 0.50), 0.50),
        t2_pct=_parse_float(combined.get("T2_PCT", 1.25), 1.25),
        trailing_pct=_parse_float(combined.get("TRAILING_PCT", 0.15), 0.15),
        breakeven_at_pct=_parse_float(combined.get("BREAKEVEN_AT_PCT", 0.25), 0.25),
        min_entry_premium=_parse_float(combined.get("MIN_ENTRY_PREMIUM", 50.0), 50.0),
        min_option_volume=_parse_int(combined.get("MIN_OPTION_VOLUME", 500), 500),
        otm_distance=_parse_int(combined.get("OTM_DISTANCE", 1), 1),
        max_spread_pct=_parse_float(combined.get("MAX_SPREAD_PCT", 0.04), 0.04),
        max_breakout_extension_pct=_parse_float(combined.get("MAX_BREAKOUT_EXTENSION_PCT", 0.05), 0.05),
        max_option_spike_pct=_parse_float(combined.get("MAX_OPTION_SPIKE_PCT", 0.20), 0.20),
        max_consecutive_losses=_parse_int(combined.get("MAX_CONSECUTIVE_LOSSES", 3), 3),
        max_drawdown_pct=_parse_float(combined.get("MAX_DRAWDOWN_PCT", 0.20), 0.20),
        min_orb_range=_parse_float(combined.get("MIN_ORB_RANGE", 50.0), 50.0),
        min_dte=_parse_int(combined.get("MIN_DTE", 2), 2),
        max_dte=_parse_int(combined.get("MAX_DTE", 7), 7),
        orb_duration_seconds=_parse_int(combined.get("ORB_DURATION_SECONDS", 900), 900),
        min_orb_range_pct=_parse_float(combined.get("MIN_ORB_RANGE_PCT", 0.0020), 0.0020),
        max_orb_range_pct=_parse_float(combined.get("MAX_ORB_RANGE_PCT", 0.0080), 0.0080),
        orb_atr_multiplier=_parse_float(combined.get("ORB_ATR_MULTIPLIER", 1.0), 1.0),
        breakout_buffer=_parse_float(combined.get("BREAKOUT_BUFFER", 5.0), 5.0),
        nifty_symbol=str(combined.get("NIFTY_SYMBOL", "NIFTY 50")).strip(),
        nifty_exchange=str(combined.get("NIFTY_EXCHANGE", "NSE")).strip(),
        poll_seconds=_parse_int(combined.get("POLL_SECONDS", 1), 1),
        signal_cooldown=_parse_int(combined.get("SIGNAL_COOLDOWN", 86400), 86400),
        gap_threshold=_parse_float(combined.get("GAP_THRESHOLD", 5.0), 5.0),
        trend_filter_enabled=_parse_bool(combined.get("TREND_FILTER_ENABLED", False), False),
        skip_first_candle=_parse_bool(combined.get("SKIP_FIRST_CANDLE", True), True),
        no_entry_after=str(combined.get("NO_ENTRY_AFTER", "13:30")).strip(),
        square_off=str(combined.get("SQUARE_OFF", "14:55")).strip(),
        reconnect_max_attempts=_parse_int(combined.get("RECONNECT_MAX_ATTEMPTS", 5), 5),
        reconnect_base_delay=_parse_float(combined.get("RECONNECT_BASE_DELAY", 1.0), 1.0),
        circuit_failure_threshold=_parse_int(combined.get("CIRCUIT_FAILURE_THRESHOLD", 3), 3),
        circuit_cooldown_seconds=_parse_int(combined.get("CIRCUIT_COOLDOWN_SECONDS", 30), 30),
        trades_file=str(combined.get("TRADES_FILE", "data/trades.csv")).strip(),
        state_file=str(combined.get("STATE_FILE", "data/runtime_state")).strip(),
        log_file=str(combined.get("LOG_FILE", "logs/bot.log")).strip(),
        dashboard_host=str(combined.get("DASHBOARD_HOST", "0.0.0.0")).strip(),
        dashboard_port=_parse_int(combined.get("DASHBOARD_PORT", 8000), 8000),
        frontend_dir=str(combined.get("FRONTEND_DIR", "frontend")).strip(),
        deadman_timeout=_parse_int(combined.get("DEADMAN_TIMEOUT", 30), 30),
    )

    return _settings
  
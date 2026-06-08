# backend/app/core/config_loader.py
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


def _strip_value(value: Any) -> str:
    if value is None:
        return ""

    text = str(value).strip()

    if (
        (text.startswith('"') and text.endswith('"'))
        or (text.startswith("'") and text.endswith("'"))
    ):
        text = text[1:-1].strip()

    if "#" in text:
        text = text.split("#", 1)[0].strip()

    return text


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    text = _strip_value(value).lower()

    if text in {"1", "true", "yes", "on", "y"}:
        return True

    if text in {"0", "false", "no", "off", "n"}:
        return False

    return default


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(_strip_value(value)))
    except (TypeError, ValueError):
        return default


def _parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(_strip_value(value))
    except (TypeError, ValueError):
        return default


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}

    if not _ENV_PATH.exists():
        return env

    for raw_line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()

        if key:
            env[key] = _strip_value(value)

    return env


def _get(combined: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = combined.get(key)

        if value is not None and str(value).strip() != "":
            return value

    return default


@dataclass(frozen=True)
class Settings:
    samco_user_id: str = ""
    samco_password: str = ""
    samco_yob: str = ""
    samco_access_token: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    mode: str = "paper"
    paper_mode_use_broker: bool = True
    use_redis: bool = False

    capital: float = 50000.0
    max_daily_loss: float = 3000.0
    max_trades: int = 10
    order_qty: int = 65

    strategy_type: str = "iron_condor"
    iron_condor_enabled: bool = True

    market_open_time: str = "09:15"
    market_close_time: str = "15:30"
    closed_log_interval_seconds: int = 60
    signal_cooldown_seconds: int = 86400
    signal_rejection_cooldown_seconds: int = 300
    startup_reconcile_timeout_seconds: int = 30
    broker_quote_timeout_seconds: int = 3
    daily_reset_check_interval_seconds: int = 10
    manual_flatten_cooldown_seconds: int = 180
    scheduler_stall_warn_seconds: float = 10.0
    scheduler_stall_hard_seconds: float = 60.0
    reconciliation_interval_seconds: int = 300

    ic_monthly_only: bool = False
    ic_one_per_day: bool = True
    ic_skip_expiry_day_entry: bool = True
    ic_skip_expiry_day_entry_use_next_week: bool = True
    ic_entry_day_start: int = 1
    ic_entry_day_end: int = 5
    ic_entry_window_start: str = "10:00"
    ic_entry_window_end: str = "12:30"
    ic_exit_time: str = "15:00"

    # -- IRON CONDOR SAFETY / PROFITABILITY -----------------
    ic_expected_move_buffer: float = 1.10
    ic_min_safety_buffer_points: float = 50.0
    ic_quote_cache_ttl_seconds: int = 3

    ic_eod_decision_time: str = "14:35"
    ic_eod_min_net_profit: float = 75.0
    ic_eod_max_acceptable_loss: float = 300.0
    ic_skip_one_day_before_expiry_after_time: str = "11:15"

    ic_target_profit_pct: float = 0.35
    ic_stop_loss_multiple: float = 1.60
    ic_extreme_loss_multiple: float = 2.40

    ic_short_distance: int = 200
    ic_wing_width: int = 50
    ic_strike_rounding: int = 50

    ic_skip_gap_pct: float = 0.007
    ic_skip_open_range_pct: float = 0.007

    ic_min_entry_premium: float = 40.0
    ic_min_gross_profit: float = 250.0
    ic_min_gross_target_profit: float = 250.0
    ic_min_net_target_profit: float = 100.0
    ic_charges_buffer_multiplier: float = 1.25
    ic_min_option_premium: float = 0.05
    ic_min_reward_risk: float = 0.35
    ic_min_net_after_cost_buffer: float = 80.0
    ic_min_credit_to_cost_ratio: float = 2.0
    ic_entry_cost_buffer_pct: float = 0.10

    ic_margin_required: float = 40000.0
    ic_max_loss_per_trade: float = 3000.0

    ic_days_to_expiry: int = 30
    ic_decay_rate: float = 0.15
    ic_min_decay_factor: float = 0.70
    ic_short_otm_pct: float = 0.024
    ic_long_otm_pct: float = 0.036
    ic_assumed_iv: float = 0.15
    ic_high_probability_mode: bool = True
    ic_require_live_iv: bool = True
    ic_min_live_iv: float = 0.12
    ic_max_live_iv: float = 0.24
    ic_min_iv_rank: float = 0.50
    ic_force_exit_dte: float = 1.0
    ic_blackout_dates: str = ""

    ic_target_short_delta: float = 0.16  # 16-delta ≈ 80% PoP; better premium vs 10-delta
    ic_min_entry_score: float = 60.0    # composite entry score gate (0-100); below this = no entry
    ic_slippage_per_leg: float = 3.0  # pts deducted per leg for bid-ask slippage
    ic_brokerage_per_order: float = 20.0
    ic_entry_order_count: int = 4
    ic_exit_order_count: int = 4
    ic_platform_charges: float = 100.0
    ic_stt_rate: float = 0.0015
    ic_stt_sell_rate: float = 0.0015
    ic_exchange_txn_rate: float = 0.00035
    ic_sebi_rate: float = 0.000001
    ic_gst_rate: float = 0.18
    ic_stamp_duty_rate: float = 0.00003

    stop_loss_pct: float = 0.45
    t1_pct: float = 0.50
    t2_pct: float = 1.25
    trailing_pct: float = 0.15
    breakeven_at_pct: float = 0.25
    min_entry_premium: float = 50.0
    min_option_volume: int = 500
    otm_distance: int = 1
    max_spread_pct: float = 0.04
    dynamic_spread_max_pct: float = 0.12
    dynamic_spread_vol_multiplier: float = 2.0
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
    min_volume_spike: float = 1.5
    max_iv_percentile: float = 70.0

    nifty_symbol: str = "NIFTY 50"
    nifty_exchange: str = "NSE"

    poll_seconds: int = 1
    gap_threshold: float = 5.0
    trend_filter_enabled: bool = False
    skip_first_candle: bool = True
    no_entry_after: str = "12:30"
    square_off: str = "14:55"

    reconnect_max_attempts: int = 5
    reconnect_base_delay: float = 1.0
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: int = 30
    deadman_timeout: int = 30

    redis_url: str = "redis://localhost:6379"

    trades_file: str = "data/trades.csv"
    state_file: str = "data/runtime_state.json"
    log_file: str = "logs/bot.log"
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000
    frontend_dir: str = "frontend"

    @property
    def is_live(self) -> bool:
        return self.mode.strip().lower() == "live"

    @property
    def is_paper(self) -> bool:
        return not self.is_live

    @property
    def ic_target_profit(self) -> float:
        return self.ic_target_profit_pct

    @property
    def ic_long_distance(self) -> int:
        return self.ic_short_distance + self.ic_wing_width


_settings: Settings | None = None


def reset_settings_cache() -> None:
    global _settings
    _settings = None


def get_settings() -> Settings:
    global _settings

    if _settings is not None:
        return _settings

    env = _load_env()
    combined: dict[str, Any] = {**os.environ, **env}

    _settings = Settings(
        samco_user_id=_strip_value(_get(combined, "SAMCO_USER_ID", default="")),
        samco_password=_strip_value(_get(combined, "SAMCO_PASSWORD", default="")),
        samco_yob=_strip_value(_get(combined, "SAMCO_YOB", default="")),
        samco_access_token=_strip_value(_get(combined, "SAMCO_ACCESS_TOKEN", default="")),

        telegram_bot_token=_strip_value(_get(combined, "TELEGRAM_BOT_TOKEN", default="")),
        telegram_chat_id=_strip_value(_get(combined, "TELEGRAM_CHAT_ID", default="")),

        mode=_strip_value(_get(combined, "MODE", default="paper")).lower(),
        paper_mode_use_broker=_parse_bool(_get(combined, "PAPER_MODE_USE_BROKER", default=True), True),
        use_redis=_parse_bool(_get(combined, "USE_REDIS", default=False), False),

        capital=_parse_float(_get(combined, "CAPITAL", default=50000.0), 50000.0),
        max_daily_loss=_parse_float(_get(combined, "MAX_DAILY_LOSS", default=3000.0), 3000.0),
        max_trades=_parse_int(_get(combined, "MAX_TRADES", default=10), 10),
        order_qty=_parse_int(_get(combined, "ORDER_QTY", default=65), 65),

        strategy_type=_strip_value(_get(combined, "STRATEGY_TYPE", default="iron_condor")).lower(),
        iron_condor_enabled=_parse_bool(_get(combined, "IRON_CONDOR_ENABLED", default=True), True),

        market_open_time=_strip_value(_get(combined, "MARKET_OPEN_TIME", default="09:15")),
        market_close_time=_strip_value(_get(combined, "MARKET_CLOSE_TIME", default="15:30")),
        closed_log_interval_seconds=_parse_int(_get(combined, "CLOSED_LOG_INTERVAL_SECONDS", default=60), 60),
        signal_cooldown_seconds=_parse_int(
            _get(combined, "SIGNAL_COOLDOWN_SECONDS", "SIGNAL_COOLDOWN", default=86400),
            86400,
        ),
        signal_rejection_cooldown_seconds=_parse_int(
            _get(combined, "SIGNAL_REJECTION_COOLDOWN_SECONDS", default=300),
            300,
        ),
        startup_reconcile_timeout_seconds=_parse_int(
            _get(combined, "STARTUP_RECONCILE_TIMEOUT_SECONDS", default=30),
            30,
        ),
        broker_quote_timeout_seconds=_parse_int(_get(combined, "BROKER_QUOTE_TIMEOUT_SECONDS", default=3), 3),
        daily_reset_check_interval_seconds=_parse_int(
            _get(combined, "DAILY_RESET_CHECK_INTERVAL_SECONDS", default=10),
            10,
        ),
        manual_flatten_cooldown_seconds=_parse_int(
            _get(combined, "MANUAL_FLATTEN_COOLDOWN_SECONDS", default=180),
            180,
        ),
        scheduler_stall_warn_seconds=_parse_float(
            _get(combined, "SCHEDULER_STALL_WARN_SECONDS", default=10.0),
            10.0,
        ),
        scheduler_stall_hard_seconds=_parse_float(
            _get(combined, "SCHEDULER_STALL_HARD_SECONDS", default=60.0),
            60.0,
        ),
        reconciliation_interval_seconds=_parse_int(
            _get(combined, "RECONCILIATION_INTERVAL_SECONDS", default=300),
            300,
        ),

        ic_monthly_only=_parse_bool(_get(combined, "IC_MONTHLY_ONLY", default=False), False),
        ic_one_per_day=_parse_bool(_get(combined, "IC_ONE_PER_DAY", default=True), True),
        ic_skip_expiry_day_entry=_parse_bool(
            _get(combined, "IC_SKIP_EXPIRY_DAY_ENTRY", default=True),
            True,
        ),
        ic_skip_expiry_day_entry_use_next_week=_parse_bool(
            _get(combined, "IC_SKIP_EXPIRY_DAY_ENTRY_USE_NEXT_WEEK", default=True),
            True,
        ),
        ic_entry_day_start=_parse_int(_get(combined, "IC_ENTRY_DAY_START", default=1), 1),
        ic_entry_day_end=_parse_int(_get(combined, "IC_ENTRY_DAY_END", default=5), 5),
        ic_entry_window_start=_strip_value(_get(combined, "IC_ENTRY_WINDOW_START", default="10:00")),
        ic_entry_window_end=_strip_value(_get(combined, "IC_ENTRY_WINDOW_END", default="12:30")),
        ic_exit_time=_strip_value(_get(combined, "IC_EXIT_TIME", "SQUARE_OFF", default="15:00")),
        ic_expected_move_buffer=_parse_float(
            _get(combined, "IC_EXPECTED_MOVE_BUFFER", default=1.10),
            1.10,
        ),
        ic_min_safety_buffer_points=_parse_float(
            _get(combined, "IC_MIN_SAFETY_BUFFER_POINTS", default=50.0),
            50.0,
        ),
        ic_quote_cache_ttl_seconds=_parse_int(_get(combined, "IC_QUOTE_CACHE_TTL_SECONDS", default=3), 3),
        ic_eod_decision_time=_strip_value(_get(combined, "IC_EOD_DECISION_TIME", default="14:35")),
        ic_eod_min_net_profit=_parse_float(_get(combined, "IC_EOD_MIN_NET_PROFIT", default=75.0), 75.0),
        ic_eod_max_acceptable_loss=_parse_float(
            _get(combined, "IC_EOD_MAX_ACCEPTABLE_LOSS", default=300.0),
            300.0,
        ),
        ic_skip_one_day_before_expiry_after_time=_strip_value(
            _get(combined, "IC_SKIP_ONE_DAY_BEFORE_EXPIRY_AFTER_TIME", default="11:15")
        ),

        ic_target_profit_pct=_parse_float(
            _get(combined, "IC_TARGET_PROFIT_PCT", "IC_TARGET_PROFIT", default=0.35),
            0.35,
        ),
        ic_stop_loss_multiple=_parse_float(_get(combined, "IC_STOP_LOSS_MULTIPLE", default=1.60), 1.60),
        ic_extreme_loss_multiple=_parse_float(_get(combined, "IC_EXTREME_LOSS_MULTIPLE", default=2.40), 2.40),

        ic_short_distance=_parse_int(_get(combined, "IC_SHORT_DISTANCE", default=200), 200),
        ic_wing_width=_parse_int(_get(combined, "IC_WING_WIDTH", default=50), 50),
        ic_strike_rounding=_parse_int(_get(combined, "IC_STRIKE_ROUNDING", default=50), 50),

        ic_skip_gap_pct=_parse_float(_get(combined, "IC_SKIP_GAP_PCT", default=0.007), 0.007),
        ic_skip_open_range_pct=_parse_float(_get(combined, "IC_SKIP_OPEN_RANGE_PCT", default=0.007), 0.007),

        ic_min_entry_premium=_parse_float(_get(combined, "IC_MIN_ENTRY_PREMIUM", default=40.0), 40.0),
        ic_min_gross_profit=_parse_float(_get(combined, "IC_MIN_GROSS_PROFIT", default=250.0), 250.0),
        ic_min_gross_target_profit=_parse_float(
            _get(combined, "IC_MIN_GROSS_TARGET_PROFIT", default=250.0),
            250.0,
        ),
        ic_min_net_target_profit=_parse_float(
            _get(combined, "IC_MIN_NET_TARGET_PROFIT", default=100.0),
            100.0,
        ),
        ic_charges_buffer_multiplier=_parse_float(
            _get(combined, "IC_CHARGES_BUFFER_MULTIPLIER", default=1.25),
            1.25,
        ),
        ic_min_option_premium=_parse_float(_get(combined, "IC_MIN_OPTION_PREMIUM", default=0.05), 0.05),
        ic_min_reward_risk=_parse_float(_get(combined, "IC_MIN_REWARD_RISK", default=0.35), 0.35),
        ic_min_net_after_cost_buffer=_parse_float(
            _get(combined, "IC_MIN_NET_AFTER_COST_BUFFER", default=80.0),
            80.0,
        ),
        ic_min_credit_to_cost_ratio=_parse_float(
            _get(combined, "IC_MIN_CREDIT_TO_COST_RATIO", default=2.0),
            2.0,
        ),
        ic_entry_cost_buffer_pct=_parse_float(_get(combined, "IC_ENTRY_COST_BUFFER_PCT", default=0.10), 0.10),

        ic_margin_required=_parse_float(_get(combined, "IC_MARGIN_REQUIRED", default=40000.0), 40000.0),
        ic_max_loss_per_trade=_parse_float(_get(combined, "IC_MAX_LOSS_PER_TRADE", default=3000.0), 3000.0),

        ic_days_to_expiry=_parse_int(_get(combined, "IC_DAYS_TO_EXPIRY", default=30), 30),
        ic_decay_rate=_parse_float(_get(combined, "IC_DECAY_RATE", default=0.15), 0.15),
        ic_min_decay_factor=_parse_float(_get(combined, "IC_MIN_DECAY_FACTOR", default=0.70), 0.70),
        ic_short_otm_pct=_parse_float(_get(combined, "IC_SHORT_OTM_PCT", default=0.024), 0.024),
        ic_long_otm_pct=_parse_float(_get(combined, "IC_LONG_OTM_PCT", default=0.036), 0.036),
        ic_assumed_iv=_parse_float(_get(combined, "IC_ASSUMED_IV", default=0.15), 0.15),
        ic_high_probability_mode=_parse_bool(
            _get(combined, "IC_HIGH_PROBABILITY_MODE", default=True),
            True,
        ),
        ic_require_live_iv=_parse_bool(_get(combined, "IC_REQUIRE_LIVE_IV", default=True), True),
        ic_min_live_iv=_parse_float(_get(combined, "IC_MIN_LIVE_IV", default=0.12), 0.12),
        ic_max_live_iv=_parse_float(_get(combined, "IC_MAX_LIVE_IV", default=0.24), 0.24),
        ic_min_iv_rank=_parse_float(_get(combined, "IC_MIN_IV_RANK", default=0.50), 0.50),
        ic_force_exit_dte=_parse_float(_get(combined, "IC_FORCE_EXIT_DTE", default=1.0), 1.0),
        ic_blackout_dates=_strip_value(_get(combined, "IC_BLACKOUT_DATES", default="")),

        ic_target_short_delta=_parse_float(_get(combined, "IC_TARGET_SHORT_DELTA", default=0.16), 0.16),
        ic_min_entry_score=_parse_float(_get(combined, "IC_MIN_ENTRY_SCORE", default=60.0), 60.0),
        ic_slippage_per_leg=_parse_float(_get(combined, "IC_SLIPPAGE_PER_LEG", default=3.0), 3.0),
        ic_brokerage_per_order=_parse_float(_get(combined, "IC_BROKERAGE_PER_ORDER", default=20.0), 20.0),
        ic_entry_order_count=_parse_int(_get(combined, "IC_ENTRY_ORDER_COUNT", default=4), 4),
        ic_exit_order_count=_parse_int(_get(combined, "IC_EXIT_ORDER_COUNT", default=4), 4),
        ic_platform_charges=_parse_float(_get(combined, "IC_PLATFORM_CHARGES", default=100.0), 100.0),
        ic_stt_rate=_parse_float(_get(combined, "IC_STT_RATE", default=0.0015), 0.0015),
        ic_stt_sell_rate=_parse_float(
            _get(combined, "IC_STT_SELL_RATE", "IC_STT_RATE", default=0.0015),
            0.0015,
        ),
        ic_exchange_txn_rate=_parse_float(_get(combined, "IC_EXCHANGE_TXN_RATE", default=0.00035), 0.00035),
        ic_sebi_rate=_parse_float(_get(combined, "IC_SEBI_RATE", default=0.000001), 0.000001),
        ic_gst_rate=_parse_float(_get(combined, "IC_GST_RATE", default=0.18), 0.18),
        ic_stamp_duty_rate=_parse_float(_get(combined, "IC_STAMP_DUTY_RATE", default=0.00003), 0.00003),

        stop_loss_pct=_parse_float(_get(combined, "STOP_LOSS_PCT", default=0.45), 0.45),
        t1_pct=_parse_float(_get(combined, "T1_PCT", default=0.50), 0.50),
        t2_pct=_parse_float(_get(combined, "T2_PCT", default=1.25), 1.25),
        trailing_pct=_parse_float(_get(combined, "TRAILING_PCT", default=0.15), 0.15),
        breakeven_at_pct=_parse_float(_get(combined, "BREAKEVEN_AT_PCT", default=0.25), 0.25),
        min_entry_premium=_parse_float(_get(combined, "MIN_ENTRY_PREMIUM", default=50.0), 50.0),
        min_option_volume=_parse_int(_get(combined, "MIN_OPTION_VOLUME", default=500), 500),
        otm_distance=_parse_int(_get(combined, "OTM_DISTANCE", default=1), 1),
        max_spread_pct=_parse_float(_get(combined, "MAX_SPREAD_PCT", default=0.04), 0.04),
        dynamic_spread_max_pct=_parse_float(_get(combined, "DYNAMIC_SPREAD_MAX_PCT", default=0.12), 0.12),
        dynamic_spread_vol_multiplier=_parse_float(
            _get(combined, "DYNAMIC_SPREAD_VOL_MULTIPLIER", default=2.0),
            2.0,
        ),
        min_dte=_parse_int(_get(combined, "MIN_DTE", default=2), 2),
        max_dte=_parse_int(_get(combined, "MAX_DTE", default=7), 7),

        orb_duration_seconds=_parse_int(_get(combined, "ORB_DURATION_SECONDS", default=900), 900),
        min_orb_range_pct=_parse_float(_get(combined, "MIN_ORB_RANGE_PCT", default=0.0020), 0.0020),
        max_orb_range_pct=_parse_float(_get(combined, "MAX_ORB_RANGE_PCT", default=0.0080), 0.0080),
        orb_atr_multiplier=_parse_float(_get(combined, "ORB_ATR_MULTIPLIER", default=1.0), 1.0),
        breakout_buffer=_parse_float(_get(combined, "BREAKOUT_BUFFER", default=5.0), 5.0),
        max_breakout_extension_pct=_parse_float(
            _get(combined, "MAX_BREAKOUT_EXTENSION_PCT", default=0.05),
            0.05,
        ),
        max_option_spike_pct=_parse_float(_get(combined, "MAX_OPTION_SPIKE_PCT", default=0.20), 0.20),

        max_consecutive_losses=_parse_int(_get(combined, "MAX_CONSECUTIVE_LOSSES", default=3), 3),
        max_drawdown_pct=_parse_float(_get(combined, "MAX_DRAWDOWN_PCT", default=0.20), 0.20),

        min_orb_range=_parse_float(_get(combined, "MIN_ORB_RANGE", default=50.0), 50.0),
        min_volume_spike=_parse_float(_get(combined, "MIN_VOLUME_SPIKE", default=1.5), 1.5),
        max_iv_percentile=_parse_float(_get(combined, "MAX_IV_PERCENTILE", default=70.0), 70.0),

        nifty_symbol=_strip_value(_get(combined, "NIFTY_SYMBOL", default="NIFTY 50")),
        nifty_exchange=_strip_value(_get(combined, "NIFTY_EXCHANGE", default="NSE")),

        poll_seconds=_parse_int(_get(combined, "POLL_SECONDS", default=1), 1),
        gap_threshold=_parse_float(_get(combined, "GAP_THRESHOLD", default=5.0), 5.0),
        trend_filter_enabled=_parse_bool(_get(combined, "TREND_FILTER_ENABLED", default=False), False),
        skip_first_candle=_parse_bool(_get(combined, "SKIP_FIRST_CANDLE", default=True), True),
        no_entry_after=_strip_value(_get(combined, "NO_ENTRY_AFTER", default="12:30")),
        square_off=_strip_value(_get(combined, "SQUARE_OFF", default="14:55")),

        reconnect_max_attempts=_parse_int(_get(combined, "RECONNECT_MAX_ATTEMPTS", default=5), 5),
        reconnect_base_delay=_parse_float(_get(combined, "RECONNECT_BASE_DELAY", default=1.0), 1.0),
        circuit_failure_threshold=_parse_int(_get(combined, "CIRCUIT_FAILURE_THRESHOLD", default=3), 3),
        circuit_cooldown_seconds=_parse_int(_get(combined, "CIRCUIT_COOLDOWN_SECONDS", default=30), 30),
        deadman_timeout=_parse_int(_get(combined, "DEADMAN_TIMEOUT", default=30), 30),

        redis_url=_strip_value(_get(combined, "REDIS_URL", default="redis://localhost:6379")),

        trades_file=_strip_value(_get(combined, "TRADES_FILE", default="data/trades.csv")),
        state_file=_strip_value(_get(combined, "STATE_FILE", default="data/runtime_state.json")),
        log_file=_strip_value(_get(combined, "LOG_FILE", default="logs/bot.log")),
        dashboard_host=_strip_value(_get(combined, "DASHBOARD_HOST", default="0.0.0.0")),
        dashboard_port=_parse_int(_get(combined, "DASHBOARD_PORT", default=8000), 8000),
        frontend_dir=_strip_value(_get(combined, "FRONTEND_DIR", default="frontend")),
    )

    return _settings

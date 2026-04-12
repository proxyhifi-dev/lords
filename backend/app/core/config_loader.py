from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):

    # -------------------------
    # APP
    # -------------------------
    app_name: str = "Lords Bot"

    # -------------------------
    # SAMCO LOGIN
    # -------------------------
    samco_user_id: str
    samco_password: str
    samco_yob: str
    samco_access_token: str = ""

    # -------------------------
    # MARKET
    # -------------------------
    nifty_symbol: str = "NIFTY 50"
    nifty_exchange: str = "NSE"
    poll_seconds: int = 1

    # -------------------------
    # TRADING
    # -------------------------
    order_qty: int = 65
    max_daily_loss: float = 5000.0
    max_trades: int = 3
    otm_distance: int = 1
    min_option_volume: int = 500
    min_entry_premium: float = 50.0      # skip options below ₹50 premium

    # ── PROVEN OPTIMAL by 12-param grid search ────────
    # SL 25% | T1 40% on 50% qty | T2 100% on 50% qty
    # Sortino=6.41 | Sharpe=1.71 | PF=1.84x | WR=47.8%
    stop_loss_pct:  float = 0.25         # 25% SL on full position
    t1_pct:         float = 0.40         # Stage 1: book 50% qty at 40% profit
    t2_pct:         float = 1.00         # Stage 2: remaining 50% at 100% profit
    trailing_pct:   float = 0.25         # 25% trail — only after T1 booked

    # Trailing SL activation — only trail after this % gain
    trailing_activation_pct: float = 0.0  # activates as soon as T1 is hit

    # -------------------------
    # ORB
    # -------------------------
    orb_start: str = "09:15"
    orb_end: str = "09:30"
    orb_duration_seconds: int = 900      # 15 min = 900s

    # ATR-based ORB quality filter
    # Skip if ORB range < orb_atr_multiplier × ATR (choppy day = no energy)
    min_orb_range: float = 5.0           # absolute floor
    orb_atr_multiplier: float = 1.0      # proven optimal: 1.0x ATR

    breakout_buffer: float = 2.0         # pts above/below ORB to confirm breakout
    signal_cooldown: int = 60            # seconds between signals
    gap_threshold: float = 5.0

    # ── TREND FILTER ──────────────────────────────────
    # Grid search PROVED: False wins on bear/crash markets
    # Set True only in strong bull markets
    trend_filter_enabled: bool = False

    # -------------------------
    # SQUARE OFF
    # -------------------------
    no_entry_after: str = "13:30"        # no new entries after 13:30
    square_off: str = "15:10"            # force exit at 15:10

    # -------------------------
    # RECONNECT
    # -------------------------
    reconnect_max_attempts: int = 5
    reconnect_base_delay: int = 1

    # -------------------------
    # CIRCUIT BREAKER
    # -------------------------
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: int = 30

    # -------------------------
    # STORAGE
    # -------------------------
    trades_file: str = "backend/storage/trades.json"
    state_file: str = "backend/storage/runtime_state.json"

    # -------------------------
    # LOG
    # -------------------------
    log_file: str = "backend/logs/bot.log"

    # -------------------------
    # DASHBOARD
    # -------------------------
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000

    # -------------------------
    # FRONTEND
    # -------------------------
    frontend_dir: str = "frontend"

    class Config:
        env_file = ".env"
        extra = "allow"


@lru_cache
def get_settings():
    return Settings()
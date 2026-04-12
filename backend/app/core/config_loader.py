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
    order_qty: int = 50
    max_daily_loss: float = 5000.0
    max_trades: int = 3
    otm_distance: int = 1

    # ── ULTRA PRO MAX: SL + 2-stage exit ──────────────
    stop_loss_pct: float = 0.25   # 25% SL on full position

    # Stage 1: book 50% qty at 40% profit
    t1_pct: float = 0.40

    # Stage 2: remaining 50% target at 100% profit
    t2_pct: float = 1.00

    # Trailing SL % — only activates AFTER T1 is booked
    trailing_pct: float = 0.25

    # Min premium to enter — skip cheap illiquid options
    min_entry_premium: float = 50.0

    # Min option volume filter
    min_option_volume: int = 500

    # -------------------------
    # ORB
    # -------------------------
    orb_start: str = "09:15"
    orb_end: str = "09:30"
    orb_duration_seconds: int = 900

    # ULTRA PRO MAX: ATR-based ORB quality filter
    # Skip if ORB range < orb_atr_multiplier * ATR (choppy day)
    min_orb_range: float = 5.0            # absolute floor
    orb_atr_multiplier: float = 1.5       # ORB must be > 1.5x ATR

    breakout_buffer: float = 2.0
    signal_cooldown: int = 60             # 60s cooldown between signals
    gap_threshold: float = 5.0

    # -------------------------
    # SQUARE OFF
    # -------------------------
    no_entry_after: str = "13:30"         # ULTRA PRO MAX: no new entries after 13:30
    square_off: str = "15:10"

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

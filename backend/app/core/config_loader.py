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
    samco_access_token: str = ""          # optional — TOTP / access token

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
    stop_loss_pct: float = 0.30           # 30 % SL
    target_pct: float = 0.60             # 60 % target
    trailing_pct: float = 0.20           # 20 % trailing SL
    min_option_volume: int = 0            # set > 0 to filter illiquid strikes

    # -------------------------
    # ORB
    # -------------------------
    orb_start: str = "09:15"
    orb_end: str = "09:30"
    orb_duration_seconds: int = 900       # 15 min = 900 s
    min_orb_range: float = 5.0
    breakout_buffer: float = 2.0
    signal_cooldown: int = 10
    gap_threshold: float = 5.0

    # -------------------------
    # SQUARE OFF
    # -------------------------
    no_entry_after: str = "15:10"
    square_off: str = "15:15"

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

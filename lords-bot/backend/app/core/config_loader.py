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
    samco_access_token: str

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
    max_daily_loss: int = 3000
    max_trades: int = 2
    stop_loss_pct: float = 0.2
    target_pct: float = 0.4

    # -------------------------
    # ORB TIMES
    # -------------------------
    orb_start: str = "09:15"
    orb_end: str = "09:30"
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

    # -------------------------
    # ENV CONFIG
    # -------------------------
    class Config:
        env_file = ".env"
        extra = "allow"


@lru_cache
def get_settings():
    return Settings()
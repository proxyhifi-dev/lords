from __future__ import annotations
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):

    # App
    app_name: str = "Lords Bot"
    mode: str = "paper"                   # paper | live

    # SAMCO
    samco_user_id: str = ""
    samco_password: str = ""
    samco_yob: str = ""
    samco_access_token: str = ""

    # Capital / Risk
    capital: float = 40_000.0
    max_daily_loss: float = 3_000.0
    max_trades: int = 2

    # Order
    order_qty: int = 65

    # Exit levels
    stop_loss_pct: float = 0.25
    t1_pct: float = 0.40
    t2_pct: float = 1.00
    trailing_pct: float = 0.25

    # Entry filters
    min_entry_premium: float = 30.0
    min_option_volume: int = 500
    otm_distance: int = 1

    # ORB
    orb_duration_seconds: int = 900
    min_orb_range: float = 5.0
    orb_atr_multiplier: float = 1.0
    breakout_buffer: float = 2.0
    signal_cooldown: int = 60
    trend_filter_enabled: bool = False
    gap_threshold: float = 5.0

    # Timing
    no_entry_after: str = "13:30"
    square_off: str = "15:10"
    poll_seconds: int = 2

    # Symbols
    nifty_symbol: str = "NIFTY 50"
    nifty_exchange: str = "NSE"

    # Reconnect
    reconnect_max_attempts: int = 5
    reconnect_base_delay: int = 1

    # Circuit breaker
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: int = 30

    # Storage
    trades_file: str = "data/trades.csv"
    state_file: str = "data/runtime_state.json"
    log_file: str = "logs/bot.log"

    # Server
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000
    frontend_dir: str = "frontend"

    class Config:
        env_file = ".env"
        extra = "allow"

    @property
    def is_live(self) -> bool:
        return self.mode.lower() == "live"


@lru_cache
def get_settings() -> Settings:
    return Settings()

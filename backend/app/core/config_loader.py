from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings

_ENV_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".env"

class Settings(BaseSettings):
    app_name: str = "Lords Bot"
    mode: str = "paper"
    samco_user_id: str = ""
    samco_password: str = ""
    samco_yob: str = ""
    samco_access_token: str = ""
    capital: float = 50_000.0
    max_daily_loss: float = 5_000.0
    max_trades: int = 3
    order_qty: int = 65
    stop_loss_pct: float = 0.30
    t1_pct: float = 0.40
    t2_pct: float = 1.00
    trailing_pct: float = 0.20
    min_entry_premium: float = 30.0
    min_option_volume: int = 0
    otm_distance: int = 1
    orb_duration_seconds: int = 900
    min_orb_range: float = 50.0
    orb_max_range: float = 150.0
    orb_atr_multiplier: float = 1.0
    breakout_buffer: float = 5.0
    signal_cooldown: int = 120
    trend_filter_enabled: bool = True
    skip_first_candle: bool = True
    gap_threshold: float = 5.0
    # Slippage model (v5.0)
    slippage_entry: float = 2.0     # ₹ extra above ask on entry market orders
    slippage_exit: float = 1.5      # ₹ extra below bid on exit market orders
    slippage_sl_gap: float = 5.0    # ₹ extra on SL gap fills (fast market)
    no_entry_after: str = "13:30"
    square_off: str = "15:10"
    poll_seconds: int = 1
    nifty_symbol: str = "NIFTY 50"
    nifty_exchange: str = "NSE"
    reconnect_max_attempts: int = 5
    reconnect_base_delay: int = 1
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: int = 30
    trades_file: str = "data/trades.csv"
    state_file: str = "data/runtime_state.json"
    log_file: str = "logs/bot.log"
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000
    frontend_dir: str = "frontend"
    class Config:
        env_file = str(_ENV_FILE)
        env_file_encoding = "utf-8"
        extra = "allow"
    @property
    def is_live(self) -> bool:
        return self.mode.lower() == "live"

@lru_cache
def get_settings() -> Settings:
    return Settings()

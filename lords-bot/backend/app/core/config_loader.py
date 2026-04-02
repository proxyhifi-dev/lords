from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    samco_user_id: str = Field(alias="SAMCO_USER_ID")
    samco_password: str = Field(alias="SAMCO_PASSWORD")
    samco_yob: str = Field(alias="SAMCO_YOB")

    nifty_symbol: str = Field(default="NIFTY 50", alias="NIFTY_SYMBOL")
    nifty_exchange: str = Field(default="NSE", alias="NIFTY_EXCHANGE")

    poll_seconds: float = Field(default=1.0, alias="POLL_SECONDS")
    order_qty: int = Field(default=50, alias="ORDER_QTY")
    risk_percent: float = Field(default=1.0, alias="RISK_PERCENT")

    max_trades: int = Field(default=2, alias="MAX_TRADES")
    max_daily_loss: float = Field(default=3000.0, alias="MAX_DAILY_LOSS")
    stop_loss_pct: float = Field(default=0.2, alias="STOP_LOSS_PCT")
    target_pct: float = Field(default=0.4, alias="TARGET_PCT")

    orb_start: str = Field(default="09:15", alias="ORB_START")
    orb_end: str = Field(default="09:30", alias="ORB_END")
    square_off: str = Field(default="15:15", alias="SQUARE_OFF")

    reconnect_max_attempts: int = Field(default=5, alias="RECONNECT_MAX_ATTEMPTS")
    reconnect_base_delay: int = Field(default=1, alias="RECONNECT_BASE_DELAY")
    circuit_failure_threshold: int = Field(default=3, alias="CIRCUIT_FAILURE_THRESHOLD")
    circuit_cooldown_seconds: int = Field(default=30, alias="CIRCUIT_COOLDOWN_SECONDS")

    log_file: str = Field(default="backend/logs/bot.log", alias="LOG_FILE")
    trades_file: str = Field(default="backend/storage/trades.json", alias="TRADES_FILE")
    state_file: str = Field(default="backend/storage/runtime_state.json", alias="STATE_FILE")

    dashboard_host: str = Field(default="0.0.0.0", alias="DASHBOARD_HOST")
    dashboard_port: int = Field(default=8000, alias="DASHBOARD_PORT")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]
    Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.trades_file).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.state_file).parent.mkdir(parents=True, exist_ok=True)
    return settings

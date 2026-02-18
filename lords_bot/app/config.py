from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Central app settings. Defaults keep the bot bootable in local/PAPER mode."""

    fyers_app_id: str = Field(default="", alias="FYERS_APP_ID")
    fyers_secret: str = Field(default="", alias="FYERS_SECRET")
    fyers_redirect_uri: AnyHttpUrl = Field(default="http://127.0.0.1:8080", alias="FYERS_REDIRECT_URI")
    fyers_pin: str = Field(default="", alias="FYERS_PIN")

    fyers_auth_url: AnyHttpUrl = Field(default="https://api-t1.fyers.in/api/v3", alias="FYERS_AUTH_URL")

    # Explicit split between trade vs market-data REST hosts.
    fyers_base_url: AnyHttpUrl = Field(default="https://api-t1.fyers.in/api/v3", alias="FYERS_BASE_URL")
    fyers_data_url: AnyHttpUrl = Field(default="https://api.fyers.in/data-rest/v3", alias="FYERS_DATA_URL")

    # Websocket defaults used by websocket_service and docs.
    fyers_data_ws_url: str = Field(default="wss://api.fyers.in/socket/v2/data/", alias="FYERS_DATA_WS_URL")
    fyers_order_ws_url: str = Field(default="wss://api.fyers.in/socket/v2/order/", alias="FYERS_ORDER_WS_URL")
    fyers_position_ws_url: str = Field(default="wss://api.fyers.in/socket/v2/position/", alias="FYERS_POSITION_WS_URL")
    fyers_trade_ws_url: str = Field(default="wss://api.fyers.in/socket/v2/trade/", alias="FYERS_TRADE_WS_URL")

    fyers_ws_ssl_verify: bool = Field(default=True, alias="FYERS_WS_SSL_VERIFY")
    fyers_ws_stop_on_ssl_error: bool = Field(default=False, alias="FYERS_WS_STOP_ON_SSL_ERROR")
    fyers_ws_max_retry_delay: float = Field(default=60.0, alias="FYERS_WS_MAX_RETRY_DELAY")

    trading_mode: str = Field(default="PAPER", alias="TRADING_MODE")
    initial_capital: float = Field(default=100000.0, alias="INITIAL_CAPITAL")
    stop_loss_pct: float = Field(default=0.15, alias="STOP_LOSS_PCT")
    target_pct: float = Field(default=0.30, alias="TARGET_PCT")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_file: str = Field(default="logs/lords_bot.log", alias="LOG_FILE")

    # API reliability controls.
    fyers_max_retries: int = Field(default=3, alias="FYERS_MAX_RETRIES")
    fyers_retry_backoff_seconds: float = Field(default=0.5, alias="FYERS_RETRY_BACKOFF_SECONDS")
    fyers_max_backoff_seconds: float = Field(default=8.0, alias="FYERS_MAX_BACKOFF_SECONDS")
    api_failure_threshold: int = Field(default=5, alias="API_FAILURE_THRESHOLD")
    api_failure_window_seconds: int = Field(default=60, alias="API_FAILURE_WINDOW_SECONDS")
    api_pause_seconds: int = Field(default=120, alias="API_PAUSE_SECONDS")

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

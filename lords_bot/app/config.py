from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """
    Centralized application settings.
    """

    # ==========================================================
    # 🔐 API CREDENTIALS
    # ==========================================================
    fyers_app_id: str = Field(default="", alias="FYERS_APP_ID")
    fyers_secret: str = Field(default="", alias="FYERS_SECRET")
    fyers_redirect_uri: AnyHttpUrl = Field(
        default="http://127.0.0.1:8080",
        alias="FYERS_REDIRECT_URI",
    )

    # ==========================================================
    # 🔑 AUTH / CALLBACK
    # ==========================================================
    auth_callback_port: int = Field(
        default=8080,
        alias="AUTH_CALLBACK_PORT",
    )

    # 🔥 THIS WAS MISSING
    # Unified base URL for all Fyers endpoints (T1 or production)
    fyers_env: str = Field(default="t1", alias="FYERS_ENV")  # 't1' or 'prod'


    @property
    def fyers_base_url(self) -> str:
        # Auth and Orders MUST always use api-t1
        return "https://api-t1.fyers.in/api/v3"

    @property
    def fyers_auth_url(self) -> str:
        return self.fyers_base_url

    @property
    def fyers_data_url(self) -> str:
        # Data REST calls
        return "https://api-t1.fyers.in/data-rest/v3"

    @property
    def fyers_data_ws_url(self) -> str:
        # Market Data WebSocket MUST always use api (No -t1)
        return "wss://api.fyers.in/socket/v2/data/"

    @property
    def fyers_order_ws_url(self) -> str:
        return "wss://api.fyers.in/socket/v2/order/"

    @property
    def fyers_position_ws_url(self) -> str:
        return "wss://api.fyers.in/socket/v2/position/"

    @property
    def fyers_trade_ws_url(self) -> str:
        return "wss://api.fyers.in/socket/v2/trade/"


    # ==========================================================
    # 📊 TRADING SETTINGS
    # ==========================================================
    trading_mode: str = Field(default="PAPER", alias="TRADING_MODE")
    initial_capital: float = Field(default=100000.0, alias="INITIAL_CAPITAL")

    # ==========================================================
    # 🔁 RETRY / CIRCUIT BREAKER
    # ==========================================================
    fyers_max_retries: int = Field(default=3, alias="FYERS_MAX_RETRIES")
    fyers_retry_backoff_seconds: float = Field(
        default=0.5,
        alias="FYERS_RETRY_BACKOFF_SECONDS",
    )
    fyers_max_backoff_seconds: float = Field(
        default=8.0,
        alias="FYERS_MAX_BACKOFF_SECONDS",
    )

    api_failure_threshold: int = Field(default=5, alias="API_FAILURE_THRESHOLD")
    api_failure_window_seconds: int = Field(
        default=60,
        alias="API_FAILURE_WINDOW_SECONDS",
    )
    api_pause_seconds: int = Field(default=120, alias="API_PAUSE_SECONDS")

    # ==========================================================
    # 📝 LOGGING
    # ==========================================================
    log_file: str = Field(
        default=str(BASE_DIR / "logs" / "lords_bot.log"),
        alias="LOG_FILE",
    )

    log_level: str = Field(
        default="INFO",
        alias="LOG_LEVEL",
    )

    # ==========================================================
    # ⚙ Pydantic Config
    # ==========================================================
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ------------------------------------------------------------
# Project Root Resolver (lords_bot folder)
# ------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """
    Application settings loaded from .env file.

    Automatically resolves .env from:
    lords_bot/.env

    Supports:
    • Trading API (api-t1)
    • Data API (data-rest)
    • PAPER / LIVE mode
    """

    # ------------------------------------------------------------
    # FYERS AUTH
    # ------------------------------------------------------------

    fyers_app_id: str = Field(..., alias="FYERS_APP_ID")
    fyers_secret: str = Field(..., alias="FYERS_SECRET")
    fyers_redirect_uri: AnyHttpUrl = Field(..., alias="FYERS_REDIRECT_URI")
    fyers_pin: str = Field(..., alias="FYERS_PIN")

    # ------------------------------------------------------------
    # FYERS API URLS (IMPORTANT FOR V3)
    # ------------------------------------------------------------

    # Trading APIs (orders, profile, funds, positions)
    fyers_trading_url: AnyHttpUrl = Field(
        default="https://api-t1.fyers.in/api/v3",
        alias="FYERS_TRADING_URL",
    )

    # Market Data APIs (history, quotes, option chain)
    fyers_data_url: AnyHttpUrl = Field(
        default="https://api.fyers.in/data-rest/v3",
        alias="FYERS_DATA_URL",
    )

    # ------------------------------------------------------------
    # Trading Mode
    # ------------------------------------------------------------

    trading_mode: str = Field(default="PAPER", alias="TRADING_MODE")

    # ------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_file: str = Field(default="logs/lords_bot.log", alias="LOG_FILE")

    # ------------------------------------------------------------
    # Pydantic Settings Config
    # ------------------------------------------------------------

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",  # Always loads correct .env
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )


# ------------------------------------------------------------
# Cached Settings Loader
# ------------------------------------------------------------

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

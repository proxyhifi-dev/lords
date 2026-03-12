from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    samco_base_url: str = Field(default="https://tradeapi.samco.in")
    samco_user_id: str = Field(default="")
    samco_password: str = Field(default="")
    samco_yob: str = Field(default="")

    nifty_symbol: str = Field(default="NIFTY")
    option_exchange: str = Field(default="NFO")
    option_type: str = Field(default="CE")
    option_expiry: str = Field(default="2026-12-31")

    poll_interval_seconds: int = Field(default=5)
    request_timeout_seconds: float = Field(default=10.0)
    max_retries: int = Field(default=3)
    retry_backoff_seconds: float = Field(default=0.75)

    log_level: str = Field(default="INFO")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)


settings = Settings()

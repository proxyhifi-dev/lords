from functools import lru_cache

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    fyers_app_id: str = Field(..., alias="FYERS_APP_ID")
    fyers_secret: str = Field(..., alias="FYERS_SECRET")
    fyers_redirect_uri: AnyHttpUrl = Field(..., alias="FYERS_REDIRECT_URI")
    fyers_pin: str = Field(..., alias="FYERS_PIN")
    fyers_base_url: AnyHttpUrl = Field(
        default="https://api-t1.fyers.in/api/v3",
        alias="FYERS_BASE_URL",
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_file: str = Field(default="logs/lords_bot.log", alias="LOG_FILE")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

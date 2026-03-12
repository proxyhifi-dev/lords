from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    app_name: str = 'Lords Bot'
    samco_base_url: str = 'https://api.stocknote.com'
    samco_user_id: str = ''
    samco_password: str = ''
    samco_yob: str = ''
    samco_api_key: str = ''
    samco_app_secret: str = ''
    samco_access_token: str = ''
    default_symbol: str = 'NIFTY'
    default_expiry: str = '2026-12-31'
    cache_ttl_seconds: int = 3
    scheduler_interval_seconds: int = 3
    trade_capital: float = 100000.0
    data_cache_file: str = '../data/cache.json'
    trades_file: str = '../data/trades.json'
    frontend_dir: str = '../frontend'


class RuntimeState(BaseModel):
    symbol: str = Field(default='NIFTY')
    expiry: str = Field(default='2026-12-31')


settings = Settings()
runtime_state = RuntimeState(symbol=settings.default_symbol, expiry=settings.default_expiry)

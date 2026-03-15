from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT_DIR / '.env'), env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'lords-bot'
    samco_base_url: str = 'https://api.stocknote.com'
    samco_api_key: str = ''
    samco_access_token: str = ''
    samco_session_token: str = ''
    samco_user_id: str = ''
    samco_password: str = ''
    samco_yob: str = ''

    symbol: str = 'NIFTY'
    expiry: str = '2026-03-26'

    trading_mode: str = 'PAPER'
    enable_real_trading: bool = False

    scheduler_interval: int = 60
    option_chain_ttl: int = 30
    analysis_ttl: int = 5
    signals_ttl: int = 5
    profile_ttl: int = 60
    funds_ttl: int = 60
    request_timeout: int = 10
    max_api_retries: int = 3
    base_retry_delay_seconds: float = 1.0

    max_trades_per_day: int = 5
    max_daily_loss: float = 5000

    frontend_dir: str = str(ROOT_DIR / 'frontend')
    cache_snapshot_file: str = str(ROOT_DIR / 'backend' / 'data' / 'cache_snapshot.json')
    trade_log_file: str = str(ROOT_DIR / 'backend' / 'data' / 'trade_log.jsonl')


settings = Settings()

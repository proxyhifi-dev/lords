from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT_DIR / '.env'), env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'lords-bot'
    environment: str = 'dev'

    # Samco auth
    samco_user_id: str = Field(default='', alias='SAMCO_USER_ID')
    samco_password: str = Field(default='', alias='SAMCO_PASSWORD')
    samco_yob: str = Field(default='', alias='SAMCO_YOB')
    samco_session_token: str = Field(default='', alias='SAMCO_SESSION_TOKEN')

    # Trading
    index_symbol: str = 'Nifty 50'
    option_root_symbol: str = 'Nifty'
    exchange: str = 'NFO'
    lot_size: int = 50

    # Reliability
    min_api_interval_seconds: float = 2.0
    request_timeout_seconds: float = 15.0
    max_api_retries: int = 4
    base_retry_delay_seconds: float = 0.5
    market_tick_interval_seconds: float = 3.0

    # Risk
    max_daily_loss: float = 3000.0
    max_trades_per_day: int = 6
    max_position_size: int = 500

    # Storage/logging
    logs_dir: str = str(ROOT_DIR / 'logs')
    log_file: str = str(ROOT_DIR / 'logs' / 'bot.jsonl')
    state_file: str = str(ROOT_DIR / 'backend' / 'runtime_state.json')


settings = Settings()

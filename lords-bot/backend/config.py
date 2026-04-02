from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT_DIR / '.env'), env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'lords-bot'
    environment: str = 'dev'
    api_key: str = ''

    # Samco credentials
    samco_base_url: str = 'https://api.stocknote.com'
    samco_api_key: str = Field(default='', validation_alias=AliasChoices('SAMCO_API_KEY', 'API_KEY'))
    samco_api_secret: str = Field(default='', validation_alias=AliasChoices('SAMCO_API_SECRET', 'API_SECRET'))
    samco_institution_id: str = Field(default='', validation_alias=AliasChoices('SAMCO_INSTITUTION_ID', 'INSTITUTION_ID'))
    samco_access_token: str = Field(default='', validation_alias=AliasChoices('SAMCO_ACCESS_TOKEN', 'ACCESS_TOKEN'))
    samco_session_token: str = Field(default='', validation_alias=AliasChoices('SAMCO_SESSION_TOKEN', 'SESSION_TOKEN'))
    samco_user_id: str = Field(default='', validation_alias=AliasChoices('SAMCO_USER_ID', 'USER_ID'))
    samco_password: str = Field(default='', validation_alias=AliasChoices('SAMCO_PASSWORD', 'PASSWORD'))
    samco_yob: str = Field(default='', validation_alias=AliasChoices('SAMCO_YOB', 'YOB'))

    # Market + strategy
    symbol: str = 'NIFTY'
    index_symbol: str = 'NIFTY 50'
    expiry: str = '2026-04-09'

    # Runtime
    trading_mode: str = 'PAPER'
    enable_real_trading: bool = False
    scheduler_interval: int = 1
    market_tick_interval_seconds: float = 1.0

    # API safety
    request_timeout: int = 10
    max_api_retries: int = 4
    base_retry_delay_seconds: float = 0.5
    max_api_calls_per_second: float = 3.0
    option_chain_ttl: int = 10
    spot_ttl: int = 1

    # Risk
    risk_per_trade_pct: float = 1.0
    capital_allocation_pct: float = 20.0
    max_trades_per_day: int = 6
    max_daily_loss: float = 3000.0
    max_drawdown: float = 7000.0
    max_position_size: int = 500
    max_consecutive_losses: int = 3
    default_lot_size: int = 50
    paper_capital: float = 100000.0

    # Storage
    frontend_dir: str = str(ROOT_DIR / 'frontend')
    logs_dir: str = str(ROOT_DIR / 'logs')
    trading_log_file: str = str(ROOT_DIR / 'logs' / 'trading.log')
    state_file: str = str(ROOT_DIR / 'backend' / 'data' / 'state.json')
    trade_log_file: str = str(ROOT_DIR / 'backend' / 'data' / 'trade_log.jsonl')


settings = Settings()

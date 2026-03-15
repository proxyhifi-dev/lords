from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT_DIR / '.env'), env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'lords-bot'
    environment: str = 'dev'

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
    api_key: str = ''

    scheduler_interval: int = 20
    option_chain_ttl: int = 30
    historical_ttl: int = 300
    spot_ttl: int = 3
    funds_ttl: int = 20
    request_timeout: int = 10
    max_api_retries: int = 3
    base_retry_delay_seconds: float = 1.0

    risk_per_trade_pct: float = 2.0
    max_trades_per_day: int = 3
    max_daily_loss: float = 2000.0
    max_position_size: int = 500
    max_consecutive_losses: int = 3
    default_lot_size: int = 50
    paper_capital: float = 100000.0

    frontend_dir: str = str(ROOT_DIR / 'frontend')
    logs_dir: str = str(ROOT_DIR / 'logs')
    trading_log_file: str = str(ROOT_DIR / 'logs' / 'trading.log')
    state_file: str = str(ROOT_DIR / 'backend' / 'data' / 'state.json')
    trade_log_file: str = str(ROOT_DIR / 'backend' / 'data' / 'trade_log.jsonl')


settings = Settings()

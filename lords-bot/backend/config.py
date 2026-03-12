from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class YamlConfig(BaseModel):
    scheduler_interval: int = 3
    pcr_bullish: float = 1.2
    pcr_bearish: float = 0.8
    max_risk_per_trade: float = 0.02
    atm_range: int = 10
    api_timeout: int = 5
    lot_size: int = 50
    cache_expiry_seconds: int = 10


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT_DIR / '.env'), extra='ignore')

    app_name: str = 'Lords Bot'
    samco_base_url: str = 'https://api.stocknote.com'
    samco_api_key: str = ''
    samco_access_token: str = ''
    samco_username: str = ''
    samco_password: str = ''
    trading_mode: Literal['PAPER', 'REAL'] = 'PAPER'
    enable_real_trading: bool = False

    default_symbol: str = 'NIFTY'
    default_expiry: str = ''

    config_file: str = str(ROOT_DIR / 'config.yaml')
    db_file: str = str(ROOT_DIR / 'data' / 'lords.db')
    data_cache_file: str = str(ROOT_DIR / 'data' / 'cache.json')
    trades_file: str = str(ROOT_DIR / 'data' / 'trades.json')
    frontend_dir: str = str(ROOT_DIR / 'frontend')
    log_file: str = str(ROOT_DIR / 'logs' / 'lords_bot.log')
    trade_capital: float = 100000.0


class RuntimeState(BaseModel):
    symbol: str = Field(default='NIFTY')
    expiry: str = Field(default='')
    trading_mode: Literal['PAPER', 'REAL'] = 'PAPER'


settings = Settings()


def _to_number(v: str):
    if '.' in v:
        return float(v)
    return int(v)


def _load_yaml() -> YamlConfig:
    path = Path(settings.config_file)
    if not path.exists():
        default = YamlConfig()
        lines = [f'{k}: {v}' for k, v in default.model_dump().items()]
        path.write_text('\n'.join(lines) + '\n')
        return default

    parsed: dict = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or ':' not in line:
            continue
        key, val = [x.strip() for x in line.split(':', 1)]
        try:
            parsed[key] = _to_number(val)
        except ValueError:
            parsed[key] = val
    return YamlConfig(**parsed)


yaml_config = _load_yaml()
runtime_state = RuntimeState(symbol=settings.default_symbol, expiry=settings.default_expiry, trading_mode=settings.trading_mode)

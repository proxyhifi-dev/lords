import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    samco_user_id: str = os.getenv("SAMCO_USER_ID", "")
    samco_password: str = os.getenv("SAMCO_PASSWORD", "")
    samco_yob: str = os.getenv("SAMCO_YOB", "")

    nifty_symbol: str = os.getenv("NIFTY_SYMBOL", "NIFTY 50")
    nifty_exchange: str = os.getenv("NIFTY_EXCHANGE", "NSE")

    poll_seconds: float = float(os.getenv("POLL_SECONDS", "1"))
    quantity: int = int(os.getenv("ORDER_QTY", "50"))

    max_trades_per_day: int = int(os.getenv("MAX_TRADES_PER_DAY", "2"))
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "0.20"))
    target_pct: float = float(os.getenv("TARGET_PCT", "0.40"))
    max_daily_loss: float = float(os.getenv("MAX_DAILY_LOSS", "3000"))

    option_expiry: str = os.getenv("OPTION_EXPIRY", "24APR")

    orb_start: str = os.getenv("ORB_START", "09:15")
    orb_end: str = os.getenv("ORB_END", "09:30")
    square_off: str = os.getenv("SQUARE_OFF", "15:15")

    dashboard_host: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    dashboard_port: int = int(os.getenv("DASHBOARD_PORT", "8000"))

    log_file: str = os.getenv("LOG_FILE", "backend/logs/bot.log")
    trades_file: str = os.getenv("TRADES_FILE", "backend/storage/trades.json")


settings = Settings()

Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
Path(settings.trades_file).parent.mkdir(parents=True, exist_ok=True)

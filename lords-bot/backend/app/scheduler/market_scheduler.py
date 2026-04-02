from apscheduler.schedulers.background import BackgroundScheduler

from backend.app.engine.trading_engine import TradingEngine
from backend.app.utils.logger import get_logger
from backend.config import settings


class MarketScheduler:
    def __init__(self, trading_engine: TradingEngine) -> None:
        self.logger = get_logger("market_scheduler")
        self.trading_engine = trading_engine
        self.scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

    def schedule(self) -> None:
        end_hour, end_min = [int(x) for x in settings.orb_end.split(":")]
        so_hour, so_min = [int(x) for x in settings.square_off.split(":")]

        self.scheduler.add_job(self._freeze_orb, "cron", hour=end_hour, minute=end_min)
        self.scheduler.add_job(self.trading_engine.square_off, "cron", hour=so_hour, minute=so_min)
        self.scheduler.start()
        self.logger.info("Market scheduler started")

    def _freeze_orb(self) -> None:
        self.trading_engine.strategy.state.is_frozen = True
        self.logger.info("ORB frozen")

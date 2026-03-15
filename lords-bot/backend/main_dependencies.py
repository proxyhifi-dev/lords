from __future__ import annotations

from core.cache import TTLCache
from engine.backtester import Backtester
from engine.scheduler import scheduler
from services.market_data_service import MarketDataService
from services.option_chain_service import OptionChainService
from services.performance_service import PerformanceService
from services.trade_logger import TradeLogger

cache = TTLCache()
market_data_service = MarketDataService(cache)
option_chain_service = OptionChainService(cache)
trade_logger = TradeLogger()
performance_service = PerformanceService()
backtester = Backtester()
__all__ = [
    'scheduler',
    'market_data_service',
    'option_chain_service',
    'trade_logger',
    'performance_service',
    'backtester',
]

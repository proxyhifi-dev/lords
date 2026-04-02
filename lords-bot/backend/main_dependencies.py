from __future__ import annotations

from engine.backtester import Backtester
from engine.scheduler import scheduler
from services.performance_service import PerformanceService

market_data_service = scheduler.market_data_service
option_chain_service = scheduler.option_chain_service
trade_logger = scheduler.trade_logger
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

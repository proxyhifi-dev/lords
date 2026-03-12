from __future__ import annotations

from datetime import datetime


class PaperTradingEngine:
    def __init__(self) -> None:
        self.trades: list[dict] = []

    def place_order(self, side: str, symbol: str) -> dict:
        trade = {'mode': 'PAPER', 'side': side, 'symbol': symbol, 'timestamp': datetime.utcnow().isoformat()}
        self.trades.append(trade)
        return trade


paper_trading_engine = PaperTradingEngine()

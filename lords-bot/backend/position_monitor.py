from __future__ import annotations

import pandas as pd

from paper_trading_engine import paper_trading_engine


class PositionMonitor:
    def update(self, df: pd.DataFrame) -> None:
        if df.empty:
            return
        for trade in [t for t in paper_trading_engine.trades if t.status == 'OPEN']:
            live = paper_trading_engine._current_price(df, trade.position_type, trade.strike)
            if live >= trade.target_price or live <= trade.stop_loss_price:
                paper_trading_engine.close_trade(trade, df)


position_monitor = PositionMonitor()

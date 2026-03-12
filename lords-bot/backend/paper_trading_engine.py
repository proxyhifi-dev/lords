from __future__ import annotations

from pathlib import Path

import pandas as pd

from config import settings
from models import PnLSnapshot, SignalResult, Trade
from utils import read_json_file, utc_now_iso, write_json_file


class PaperTradingEngine:
    def __init__(self) -> None:
        self.capital = settings.trade_capital
        self.trades_file = Path(__file__).resolve().parent.joinpath(settings.trades_file)
        self.trades: list[Trade] = [Trade(**row) for row in read_json_file(self.trades_file, default=[])]

    def _persist(self) -> None:
        write_json_file(self.trades_file, [trade.model_dump() for trade in self.trades])

    def _current_price(self, df: pd.DataFrame, position_type: str, strike: float) -> float:
        if df.empty:
            return 0.0
        row = df.iloc[(df['strike_price'] - strike).abs().argsort()[:1]].iloc[0]
        return float(row['call_ltp'] if position_type == 'CALL' else row['put_ltp'])

    def evaluate(self, signal: SignalResult, df: pd.DataFrame, symbol: str, atm_strike: float) -> None:
        open_trade = next((t for t in self.trades if t.status == 'OPEN'), None)

        desired_type = 'CALL' if signal.signal == 'BUY CALL' else 'PUT' if signal.signal == 'BUY PUT' else None
        if open_trade and desired_type and open_trade.position_type != desired_type:
            self._close_trade(open_trade, df)
            open_trade = None
        elif open_trade and desired_type is None:
            self._close_trade(open_trade, df)
            open_trade = None

        if desired_type and open_trade is None:
            entry = self._current_price(df, desired_type, atm_strike)
            trade = Trade(
                id=(self.trades[-1].id + 1) if self.trades else 1,
                position_type=desired_type,
                symbol=symbol,
                strike=float(atm_strike),
                entry_price=entry,
                entry_time=utc_now_iso(),
            )
            self.trades.append(trade)
            self._persist()

    def _close_trade(self, trade: Trade, df: pd.DataFrame) -> None:
        exit_price = self._current_price(df, trade.position_type, trade.strike)
        trade.exit_price = exit_price
        trade.exit_time = utc_now_iso()
        trade.status = 'CLOSED'
        trade.pnl = round((exit_price - trade.entry_price) * trade.quantity, 2)
        self._persist()

    def get_trades(self) -> list[dict]:
        return [t.model_dump() for t in self.trades]

    def get_pnl(self, df: pd.DataFrame | None = None) -> PnLSnapshot:
        realized = sum(t.pnl for t in self.trades if t.status == 'CLOSED')
        unrealized = 0.0
        open_trades = [t for t in self.trades if t.status == 'OPEN']
        if df is not None and not df.empty:
            for t in open_trades:
                live = self._current_price(df, t.position_type, t.strike)
                unrealized += round((live - t.entry_price) * t.quantity, 2)

        total = realized + unrealized
        return PnLSnapshot(
            capital=self.capital,
            realized_pnl=round(realized, 2),
            unrealized_pnl=round(unrealized, 2),
            total_pnl=round(total, 2),
            open_positions=len(open_trades),
        )


paper_trading_engine = PaperTradingEngine()

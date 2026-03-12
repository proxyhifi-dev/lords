from __future__ import annotations

from pathlib import Path

import pandas as pd

from config import settings, yaml_config
from database import database
from models import PnLSnapshot, SignalResult, Trade
from utils import read_json_file, utc_now_iso, write_json_file


class PaperTradingEngine:
    def __init__(self) -> None:
        self.capital = settings.trade_capital
        self.trades_file = Path(settings.trades_file)
        self.trades: list[Trade] = [Trade(**row) for row in read_json_file(self.trades_file, default=[])]

    def _persist(self) -> None:
        write_json_file(self.trades_file, [trade.model_dump() for trade in self.trades])
        with database.connection() as conn:
            conn.execute('DELETE FROM paper_trades')
            conn.executemany(
                '''INSERT INTO paper_trades (trade_id, position_type, symbol, strike, entry_price, exit_price, quantity, status, entry_time, exit_time, pnl, target_price, stop_loss_price)
                VALUES (:trade_id, :position_type, :symbol, :strike, :entry_price, :exit_price, :quantity, :status, :entry_time, :exit_time, :pnl, :target_price, :stop_loss_price)''',
                [trade.model_dump() for trade in self.trades],
            )

    def _current_price(self, df: pd.DataFrame, position_type: str, strike: float) -> float:
        row = df.iloc[(df['strike_price'] - strike).abs().argsort()[:1]].iloc[0]
        return float(row['call_ltp'] if position_type == 'CALL' else row['put_ltp'])

    def _calculate_quantity(self, entry_price: float) -> int:
        risk_amount = self.capital * yaml_config.max_risk_per_trade
        per_lot_risk = max(entry_price * 0.1 * yaml_config.lot_size, 1)
        lots = max(1, int(risk_amount // per_lot_risk))
        return lots * yaml_config.lot_size

    def evaluate(self, signal: SignalResult, df: pd.DataFrame, symbol: str, atm_strike: float) -> None:
        if df.empty:
            return
        open_trade = next((t for t in self.trades if t.status == 'OPEN'), None)
        desired_type = 'CALL' if signal.signal == 'BUY CALL' else 'PUT' if signal.signal == 'BUY PUT' else None

        if open_trade and (desired_type is None or open_trade.position_type != desired_type):
            self.close_trade(open_trade, df)
            open_trade = None

        if desired_type and not open_trade:
            entry = self._current_price(df, desired_type, atm_strike)
            quantity = self._calculate_quantity(entry)
            trade = Trade(
                trade_id=(self.trades[-1].trade_id + 1) if self.trades else 1,
                position_type=desired_type,
                symbol=symbol,
                strike=float(atm_strike),
                entry_price=entry,
                quantity=quantity,
                entry_time=utc_now_iso(),
                target_price=round(entry * 1.2, 2),
                stop_loss_price=round(entry * 0.9, 2),
            )
            self.trades.append(trade)
            self._persist()

    def close_trade(self, trade: Trade, df: pd.DataFrame) -> None:
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
        open_trades = [t for t in self.trades if t.status == 'OPEN']
        unrealized = 0.0
        if df is not None and not df.empty:
            unrealized = sum((self._current_price(df, t.position_type, t.strike) - t.entry_price) * t.quantity for t in open_trades)
        return PnLSnapshot(
            capital=self.capital,
            realized_pnl=round(realized, 2),
            unrealized_pnl=round(unrealized, 2),
            total_pnl=round(realized + unrealized, 2),
            open_positions=len(open_trades),
            total_trades=len(self.trades),
        )


paper_trading_engine = PaperTradingEngine()

"""
Lords Bot — Trade Store
Writes closed trades to data/trades.csv.
In-memory ring-buffer of last 200 trades for dashboard.
"""
from __future__ import annotations

import csv
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.app.core.config_loader import get_settings
from backend.app.utils.logger import get_logger

settings = get_settings()
logger   = get_logger("trade_store")

FIELDS = [
    "date", "time", "signal", "symbol", "strike",
    "entry", "exit_price", "qty", "pnl", "daily_pnl",
    "reason", "order_id", "sell_order_id",
]


class TradeStore:

    def __init__(self, trades_file: str | None = None):
        self._file   = Path(trades_file or settings.trades_file)
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._recent: deque[dict] = deque(maxlen=200)
        self._daily_pnl: float = 0.0
        self._load_existing()

    def _load_existing(self) -> None:
        if not self._file.exists():
            self._write_header(); return
        try:
            today = datetime.now().date().isoformat()
            with self._file.open(encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if row.get("date") == today:
                        self._recent.append(row)
                        try: self._daily_pnl += float(row.get("pnl") or 0)
                        except ValueError: pass
            logger.info("TradeStore loaded %d today trades pnl=%.2f", len(self._recent), self._daily_pnl)
        except Exception as exc:
            logger.warning("TradeStore load failed: %s", exc)
            self._write_header()

    def _write_header(self) -> None:
        with self._file.open("w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=FIELDS).writeheader()

    def append_trade(self, trade: dict[str, Any], daily_pnl: float | None = None) -> None:
        now = datetime.now(UTC)
        row = {
            "date":          now.date().isoformat(),
            "time":          now.strftime("%H:%M:%S"),
            "signal":        trade.get("signal", ""),
            "symbol":        trade.get("symbol", ""),
            "strike":        trade.get("strike", ""),
            "entry":         trade.get("entry_price", ""),
            "exit_price":    trade.get("exit_price", ""),
            "qty":           trade.get("qty", ""),
            "pnl":           trade.get("pnl", ""),
            "daily_pnl":     round(daily_pnl or 0, 2),
            "reason":        trade.get("exit_reason", ""),
            "order_id":      trade.get("order_id", ""),
            "sell_order_id": trade.get("sell_order_id", ""),
        }
        self._recent.append(row)
        if daily_pnl is not None:
            self._daily_pnl = daily_pnl

        if not self._file.exists() or self._file.stat().st_size == 0:
            self._write_header()

        with self._file.open("a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=FIELDS).writerow(row)

        logger.info("Trade logged %s pnl=₹%.2f reason=%s",
                    row["symbol"], float(row.get("pnl") or 0), row["reason"])

    def get_all_trades(self) -> list[dict]:
        return list(self._recent)

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def daily_reset(self) -> None:
        self._daily_pnl = 0.0
        self._recent.clear()
        logger.info("TradeStore daily reset")

# backend/app/storage/trade_store.py

from __future__ import annotations

import csv
import os
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.app.core.config_loader import get_settings
from backend.app.utils.logger import get_logger

settings = get_settings()
logger   = get_logger("trade_store")

IST = ZoneInfo("Asia/Kolkata")

# ✅ ENHANCED: added fee breakdown, slippage, quality_score, t1_exit_price, holding_seconds
FIELDS = [
    "date", "time", "entry_time", "exit_time",
    "signal", "symbol", "strike",
    "entry_price", "entry_ltp", "entry_slippage",
    "exit_price", "qty",
    "t1_hit", "t1_exit_price", "t1_pnl",
    "gross_pnl", "pnl",
    "brokerage", "stt", "exchange_fee", "gst", "stamp_duty", "total_charges",
    "daily_pnl", "reason", 
    "order_id", "sell_order_id",
    "quality_score", "regime",
    "holding_seconds",
]

class TradeStore:

    def __init__(self, trades_file: str | None = None):
        self._file   = Path(trades_file or settings.trades_file)
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._recent: deque[dict] = deque(maxlen=200)
        self._daily_pnl: float = 0.0
        self._lock = threading.Lock()
        self._load_existing()

    def _load_existing(self) -> None:
        if not self._file.exists():
            self._write_header()
            return

        try:
            today = datetime.now(IST).date().isoformat()

            with self._file.open(encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if row.get("date") == today:
                        self._recent.append(row)
                        try:
                            self._daily_pnl += float(row.get("pnl") or 0)
                        except (ValueError, TypeError):
                            continue

            logger.info(
                "TradeStore loaded %d trades pnl=%.2f",
                len(self._recent), self._daily_pnl
            )

        except Exception as exc:
            logger.warning("TradeStore load failed: %s", exc)
            self._write_header()

    def _write_header(self) -> None:
        with self._file.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()

    def append_trade(self, trade: dict[str, Any], daily_pnl: float | None = None) -> None:
        with self._lock:
            now = datetime.now(IST)
            
            # Extract charges (if present in trade dict)
            charges = trade.get("charges", {})

            row = {
                "date": now.date().isoformat(),
                "time": now.strftime("%H:%M:%S"),
                "entry_time": trade.get("entry_time", ""),
                "exit_time": trade.get("exit_time", ""),
                "signal": trade.get("signal", ""),
                "symbol": trade.get("symbol", ""),
                "strike": trade.get("strike", ""),
                "entry_price": trade.get("entry_price", ""),
                "entry_ltp": trade.get("entry_ltp", ""),
                "entry_slippage": round(trade.get("entry_price", 0) - trade.get("entry_ltp", 0), 2),
                "exit_price": trade.get("exit_price", ""),
                "qty": trade.get("qty", ""),
                "t1_hit": trade.get("t1_hit", False),
                "t1_exit_price": trade.get("t1_exit_price", ""),
                "t1_pnl": trade.get("t1_pnl", ""),
                "gross_pnl": trade.get("gross_pnl", ""),
                "pnl": trade.get("pnl", ""),
                "brokerage": charges.get("brokerage_total", ""),
                "stt": charges.get("stt_sell", ""),
                "exchange_fee": charges.get("exch_txn_total", ""),
                "gst": charges.get("gst", ""),
                "stamp_duty": charges.get("stamp_duty", ""),
                "total_charges": charges.get("total_charges", ""),
                "daily_pnl": round(daily_pnl or 0, 2),
                "reason": trade.get("exit_reason", ""),
                "order_id": trade.get("order_id", ""),
                "sell_order_id": trade.get("sell_order_id", ""),
                "quality_score": trade.get("quality_score", ""),
                "regime": trade.get("regime", ""),
                "holding_seconds": trade.get("holding_seconds", ""),
            }

            self._recent.append(row)

            if daily_pnl is not None:
                self._daily_pnl = daily_pnl

            if not self._file.exists() or self._file.stat().st_size == 0:
                self._write_header()

            try:
                with self._file.open("a", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=FIELDS)
                    writer.writerow(row)
                    fh.flush()
                    os.fsync(fh.fileno())
            except Exception as exc:
                logger.error("TradeStore write failed: %s", exc)

            try:
                pnl_val = float(row.get("pnl") or 0)
            except (ValueError, TypeError):
                pnl_val = 0.0

            logger.info(
                "Trade logged %s pnl=₹%.2f reason=%s",
                row["symbol"], pnl_val, row["reason"]
            )

    def get_all_trades(self) -> list[dict]:
        """Fetch all historical trades from the CSV."""
        try:
            with self._file.open(encoding="utf-8") as fh:
                return list(csv.DictReader(fh))
        except Exception:
            # Fallback to in-memory deque if file read fails
            return list(self._recent)

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def daily_reset(self) -> None:
        self._daily_pnl = 0.0
        self._recent.clear()
        logger.info("TradeStore daily reset")
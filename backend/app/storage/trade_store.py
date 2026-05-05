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
logger = get_logger("trade_store")
IST = ZoneInfo("Asia/Kolkata")

FIELDS = [
    "date",
    "time",
    "entry_time",
    "exit_time",
    "strategy",
    "signal",
    "symbol",
    "underlying",
    "expiry",
    "strike",
    "entry_price",
    "entry_ltp",
    "entry_slippage",
    "exit_price",
    "exit_premium",
    "qty",
    "t1_hit",
    "t1_exit_price",
    "t1_pnl",
    "gross_pnl",
    "pnl",
    "net_pnl",
    "brokerage",
    "stt",
    "exchange_fee",
    "gst",
    "stamp_duty",
    "total_charges",
    "daily_pnl",
    "reason",
    "order_id",
    "sell_order_id",
    "pricing_source",
    "quality_score",
    "regime",
    "holding_seconds",
]


class TradeStore:
    def __init__(self, trades_file: str | None = None):
        self._file = Path(trades_file or settings.trades_file)
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._recent: deque[dict[str, Any]] = deque(maxlen=200)
        self._daily_pnl: float = 0.0
        self._lock = threading.Lock()
        self._load_existing()

    def _write_header(self) -> None:
        with self._file.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=FIELDS,
                extrasaction="ignore",
            )
            writer.writeheader()

    @staticmethod
    def _clean_text(value: Any, default: str = "") -> str:
        if value is None:
            return default
        text = str(value).strip()
        return text if text else default

    @staticmethod
    def _to_float(value: Any, default: float | None = None) -> float | None:
        if value is None:
            return default
        text = str(value).strip()
        if not text:
            return default
        try:
            return float(text)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        return text in {"1", "true", "yes", "y"}

    @staticmethod
    def _is_numeric_text(value: Any) -> bool:
        if value is None:
            return False
        try:
            float(str(value).strip())
            return True
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _known_exit_reason(value: Any) -> bool:
        if value is None:
            return False
        text = str(value).strip().upper()
        if not text:
            return False
        return any(
            token in text
            for token in (
                "TARGET",
                "STOP",
                "SL",
                "EOD",
                "SQUAREOFF",
                "THETA",
                "EXIT",
                "TRAIL",
                "FORCED",
            )
        )

    def _compute_holding_seconds(self, entry_time: Any, exit_time: Any) -> str | int:
        if not entry_time or not exit_time:
            return ""
        try:
            entry_dt = datetime.fromisoformat(str(entry_time))
            exit_dt = datetime.fromisoformat(str(exit_time))
            return max(int((exit_dt - entry_dt).total_seconds()), 0)
        except Exception:
            return ""

    def _normalize_reason(self, raw_reason: Any, order_id: Any, fallback: str = "") -> str:
        reason = self._clean_text(raw_reason)
        order_marker = self._clean_text(order_id).upper()

        if reason and not self._is_numeric_text(reason):
            return reason

        if self._known_exit_reason(order_marker):
            return order_marker

        return fallback

    def _normalize_symbol(self, row: dict[str, Any]) -> str:
        strategy = self._clean_text(row.get("strategy") or row.get("signal")).upper()
        symbol = self._clean_text(row.get("symbol"))
        underlying = self._clean_text(row.get("underlying"))

        if strategy == "IRON_CONDOR":
            if symbol.upper() == "IRON_CONDOR":
                return underlying or settings.nifty_symbol
            return symbol or underlying or settings.nifty_symbol

        return symbol or underlying or ""

    def _derive_total_charges(
        self,
        gross_pnl: Any,
        net_pnl: Any,
        explicit_total_charges: Any,
    ) -> float | str:
        explicit = self._to_float(explicit_total_charges, None)
        if explicit is not None:
            return round(explicit, 2)

        gross = self._to_float(gross_pnl, None)
        net = self._to_float(net_pnl, None)
        if gross is None or net is None:
            return ""

        return round(abs(gross - net), 2)

    def _normalize_loaded_row(self, raw_row: dict[str, Any]) -> dict[str, Any]:
        row = dict(raw_row)

        strategy = self._clean_text(row.get("strategy") or row.get("signal"))
        signal = self._clean_text(row.get("signal") or strategy)
        symbol = self._normalize_symbol(row)
        underlying = self._clean_text(row.get("underlying") or symbol or settings.nifty_symbol)
        expiry = self._clean_text(row.get("expiry"))
        strike = self._clean_text(row.get("strike"))
        entry_price = self._clean_text(row.get("entry_price"))
        entry_ltp = self._clean_text(row.get("entry_ltp") or entry_price)
        exit_price = self._clean_text(row.get("exit_price") or row.get("exit_premium"))
        qty = self._clean_text(row.get("qty"))
        gross_pnl = self._clean_text(row.get("gross_pnl"))
        pnl = self._clean_text(row.get("pnl") or row.get("net_pnl"))
        net_pnl = self._clean_text(row.get("net_pnl") or pnl)
        pricing_source = self._clean_text(row.get("pricing_source"))
        order_id = self._clean_text(row.get("order_id"))
        sell_order_id = self._clean_text(row.get("sell_order_id"))
        exit_time = self._clean_text(row.get("exit_time"))
        entry_time = self._clean_text(row.get("entry_time"))
        holding_seconds = row.get("holding_seconds") or self._compute_holding_seconds(entry_time, exit_time)

        total_charges = self._derive_total_charges(
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            explicit_total_charges=row.get("total_charges"),
        )

        reason = self._normalize_reason(
            raw_reason=row.get("reason") or row.get("exit_reason"),
            order_id=order_id,
            fallback="CLOSED" if exit_time else "OPEN",
        )

        brokerage = self._clean_text(row.get("brokerage"))
        if self._is_numeric_text(brokerage) and self._is_numeric_text(net_pnl):
            if abs(float(brokerage) - float(net_pnl)) < 0.0001:
                brokerage = ""

        normalized = {
            "date": self._clean_text(row.get("date")),
            "time": self._clean_text(row.get("time")),
            "entry_time": entry_time,
            "exit_time": exit_time,
            "strategy": strategy,
            "signal": signal,
            "symbol": symbol,
            "underlying": underlying,
            "expiry": expiry,
            "strike": strike,
            "entry_price": entry_price,
            "entry_ltp": entry_ltp,
            "entry_slippage": self._clean_text(row.get("entry_slippage")),
            "exit_price": exit_price,
            "exit_premium": self._clean_text(row.get("exit_premium") or exit_price),
            "qty": qty,
            "t1_hit": self._clean_text(row.get("t1_hit")),
            "t1_exit_price": self._clean_text(row.get("t1_exit_price")),
            "t1_pnl": self._clean_text(row.get("t1_pnl")),
            "gross_pnl": gross_pnl,
            "pnl": pnl,
            "net_pnl": net_pnl,
            "brokerage": brokerage,
            "stt": self._clean_text(row.get("stt")),
            "exchange_fee": self._clean_text(row.get("exchange_fee")),
            "gst": self._clean_text(row.get("gst")),
            "stamp_duty": self._clean_text(row.get("stamp_duty")),
            "total_charges": total_charges,
            "daily_pnl": self._clean_text(row.get("daily_pnl")),
            "reason": reason,
            "order_id": order_id if not self._known_exit_reason(order_id) else "",
            "sell_order_id": sell_order_id,
            "pricing_source": pricing_source,
            "quality_score": self._clean_text(row.get("quality_score")),
            "regime": self._clean_text(row.get("regime")),
            "holding_seconds": holding_seconds,
        }
        return normalized

    def _build_row(self, trade: dict[str, Any], daily_pnl: float | None = None) -> dict[str, Any]:
        now = datetime.now(IST)

        charges_raw = trade.get("charges", {})
        if isinstance(charges_raw, (int, float)):
            charges = {"total_charges": float(charges_raw)}
        elif isinstance(charges_raw, dict):
            charges = dict(charges_raw)
        else:
            charges = {}

        entry_time = self._clean_text(trade.get("entry_time"))
        exit_time = self._clean_text(trade.get("exit_time"))
        holding_seconds = trade.get("holding_seconds") or self._compute_holding_seconds(entry_time, exit_time)

        entry_price = trade.get("entry_price", "")
        entry_ltp = trade.get("entry_ltp", entry_price)
        try:
            entry_slippage = round(float(entry_price or 0) - float(entry_ltp or 0), 2)
        except (TypeError, ValueError):
            entry_slippage = ""

        strategy = self._clean_text(trade.get("strategy") or trade.get("signal"))
        signal = self._clean_text(trade.get("signal") or strategy)
        symbol = self._clean_text(trade.get("symbol"))
        underlying = self._clean_text(trade.get("underlying") or symbol or settings.nifty_symbol)
        expiry = self._clean_text(trade.get("expiry"))
        strike = self._clean_text(trade.get("strike"))
        qty = trade.get("qty", "")
        gross_pnl = trade.get("gross_pnl", "")
        net_pnl = trade.get("net_pnl", trade.get("pnl", ""))
        pnl = trade.get("pnl", net_pnl)

        total_charges = charges.get("total_charges", trade.get("total_charges", ""))
        if total_charges in ("", None):
            total_charges = self._derive_total_charges(gross_pnl, net_pnl, None)

        reason = self._normalize_reason(
            raw_reason=trade.get("exit_reason", trade.get("reason", "")),
            order_id=trade.get("order_id", ""),
            fallback="CLOSED" if exit_time else "OPEN",
        )

        row = {
            "date": now.date().isoformat(),
            "time": now.strftime("%H:%M:%S"),
            "entry_time": entry_time,
            "exit_time": exit_time,
            "strategy": strategy,
            "signal": signal,
            "symbol": symbol or underlying,
            "underlying": underlying,
            "expiry": expiry,
            "strike": strike,
            "entry_price": entry_price,
            "entry_ltp": entry_ltp,
            "entry_slippage": entry_slippage,
            "exit_price": trade.get("exit_price", trade.get("exit_premium", "")),
            "exit_premium": trade.get("exit_premium", trade.get("exit_price", "")),
            "qty": qty,
            "t1_hit": trade.get("t1_hit", False),
            "t1_exit_price": trade.get("t1_exit_price", ""),
            "t1_pnl": trade.get("t1_pnl", ""),
            "gross_pnl": gross_pnl,
            "pnl": pnl,
            "net_pnl": net_pnl,
            "brokerage": charges.get("brokerage_total", charges.get("brokerage", "")),
            "stt": charges.get("stt_sell", charges.get("stt", "")),
            "exchange_fee": charges.get("exch_txn_total", charges.get("exchange_txn", "")),
            "gst": charges.get("gst", ""),
            "stamp_duty": charges.get("stamp_duty", ""),
            "total_charges": total_charges,
            "daily_pnl": round(daily_pnl or 0, 2),
            "reason": reason,
            "order_id": trade.get("order_id", ""),
            "sell_order_id": trade.get("sell_order_id", ""),
            "pricing_source": trade.get("pricing_source", ""),
            "quality_score": trade.get("quality_score", ""),
            "regime": trade.get("regime", ""),
            "holding_seconds": holding_seconds,
        }

        return self._normalize_loaded_row(row)

    def _load_existing(self) -> None:
        if not self._file.exists():
            self._write_header()
            return

        try:
            today = datetime.now(IST).date().isoformat()
            with self._file.open(encoding="utf-8") as fh:
                for raw_row in csv.DictReader(fh):
                    row = self._normalize_loaded_row(raw_row)
                    if row.get("date") == today:
                        self._recent.append(row)
                        try:
                            pnl_val = row.get("net_pnl") or row.get("pnl") or 0
                            self._daily_pnl += float(pnl_val)
                        except (ValueError, TypeError):
                            continue

            logger.info(
                "TradeStore loaded %d trades pnl=%.2f",
                len(self._recent),
                self._daily_pnl,
            )
        except Exception as exc:
            logger.warning("TradeStore load failed: %s", exc)
            self._write_header()

    def append_trade(self, trade: dict[str, Any], daily_pnl: float | None = None) -> None:
        with self._lock:
            row = self._build_row(trade, daily_pnl=daily_pnl)

            self._recent.append(row)
            if daily_pnl is not None:
                self._daily_pnl = daily_pnl

            if not self._file.exists() or self._file.stat().st_size == 0:
                self._write_header()

            try:
                with self._file.open("a", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(
                        fh,
                        fieldnames=FIELDS,
                        extrasaction="ignore",
                    )
                    writer.writerow(row)
                    fh.flush()
                    os.fsync(fh.fileno())
            except Exception as exc:
                logger.error("TradeStore write failed: %s", exc)

            try:
                pnl_val = float(row.get("net_pnl") or row.get("pnl") or 0)
            except (ValueError, TypeError):
                pnl_val = 0.0

            logger.info(
                "Trade logged %s pnl=₹%.2f reason=%s",
                row["symbol"],
                pnl_val,
                row["reason"],
            )

    def get_all_trades(self) -> list[dict[str, Any]]:
        try:
            with self._file.open(encoding="utf-8") as fh:
                return [self._normalize_loaded_row(row) for row in csv.DictReader(fh)]
        except Exception:
            return [self._normalize_loaded_row(row) for row in list(self._recent)]

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def daily_reset(self) -> None:
        self._daily_pnl = 0.0
        self._recent.clear()
        logger.info("TradeStore daily reset")
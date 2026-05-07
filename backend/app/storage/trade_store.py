# backend/app/storage/trade_store.py
from __future__ import annotations

import csv
import json
import os
import shutil
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
    "legs_json",
    "exit_legs_json",
]


class TradeStore:
    def __init__(self, trades_file: str | None = None):
        self._file = Path(trades_file or settings.trades_file)
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._recent: deque[dict[str, Any]] = deque(maxlen=200)
        self._daily_pnl: float = 0.0
        self._lock = threading.RLock()
        self._load_existing()

    def _write_header(self) -> None:
        with self._file.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
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
        text = str(value).strip().replace(",", "")
        if not text:
            return default
        try:
            return float(text)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        try:
            return int(float(str(value).strip().replace(",", "")))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _is_numeric_text(value: Any) -> bool:
        if value is None:
            return False
        try:
            float(str(value).strip().replace(",", ""))
            return True
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _setting_float(name: str, fallback: float | None = None) -> float:
        value = getattr(settings, name, fallback)
        if value is None or str(value).strip() == "":
            raise RuntimeError(f"Missing numeric setting: {name}")
        return float(value)

    @staticmethod
    def _looks_like_expiry(value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return False

        for fmt in ("%Y-%m-%d", "%d-%b-%y", "%d-%b-%Y", "%d %b %y", "%d %b %Y"):
            try:
                datetime.strptime(text, fmt)
                return True
            except ValueError:
                continue

        return False

    @staticmethod
    def _looks_like_strike_pair(value: Any) -> bool:
        text = str(value or "").strip()
        if "/" not in text:
            return False

        left, right = text.split("/", 1)
        return left.strip().isdigit() and right.strip().isdigit()

    @staticmethod
    def _known_exit_reason(value: Any) -> bool:
        text = str(value or "").strip().upper()
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
                "LOSS",
                "CLOSED",
                "EXPIRY",
                "MANUAL",
                "FLATTEN",
            )
        )

    @staticmethod
    def _drop_extra_csv_columns(row: dict[str, Any]) -> dict[str, Any]:
        cleaned = dict(row)
        cleaned.pop(None, None)
        return cleaned

    @staticmethod
    def _json_dumps(value: Any) -> str:
        if value in ("", None):
            return ""

        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            logger.warning("Failed to JSON serialize trade legs")
            return ""

    @staticmethod
    def _json_loads_list(value: Any) -> list[dict[str, Any]]:
        if not value:
            return []

        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

        if isinstance(value, tuple):
            return [item for item in value if isinstance(item, dict)]

        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

        if not isinstance(parsed, list):
            return []

        return [item for item in parsed if isinstance(item, dict)]

    @staticmethod
    def _normalize_leg_number(value: Any) -> int | float | Any:
        number = TradeStore._to_float(value, None)
        if number is None:
            return value
        if float(number).is_integer():
            return int(number)
        return round(number, 2)

    @staticmethod
    def _normalize_leg(leg: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(leg)

        for key in (
            "entry_price",
            "fill_price",
            "price",
            "entry_bid",
            "entry_ask",
            "entry_ltp",
            "exit_price",
            "exit_bid",
            "exit_ask",
            "exit_ltp",
            "current_price",
            "current_close_price",
            "current_bid",
            "current_ask",
            "current_ltp",
            "qty",
            "filled_qty",
            "strike",
        ):
            if key in normalized:
                normalized[key] = TradeStore._normalize_leg_number(normalized.get(key))

        if "side" in normalized:
            normalized["side"] = str(normalized["side"]).upper()

        if "name" in normalized:
            normalized["name"] = str(normalized["name"]).strip().lower()

        return normalized

    def _normalize_legs(self, legs: Any) -> list[dict[str, Any]]:
        return [self._normalize_leg(leg) for leg in self._json_loads_list(legs)]

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
            return reason.upper()

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
        if explicit is not None and explicit > 0:
            return round(explicit, 2)

        gross = self._to_float(gross_pnl, None)
        net = self._to_float(net_pnl, None)

        if gross is None or net is None:
            return ""

        derived = abs(gross - net)
        return round(derived, 2) if derived > 0 else ""

    def _estimate_intraday_charges(
        self,
        entry_price: Any,
        exit_price: Any,
        qty: Any,
        strategy: Any,
    ) -> dict[str, float]:
        entry = self._to_float(entry_price, 0.0) or 0.0
        exit_ = self._to_float(exit_price, 0.0) or 0.0
        quantity = self._to_int(qty, 0)

        if entry <= 0 or exit_ < 0 or quantity <= 0:
            return self._zero_charges()

        strategy_name = self._clean_text(strategy).upper()

        if strategy_name == "IRON_CONDOR":
            brokerage = self._setting_float("ic_platform_charges")
            stt_rate = self._setting_float("ic_stt_rate")
            exchange_rate = self._setting_float("ic_exchange_txn_rate")
            gst_rate = self._setting_float("ic_gst_rate")
            stamp_duty_rate = self._setting_float("ic_stamp_duty_rate")
        else:
            brokerage = self._setting_float("platform_charges", 0.0)
            stt_rate = self._setting_float("stt_rate", 0.0)
            exchange_rate = self._setting_float("exchange_txn_rate", 0.0)
            gst_rate = self._setting_float("gst_rate", 0.0)
            stamp_duty_rate = self._setting_float("stamp_duty_rate", 0.0)

        turnover = (entry + exit_) * quantity
        stt = entry * quantity * stt_rate
        exchange_fee = turnover * exchange_rate
        stamp_duty = entry * quantity * stamp_duty_rate
        gst = (brokerage + exchange_fee) * gst_rate
        total = brokerage + stt + exchange_fee + gst + stamp_duty

        return {
            "brokerage": round(brokerage, 2),
            "stt": round(stt, 2),
            "exchange_fee": round(exchange_fee, 2),
            "gst": round(gst, 2),
            "stamp_duty": round(stamp_duty, 2),
            "total_charges": round(total, 2),
        }

    @staticmethod
    def _zero_charges() -> dict[str, float]:
        return {
            "brokerage": 0.0,
            "stt": 0.0,
            "exchange_fee": 0.0,
            "gst": 0.0,
            "stamp_duty": 0.0,
            "total_charges": 0.0,
        }

    def _repair_shifted_row(self, row: dict[str, Any]) -> dict[str, Any]:
        repaired = self._drop_extra_csv_columns(row)

        signal = self._clean_text(repaired.get("signal")).upper()
        symbol = self._clean_text(repaired.get("symbol"))
        strike = self._clean_text(repaired.get("strike"))
        entry_price = self._clean_text(repaired.get("entry_price"))
        entry_ltp = self._clean_text(repaired.get("entry_ltp"))
        entry_slippage = self._clean_text(repaired.get("entry_slippage"))
        exit_price = self._clean_text(repaired.get("exit_price"))
        qty = self._clean_text(repaired.get("qty"))
        t1_hit = self._clean_text(repaired.get("t1_hit"))
        gross_pnl = self._clean_text(repaired.get("gross_pnl"))
        pnl = self._clean_text(repaired.get("pnl"))
        reason = self._clean_text(repaired.get("reason"))
        order_id = self._clean_text(repaired.get("order_id"))
        pricing_source = self._clean_text(repaired.get("pricing_source"))
        holding_seconds = self._clean_text(repaired.get("holding_seconds"))

        if (
            signal == "IRON_CONDOR"
            and symbol.upper() == "IRON_CONDOR"
            and not self._looks_like_strike_pair(strike)
            and self._looks_like_expiry(entry_ltp)
            and self._looks_like_strike_pair(entry_slippage)
        ):
            repaired["strategy"] = "IRON_CONDOR"
            repaired["symbol"] = strike or settings.nifty_symbol
            repaired["underlying"] = entry_price or repaired["symbol"]
            repaired["expiry"] = entry_ltp
            repaired["strike"] = entry_slippage
            repaired["entry_price"] = exit_price
            repaired["entry_ltp"] = qty
            repaired["entry_slippage"] = t1_hit
            repaired["exit_price"] = repaired.get("t1_exit_price", "")
            repaired["exit_premium"] = repaired.get("t1_pnl", "")
            repaired["qty"] = gross_pnl
            repaired["t1_hit"] = pnl
            repaired["t1_exit_price"] = ""
            repaired["t1_pnl"] = ""
            repaired["gross_pnl"] = repaired.get("brokerage", "")
            repaired["pnl"] = repaired.get("stt", "")
            repaired["net_pnl"] = repaired.get("exchange_fee", "")
            repaired["brokerage"] = repaired.get("gst", "")
            repaired["stt"] = repaired.get("stamp_duty", "")
            repaired["exchange_fee"] = repaired.get("total_charges", "")
            repaired["gst"] = repaired.get("daily_pnl", "")
            repaired["stamp_duty"] = repaired.get("reason", "")
            repaired["total_charges"] = repaired.get("order_id", "")
            repaired["daily_pnl"] = repaired.get("sell_order_id", "")
            repaired["reason"] = pricing_source
            repaired["order_id"] = repaired.get("quality_score", "")
            repaired["sell_order_id"] = repaired.get("regime", "")
            repaired["pricing_source"] = repaired.get("holding_seconds", "")
            repaired["quality_score"] = ""
            repaired["regime"] = ""
            repaired["holding_seconds"] = ""

        if self._known_exit_reason(order_id) and not self._known_exit_reason(reason):
            repaired["reason"] = order_id
            repaired["order_id"] = ""

        if not repaired.get("pricing_source") and self._clean_text(
            repaired.get("quality_score")
        ).lower() in {"broker_quote_snapshot", "broker_fill", "model_fallback"}:
            repaired["pricing_source"] = repaired.get("quality_score", "")
            repaired["quality_score"] = ""

        if holding_seconds and self._is_numeric_text(holding_seconds):
            repaired["holding_seconds"] = holding_seconds

        return repaired

    def _repair_missing_charges(self, row: dict[str, Any]) -> dict[str, Any]:
        repaired = dict(row)
        exit_time = self._clean_text(repaired.get("exit_time"))

        if not exit_time:
            return repaired

        gross = self._to_float(repaired.get("gross_pnl"), None)
        net = self._to_float(repaired.get("net_pnl") or repaired.get("pnl"), None)
        total_charges = self._to_float(repaired.get("total_charges"), None)

        if gross is None:
            return repaired

        if total_charges is not None and total_charges > 0:
            return repaired

        estimated = self._estimate_intraday_charges(
            entry_price=repaired.get("entry_price"),
            exit_price=repaired.get("exit_price") or repaired.get("exit_premium"),
            qty=repaired.get("qty"),
            strategy=repaired.get("strategy") or repaired.get("signal"),
        )

        if estimated["total_charges"] <= 0:
            return repaired

        repaired["brokerage"] = estimated["brokerage"]
        repaired["stt"] = estimated["stt"]
        repaired["exchange_fee"] = estimated["exchange_fee"]
        repaired["gst"] = estimated["gst"]
        repaired["stamp_duty"] = estimated["stamp_duty"]
        repaired["total_charges"] = estimated["total_charges"]

        if net is None or abs(net - gross) < 0.0001:
            repaired["net_pnl"] = round(gross - estimated["total_charges"], 2)
            repaired["pnl"] = repaired["net_pnl"]

        return repaired

    def _build_fallback_legs_from_trade(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        strategy = self._clean_text(row.get("strategy") or row.get("signal")).upper()
        if strategy != "IRON_CONDOR":
            return []

        strikes = row.get("strikes") if isinstance(row.get("strikes"), dict) else {}
        premiums = row.get("premiums") if isinstance(row.get("premiums"), dict) else {}

        short_call = strikes.get("short_call")
        long_call = strikes.get("long_call")
        short_put = strikes.get("short_put")
        long_put = strikes.get("long_put")

        if not (short_call and long_call and short_put and long_put):
            return []

        qty = self._to_int(row.get("qty"), 0)
        expiry = self._clean_text(row.get("expiry"))
        underlying = self._clean_text(row.get("underlying") or row.get("symbol") or settings.nifty_symbol)

        def leg(
            name: str,
            side: str,
            strike: Any,
            option_type: str,
            premium_key: str,
        ) -> dict[str, Any]:
            price = self._to_float(premiums.get(premium_key), 0.0) or 0.0
            return self._normalize_leg(
                {
                    "name": name,
                    "side": side,
                    "strike": strike,
                    "option_type": option_type,
                    "entry_price": price,
                    "fill_price": price,
                    "qty": qty,
                    "display_symbol": f"{underlying} {expiry} {strike} {option_type}".strip(),
                }
            )

        return [
            leg("short_call", "SELL", short_call, "CE", "short_call"),
            leg("long_call", "BUY", long_call, "CE", "long_call"),
            leg("short_put", "SELL", short_put, "PE", "short_put"),
            leg("long_put", "BUY", long_put, "PE", "long_put"),
        ]

    def _normalize_loaded_row(self, raw_row: dict[str, Any]) -> dict[str, Any]:
        row = self._repair_missing_charges(self._repair_shifted_row(dict(raw_row)))

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

        holding_seconds = row.get("holding_seconds") or self._compute_holding_seconds(
            entry_time,
            exit_time,
        )

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

        legs = self._normalize_legs(row.get("legs") or row.get("legs_json"))
        if not legs:
            legs = self._build_fallback_legs_from_trade(row)

        exit_legs = self._normalize_legs(
            row.get("exit_legs")
            or row.get("current_legs")
            or row.get("closed_legs")
            or row.get("exit_legs_json")
        )

        legs_json = self._json_dumps(legs)
        exit_legs_json = self._json_dumps(exit_legs)

        return {
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
            "legs": legs,
            "exit_legs": exit_legs,
            "current_legs": exit_legs,
            "legs_json": legs_json,
            "exit_legs_json": exit_legs_json,
        }

    def _extract_trade_legs(self, trade: dict[str, Any]) -> list[dict[str, Any]]:
        legs = trade.get("legs") or trade.get("legs_json")
        parsed = self._normalize_legs(legs)

        if parsed:
            return parsed

        return self._build_fallback_legs_from_trade(trade)

    def _extract_trade_exit_legs(self, trade: dict[str, Any]) -> list[dict[str, Any]]:
        exit_legs = (
            trade.get("exit_legs")
            or trade.get("current_legs")
            or trade.get("closed_legs")
            or trade.get("exit_legs_json")
        )
        return self._normalize_legs(exit_legs)

    def _build_row(self, trade: dict[str, Any], daily_pnl: float | None = None) -> dict[str, Any]:
        now = datetime.now(IST)
        charges_raw = trade.get("charges", {})

        if isinstance(charges_raw, (int, float)):
            charges = {"total_charges": float(charges_raw)}
        elif isinstance(charges_raw, dict):
            charges = dict(charges_raw)
        else:
            charges = {}

        for source_key, target_key in (
            ("brokerage", "brokerage"),
            ("brokerage_total", "brokerage_total"),
            ("stt", "stt"),
            ("stt_sell", "stt_sell"),
            ("exchange_fee", "exchange_fee"),
            ("exchange_txn", "exchange_txn"),
            ("exch_txn_total", "exch_txn_total"),
            ("gst", "gst"),
            ("stamp_duty", "stamp_duty"),
            ("total_charges", "total_charges"),
        ):
            if source_key in trade and source_key not in charges:
                charges[target_key] = trade[source_key]

        entry_time = self._clean_text(trade.get("entry_time"))
        exit_time = self._clean_text(trade.get("exit_time"))
        holding_seconds = trade.get("holding_seconds") or self._compute_holding_seconds(
            entry_time,
            exit_time,
        )

        strategy = self._clean_text(trade.get("strategy") or trade.get("signal"))
        signal = self._clean_text(trade.get("signal") or strategy)
        symbol = self._clean_text(trade.get("symbol"))
        underlying = self._clean_text(trade.get("underlying") or symbol or settings.nifty_symbol)
        expiry = self._clean_text(trade.get("expiry"))
        strike = self._clean_text(trade.get("strike"))
        entry_price = trade.get("entry_price", "")
        entry_ltp = trade.get("entry_ltp", entry_price)
        exit_price = trade.get("exit_price", trade.get("exit_premium", ""))
        exit_premium = trade.get("exit_premium", exit_price)
        qty = trade.get("qty", "")

        try:
            entry_slippage = round(float(entry_price or 0) - float(entry_ltp or 0), 2)
        except (TypeError, ValueError):
            entry_slippage = ""

        gross_pnl = trade.get("gross_pnl", "")
        pnl = trade.get("pnl", trade.get("net_pnl", ""))
        net_pnl = trade.get("net_pnl", pnl)

        explicit_total_charges = charges.get("total_charges", trade.get("total_charges", ""))
        total_charges = self._derive_total_charges(
            gross_pnl,
            net_pnl,
            explicit_total_charges,
        )

        gross_float = self._to_float(gross_pnl, None)
        net_float = self._to_float(net_pnl, None)
        total_float = self._to_float(total_charges, None)

        if (
            exit_time
            and gross_float is not None
            and abs(gross_float) > 0
            and (total_float is None or total_float <= 0)
        ):
            estimated = self._estimate_intraday_charges(
                entry_price=entry_price,
                exit_price=exit_price or exit_premium,
                qty=qty,
                strategy=strategy,
            )
            charges = {
                **charges,
                "brokerage": estimated["brokerage"],
                "brokerage_total": estimated["brokerage"],
                "stt": estimated["stt"],
                "stt_sell": estimated["stt"],
                "exchange_txn": estimated["exchange_fee"],
                "exch_txn_total": estimated["exchange_fee"],
                "gst": estimated["gst"],
                "stamp_duty": estimated["stamp_duty"],
                "total_charges": estimated["total_charges"],
            }
            total_charges = estimated["total_charges"]

            if net_float is None or abs(net_float - gross_float) < 0.0001:
                net_pnl = round(gross_float - estimated["total_charges"], 2)
                pnl = net_pnl

        if net_float is None and gross_float is not None:
            total_float = self._to_float(total_charges, 0.0) or 0.0
            net_pnl = round(gross_float - total_float, 2)
            pnl = net_pnl

        reason = self._normalize_reason(
            raw_reason=trade.get("exit_reason", trade.get("reason", "")),
            order_id=trade.get("order_id", ""),
            fallback="CLOSED" if exit_time else "OPEN",
        )

        legs = self._extract_trade_legs(trade)
        exit_legs = self._extract_trade_exit_legs(trade)

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
            "exit_price": exit_price,
            "exit_premium": exit_premium,
            "qty": qty,
            "t1_hit": trade.get("t1_hit", False),
            "t1_exit_price": trade.get("t1_exit_price", ""),
            "t1_pnl": trade.get("t1_pnl", ""),
            "gross_pnl": gross_pnl,
            "pnl": pnl,
            "net_pnl": net_pnl,
            "brokerage": charges.get(
                "brokerage_total",
                charges.get("brokerage", trade.get("brokerage", "")),
            ),
            "stt": charges.get("stt_sell", charges.get("stt", trade.get("stt", ""))),
            "exchange_fee": charges.get(
                "exch_txn_total",
                charges.get("exchange_txn", charges.get("exchange_fee", "")),
            ),
            "gst": charges.get("gst", trade.get("gst", "")),
            "stamp_duty": charges.get("stamp_duty", trade.get("stamp_duty", "")),
            "total_charges": total_charges,
            "daily_pnl": round(daily_pnl if daily_pnl is not None else 0.0, 2),
            "reason": reason,
            "order_id": trade.get("order_id", ""),
            "sell_order_id": trade.get("sell_order_id", ""),
            "pricing_source": trade.get("pricing_source", ""),
            "quality_score": trade.get("quality_score", ""),
            "regime": trade.get("regime", ""),
            "holding_seconds": holding_seconds,
            "legs": legs,
            "exit_legs": exit_legs,
            "current_legs": exit_legs,
            "legs_json": self._json_dumps(legs),
            "exit_legs_json": self._json_dumps(exit_legs),
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

                    if row.get("date") != today:
                        continue

                    self._recent.append(row)

                    try:
                        pnl_val = row.get("net_pnl") or row.get("pnl") or 0
                        self._daily_pnl += float(str(pnl_val).replace(",", ""))
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

    def _ensure_current_header(self) -> None:
        if not self._file.exists() or self._file.stat().st_size == 0:
            self._write_header()
            return

        try:
            with self._file.open(encoding="utf-8") as fh:
                reader = csv.reader(fh)
                header = next(reader, [])
        except Exception:
            self._write_header()
            return

        missing_fields = [field for field in FIELDS if field not in header]
        if missing_fields:
            logger.warning(
                "TradeStore CSV schema missing fields=%s; rewriting normalized file",
                missing_fields,
            )
            self.rewrite_normalized_file()

    def append_trade(self, trade: dict[str, Any], daily_pnl: float | None = None) -> None:
        with self._lock:
            row = self._build_row(trade, daily_pnl=daily_pnl)
            self._recent.append(row)

            if daily_pnl is not None:
                self._daily_pnl = daily_pnl

            self._ensure_current_header()

            try:
                with self._file.open("a", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
                    writer.writerow(row)
                    fh.flush()
                    os.fsync(fh.fileno())
            except Exception as exc:
                logger.error("TradeStore write failed: %s", exc)

            try:
                pnl_val = float(str(row.get("net_pnl") or row.get("pnl") or 0).replace(",", ""))
            except (ValueError, TypeError):
                pnl_val = 0.0

            logger.info(
                "Trade logged %s pnl=%.2f charges=%s reason=%s legs=%d exit_legs=%d",
                row["symbol"],
                pnl_val,
                row.get("total_charges", ""),
                row["reason"],
                len(row.get("legs") or []),
                len(row.get("exit_legs") or []),
            )

    def get_all_trades(self) -> list[dict[str, Any]]:
        try:
            with self._file.open(encoding="utf-8") as fh:
                return [self._normalize_loaded_row(row) for row in csv.DictReader(fh)]
        except Exception:
            return [self._normalize_loaded_row(row) for row in list(self._recent)]

    def rewrite_normalized_file(self) -> bool:
        with self._lock:
            if not self._file.exists():
                self._write_header()
                return True

            try:
                backup_path = self._file.with_suffix(self._file.suffix + ".backup")
                shutil.copy2(self._file, backup_path)

                with self._file.open(encoding="utf-8") as fh:
                    rows = [self._normalize_loaded_row(row) for row in csv.DictReader(fh)]

                with self._file.open("w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(rows)
                    fh.flush()
                    os.fsync(fh.fileno())

                logger.info(
                    "TradeStore normalized rewrite complete rows=%d backup=%s",
                    len(rows),
                    backup_path,
                )
                return True
            except Exception as exc:
                logger.error("TradeStore normalized rewrite failed: %s", exc, exc_info=True)
                return False

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def daily_reset(self) -> None:
        self._daily_pnl = 0.0
        self._recent.clear()
        logger.info("TradeStore daily reset")
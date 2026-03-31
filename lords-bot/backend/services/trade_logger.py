from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from config import settings


class TradeLogger:

    def __init__(self, filepath: str | None = None) -> None:

        self.path = Path(filepath or settings.trade_log_file)

        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------
    # LOG TRADE
    # ------------------------------------------------

    def log_trade(self, payload: dict[str, Any]) -> None:

        record = {
            "timestamp": datetime.utcnow().isoformat(),
            **payload,
        }

        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ------------------------------------------------
    # LOAD TRADES
    # ------------------------------------------------

    def load_trades(self) -> list[dict[str, Any]]:

        if not self.path.exists():
            return []

        records: list[dict[str, Any]] = []

        for line in self.path.read_text(encoding="utf-8").splitlines():

            if not line.strip():
                continue

            try:
                records.append(json.loads(line))
            except Exception:
                # skip corrupted line
                continue

        return records

    # ------------------------------------------------
    # PERFORMANCE METRICS
    # ------------------------------------------------

    def performance_summary(self) -> dict[str, Any]:

        trades = self.load_trades()

        total = len(trades)

        wins = 0
        pnl = 0.0

        for t in trades:

            trade_pnl = float(t.get("pnl") or 0.0)

            pnl += trade_pnl

            if trade_pnl > 0:
                wins += 1

        win_rate = (wins / total * 100) if total else 0

        return {
            "total_trades": total,
            "wins": wins,
            "win_rate": round(win_rate, 2),
            "realized_pnl": round(pnl, 2),
        }

    # ------------------------------------------------
    # DAILY PNL
    # ------------------------------------------------

    def daily_pnl(self) -> float:

        trades = self.load_trades()

        today = datetime.utcnow().date()

        pnl = 0.0

        for t in trades:

            ts = t.get("timestamp")

            if not ts:
                continue

            try:
                trade_date = datetime.fromisoformat(ts).date()
            except Exception:
                continue

            if trade_date == today:
                pnl += float(t.get("pnl") or 0)

        return round(pnl, 2)
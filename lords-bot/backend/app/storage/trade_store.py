from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.config import settings


class TradeStore:

    def __init__(self, path: str | None = None) -> None:

        self.path = Path(path or settings.trades_file)

        self.path.parent.mkdir(parents=True, exist_ok=True)

        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    # -----------------------------
    # LOAD TRADES
    # -----------------------------
    def load_trades(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []

    # -----------------------------
    # APPEND TRADE
    # -----------------------------
    def append_trade(self, trade: dict[str, Any]) -> None:

        trades = self.load_trades()

        trades.append(trade)

        self.path.write_text(
            json.dumps(trades, indent=2),
            encoding="utf-8"
        )

    # -----------------------------
    # GET ALL TRADES
    # -----------------------------
    def get_all_trades(self) -> list[dict[str, Any]]:

        return self.load_trades()
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import settings


class TradeLogger:
    def __init__(self, filepath: str | None = None) -> None:
        self.path = Path(filepath or settings.trade_log_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log_trade(self, payload: dict[str, Any]) -> None:
        with self.path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + '\n')

    def load_trades(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

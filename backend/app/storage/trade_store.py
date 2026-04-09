import json
from pathlib import Path
from typing import Dict, Any, List


class TradeStore:

    def __init__(self):

        self.file = Path("trades.json")

        if not self.file.exists():
            self.file.write_text("[]")

    def load(self) -> List[Dict[str, Any]]:
        try:
            return json.loads(self.file.read_text())
        except Exception:
            return []

    def save(self, trade: Dict[str, Any]):

        trades = self.load()

        trades.append(trade)

        self.file.write_text(
            json.dumps(trades, indent=2)
        )

    def clear(self):

        self.file.write_text("[]")
from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.app.core.config_loader import get_settings
from backend.app.utils.logger import get_logger

logger = get_logger("option_store")
settings = get_settings()


@dataclass(slots=True)
class OptionSnapshot:
    timestamp: str
    strike: int
    ce_bid: float
    ce_ask: float
    ce_ltp: float
    pe_bid: float
    pe_ask: float
    pe_ltp: float
    volume: int


class OptionChainCollector:
    """Collects option-chain snapshots and stores them as CSV and JSONL rows."""

    FIELDNAMES = [
        "timestamp",
        "strike",
        "ce_bid",
        "ce_ask",
        "ce_ltp",
        "pe_bid",
        "pe_ask",
        "pe_ltp",
        "volume",
    ]

    def __init__(self, samco_client: Any, *, output_dir: str | Path = "data", interval_seconds: int = 60):
        self._samco_client = samco_client
        self._interval_seconds = interval_seconds
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    async def run_forever(self) -> None:
        logger.info("Option collector started (interval=%ss)", self._interval_seconds)
        while True:
            try:
                await self.collect_once()
            except Exception as exc:  # pragma: no cover - runtime resilience
                logger.exception("Option collection failed: %s", exc)
            await asyncio.sleep(self._interval_seconds)

    async def collect_once(self) -> list[OptionSnapshot]:
        symbol = settings.nifty_symbol
        expiry_date = self._resolve_expiry_date()
        strike = str(await self._resolve_strike())

        ce_raw = await self._samco_client.get_option_chain(
            search_symbol_name=symbol,
            exchange="NFO",
            expiry_date=expiry_date,
            strike_price=strike,
            option_type="CE",
        )
        pe_raw = await self._samco_client.get_option_chain(
            search_symbol_name=symbol,
            exchange="NFO",
            expiry_date=expiry_date,
            strike_price=strike,
            option_type="PE",
        )

        snapshots = self._merge_chain_rows(ce_raw, pe_raw)
        if not snapshots:
            logger.warning("No option rows found for %s expiry=%s", symbol, expiry_date)
            return []

        csv_path, jsonl_path = self._current_paths()
        self._append_csv(csv_path, snapshots)
        self._append_jsonl(jsonl_path, snapshots)

        logger.info("Stored %d option snapshots → %s and %s", len(snapshots), csv_path, jsonl_path)
        return snapshots

    def _merge_chain_rows(self, ce_chain: dict, pe_chain: dict) -> list[OptionSnapshot]:
        ce_rows = self._normalize_chain(ce_chain)
        pe_rows = self._normalize_chain(pe_chain)

        out: list[OptionSnapshot] = []
        ts = datetime.now(timezone.utc).isoformat()

        all_strikes = sorted(set(ce_rows.keys()) | set(pe_rows.keys()))
        for strike in all_strikes:
            ce = ce_rows.get(strike, {})
            pe = pe_rows.get(strike, {})

            volume = int(max(float(ce.get("tradedVolume", 0) or 0), float(pe.get("tradedVolume", 0) or 0)))
            snapshot = OptionSnapshot(
                timestamp=ts,
                strike=int(strike),
                ce_bid=float(ce.get("bestBidPrice", 0) or 0),
                ce_ask=float(ce.get("bestAskPrice", 0) or 0),
                ce_ltp=float(ce.get("lastTradedPrice", 0) or 0),
                pe_bid=float(pe.get("bestBidPrice", 0) or 0),
                pe_ask=float(pe.get("bestAskPrice", 0) or 0),
                pe_ltp=float(pe.get("lastTradedPrice", 0) or 0),
                volume=volume,
            )
            out.append(snapshot)
        return out

    @staticmethod
    def _normalize_chain(raw: dict) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        if not isinstance(raw, dict):
            return rows

        candidates: list[dict[str, Any]] = []
        if isinstance(raw.get("optionChain"), list):
            candidates = [r for r in raw["optionChain"] if isinstance(r, dict)]
        elif isinstance(raw.get("optionChain"), dict):
            nested = raw["optionChain"].get("data")
            if isinstance(nested, list):
                candidates = [r for r in nested if isinstance(r, dict)]
        elif isinstance(raw.get("data"), list):
            candidates = [r for r in raw["data"] if isinstance(r, dict)]

        for row in candidates:
            strike = row.get("strikePrice") or row.get("strike")
            if strike is None:
                continue
            rows[str(strike)] = row
        return rows

    def _current_paths(self) -> tuple[Path, Path]:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        return (
            self._output_dir / f"option_chain_{day}.csv",
            self._output_dir / f"option_chain_{day}.jsonl",
        )

    def _append_csv(self, path: Path, rows: list[OptionSnapshot]) -> None:
        write_header = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=self.FIELDNAMES)
            if write_header:
                writer.writeheader()
            writer.writerows(asdict(r) for r in rows)

    def _append_jsonl(self, path: Path, rows: list[OptionSnapshot]) -> None:
        with path.open("a", encoding="utf-8") as fp:
            for row in rows:
                fp.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")

    async def _resolve_strike(self) -> int:
        quote = await self._samco_client.get_index_quote(settings.nifty_symbol)
        spot = self._samco_client.parse_spot(quote)
        if spot is None:
            raise RuntimeError("Unable to resolve spot for strike selection")
        return int(round(spot / 50.0) * 50)

    @staticmethod
    def _resolve_expiry_date() -> str:
        # Samco expects DD-Mon-YYYY; nearest weekly expiry (Thursday).
        today = datetime.now(timezone.utc).date()
        days_until_thu = (3 - today.weekday()) % 7
        expiry = today + timedelta(days=days_until_thu)
        return expiry.strftime("%d-%b-%Y")

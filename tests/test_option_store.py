from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.app.data.option_store import OptionChainCollector


class _FakeSamcoClient:
    def __init__(self):
        self._option_chains = {
            "CE": {
                "optionChain": [
                    {
                        "strikePrice": "24500",
                        "bestBidPrice": 101.5,
                        "bestAskPrice": 102.0,
                        "lastTradedPrice": 101.7,
                        "tradedVolume": 1500,
                    }
                ]
            },
            "PE": {
                "optionChain": [
                    {
                        "strikePrice": "24500",
                        "bestBidPrice": 98.2,
                        "bestAskPrice": 98.8,
                        "lastTradedPrice": 98.5,
                        "tradedVolume": 1800,
                    }
                ]
            },
        }

    async def get_option_chain(self, **kwargs):
        return self._option_chains[kwargs["option_type"]]

    async def get_index_quote(self, index_name: str):
        return {"indexDetails": [{"spotPrice": "24,512.65"}]}

    @staticmethod
    def parse_spot(quote):
        return 24512.65


def test_collect_once_writes_csv_and_jsonl(tmp_path: Path) -> None:
    async def _run() -> None:
        collector = OptionChainCollector(_FakeSamcoClient(), output_dir=tmp_path)

        snapshots = await collector.collect_once()

        assert len(snapshots) == 1
        assert snapshots[0].strike == 24500

        csv_files = list(tmp_path.glob("option_chain_*.csv"))
        jsonl_files = list(tmp_path.glob("option_chain_*.jsonl"))
        assert len(csv_files) == 1
        assert len(jsonl_files) == 1

        csv_text = csv_files[0].read_text(encoding="utf-8")
        assert "ce_bid" in csv_text
        assert "24500" in csv_text

        row = json.loads(jsonl_files[0].read_text(encoding="utf-8").strip())
        assert row["pe_ltp"] == pytest.approx(98.5)

    asyncio.run(_run())


def test_collect_once_returns_empty_on_missing_chain(tmp_path: Path) -> None:
    async def _run() -> None:
        client = _FakeSamcoClient()
        client._option_chains["CE"] = {}
        client._option_chains["PE"] = {}

        collector = OptionChainCollector(client, output_dir=tmp_path)
        snapshots = await collector.collect_once()

        assert snapshots == []
        assert not list(tmp_path.glob("option_chain_*.csv"))

    asyncio.run(_run())

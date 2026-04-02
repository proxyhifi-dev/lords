from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from broker.samco_broker import build_nfo_option_symbol, next_weekly_expiry


def test_symbol_builder_and_expiry_smoke() -> None:
    expiry = next_weekly_expiry()
    symbol = build_nfo_option_symbol('NIFTY', expiry, 22000, 'CE')
    assert symbol.startswith('NIFTY')
    assert symbol.endswith('CE')

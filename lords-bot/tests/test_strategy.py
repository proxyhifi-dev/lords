from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from strategies.orb_strategy import OrbStrategy


def test_orb_generates_call_signal_on_breakout() -> None:
    strategy = OrbStrategy()
    strategy.set_orb(orb_high=22000, orb_low=21900)
    signal = strategy.on_tick(spot_price=22020, qty=50)
    assert signal is not None
    assert signal.option_type == 'CE'


def test_orb_no_signal_inside_range() -> None:
    strategy = OrbStrategy()
    strategy.set_orb(orb_high=22000, orb_low=21900)
    signal = strategy.on_tick(spot_price=21950, qty=50)
    assert signal is None

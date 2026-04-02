from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from engine.execution_engine import ExecutionEngine
from risk.risk_manager import RiskManager
from services.option_chain_service import OptionChainService
from services.state_manager import RuntimeState, StateManager
from strategies.orb_strategy import TradeSignal


class FakeBroker:
    def __init__(self) -> None:
        self.calls = 0

    async def place_order(self, body: dict):
        self.calls += 1
        return type('R', (), {'ok': True, 'payload': {'orderStatus': 'COMPLETE'}, 'error': ''})


class FakeOptionService(OptionChainService):
    def __init__(self):
        pass

    def build_symbol(self, strike: int, option_type: str):
        return f'NIFTY10APR2622000{option_type}'


async def _run_test(tmp_path: Path) -> FakeBroker:
    broker = FakeBroker()
    queue: asyncio.Queue = asyncio.Queue()
    state = RuntimeState()
    state_manager = StateManager(str(tmp_path / 'runtime_state.json'))

    engine = ExecutionEngine(
        broker=broker,
        option_chain_service=FakeOptionService(),
        risk_manager=RiskManager(),
        state_manager=state_manager,
        signal_queue=queue,
        state=state,
    )

    task = asyncio.create_task(engine.run())
    signal = TradeSignal(action='BUY', reason='orb_breakout_up', strike=22000, option_type='CE', qty=50)
    await queue.put(signal)
    await queue.put(signal)
    await asyncio.sleep(0.3)
    engine.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return broker


def test_execution_prevents_duplicate_orders(tmp_path: Path) -> None:
    broker = asyncio.run(_run_test(tmp_path))
    assert broker.calls == 1

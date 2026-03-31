from __future__ import annotations

from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from core.cache import TTLCache  # noqa: E402
from engine.order_manager import OrderManager  # noqa: E402
from main import app  # noqa: E402
from models import EngineState  # noqa: E402
from risk.risk_manager import RiskManager  # noqa: E402
from services.market_data_service import MarketDataService  # noqa: E402
from strategies.orb_strategy import OrbStrategy  # noqa: E402
from strategies.strategy_manager import StrategyManager  # noqa: E402


def test_api_connectivity_health() -> None:
    client = TestClient(app)
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


def test_strategy_signal_contract() -> None:
    strategy = OrbStrategy()
    manager = StrategyManager([strategy])
    market_data = {
        'spot_price': 22100,
        'orb_high': 22050,
        'orb_low': 21950,
        'option_chain_bias': 'BULLISH',
        'option_chain': [],
        'candles': [{'close': 22000 + i} for i in range(20)],
    }
    signal = manager.choose(market_data)
    for key in ('signal', 'reason', 'entry_price', 'stop_loss', 'target_price'):
        assert key in signal


def test_risk_controls_daily_loss_halts_trading() -> None:
    state = EngineState(realized_pnl=-3000.0)
    status = RiskManager().circuit_breaker(state, broker_ok=True, api_ok=True)
    assert status == 'DAILY_LOSS_LIMIT'


def test_order_execution_paper_mode_and_pnl() -> None:
    manager = OrderManager()
    payload = {
        'exchange': 'NFO',
        'symbolName': 'NIFTY',
        'expiryDate': '2026-03-26',
        'strikePrice': '22100',
        'optionType': 'CE',
        'transactionType': 'BUY',
        'orderType': 'MKT',
        'productType': 'MIS',
        'quantity': 50,
        'price': 100,
    }
    import asyncio

    order = asyncio.run(manager.place_order(payload, mode='PAPER'))
    assert order['status'] == 'Success'
    pnl = manager.update_pnl(order['order_id'], latest_price=105)
    assert pnl == 250


def test_dashboard_data_market_service_tick_buffer() -> None:
    service = MarketDataService(TTLCache())
    service.add_tick(22100)
    ticks = service.get_recent_ticks(limit=1)
    assert len(ticks) == 1

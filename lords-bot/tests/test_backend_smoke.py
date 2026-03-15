from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from brokers import samco_client as samco_module  # noqa: E402
from brokers.samco_client import SamcoClient  # noqa: E402
from core.cache import TTLCache  # noqa: E402
from engine.candle_builder import CandleBuilder  # noqa: E402
from engine.order_manager import OrderManager  # noqa: E402
from engine.scheduler import Scheduler  # noqa: E402
from main import app  # noqa: E402
from models import EngineState  # noqa: E402
from risk.risk_manager import RiskManager  # noqa: E402
from services.option_chain_service import OptionChainService  # noqa: E402
from strategies.orb_strategy import OrbStrategy  # noqa: E402


class FakeBridge:
    EXCHANGE_NFO = 'NFO'

    def __init__(self) -> None:
        self.login_calls = 0
        self.session_token = ''

    def set_session_token(self, sessionToken: str) -> None:  # noqa: N803
        self.session_token = sessionToken

    def login(self, body: dict) -> dict:
        self.login_calls += 1
        return {'status': 'Success', 'sessionToken': 'token-123'}

    def index_quote(self, **kwargs) -> dict:
        return {'status': 'Success', 'indexDetails': [{'spotPrice': '22100.5'}]}

    def get_option_chain(self, **kwargs):
        return {
            'status': 'Success',
            'optionDetails': [
                {'strikePrice': '22100', 'optionType': 'CE', 'openInterest': '100', 'lastTradedPrice': '120'},
                {'strikePrice': '22100', 'optionType': 'PE', 'openInterest': '200', 'lastTradedPrice': '98'},
            ],
        }

    def user_details(self):
        return {'status': 'Success', 'data': {}}

    def get_limits(self):
        return {'status': 'Success', 'data': {}}

    def place_order(self, body: dict):
        return {'status': 'Success', 'order_id': 'O123'}

    def get_order_status(self, order_id: str):
        return {'status': 'Success', 'order_status': 'COMPLETE'}


def test_expiry_conversion() -> None:
    assert SamcoClient.to_expiry_code('2026-03-26') == '26MAR26'
    assert SamcoClient.to_expiry_api_date('26MAR2026') == '2026-03-26'


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get('/health')
    assert response.status_code == 200


def test_login_happens_once(monkeypatch) -> None:
    monkeypatch.setattr(samco_module, 'StocknoteAPIPythonBridge', FakeBridge)
    client = SamcoClient()
    assert client.login() is True
    assert client.login() is True
    assert isinstance(client.samco, FakeBridge)
    assert client.samco.login_calls == 1


def test_option_chain_normalization(monkeypatch) -> None:
    monkeypatch.setattr(samco_module, 'StocknoteAPIPythonBridge', FakeBridge)
    service = OptionChainService(TTLCache())

    async def _run() -> list[dict]:
        fake_client = SamcoClient()
        monkeypatch.setattr('services.option_chain_service.samco_client', fake_client)
        return await service.get_option_chain('NIFTY', '2026-03-26', strike_price='22100')

    chain = asyncio.run(_run())
    assert chain[0]['strike_price'] == 22100.0
    assert chain[0]['call_oi'] == 100.0


def test_scheduler_enforces_minimum_interval() -> None:
    scheduler = Scheduler()
    assert scheduler.interval_seconds >= 5


def test_candle_builder_opening_range_uses_915_to_945_window() -> None:
    builder = CandleBuilder()
    ticks = [
        {'timestamp': '2026-03-26T09:15:10+05:30', 'price': 100},
        {'timestamp': '2026-03-26T09:20:01+05:30', 'price': 99},
        {'timestamp': '2026-03-26T09:25:01+05:30', 'price': 105},
        {'timestamp': '2026-03-26T09:30:01+05:30', 'price': 101},
        {'timestamp': '2026-03-26T09:35:01+05:30', 'price': 98},
        {'timestamp': '2026-03-26T09:40:01+05:30', 'price': 103},
        {'timestamp': '2026-03-26T09:45:01+05:30', 'price': 400},
    ]
    candles = builder.build_5min_candles(ticks)
    orb = builder.opening_range(candles)
    assert orb['high'] == 105.0
    assert orb['low'] == 98.0


def test_orb_strategy_rule_gatekeeping() -> None:
    strategy = OrbStrategy()
    candles = [{'close': 100 + i} for i in range(20)]
    signal = strategy.generate(110, 105, 95, 'BULLISH', candles)
    assert signal.signal == 'BUY'
    assert signal.option_side == 'CALL'


def test_risk_manager_blocks_on_consecutive_losses() -> None:
    state = EngineState(consecutive_losses=3)
    decision = RiskManager().pre_trade_check(state, capital=100000, entry=100.0, stop=95.0)
    assert decision.allowed is False


def test_order_manager_payload_validation() -> None:
    valid, reason = OrderManager().validate_payload({'exchange': 'NFO'})
    assert valid is False
    assert reason.startswith('missing_fields')


def test_scheduler_market_hours_guard() -> None:
    scheduler = Scheduler()
    dt = datetime.fromisoformat('2026-03-26T08:00:00+05:30')
    assert scheduler._in_market_hours(dt) is False


def test_candle_builder_opening_range_allows_partial_window_after_945() -> None:
    builder = CandleBuilder()
    ticks = [
        {'timestamp': '2026-03-26T09:20:01+05:30', 'price': 100},
        {'timestamp': '2026-03-26T09:30:01+05:30', 'price': 104},
        {'timestamp': '2026-03-26T09:44:59+05:30', 'price': 98},
        {'timestamp': '2026-03-26T09:50:01+05:30', 'price': 101},
    ]
    candles = builder.build_5min_candles(ticks)
    orb = builder.opening_range(candles)
    assert orb['high'] == 104.0
    assert orb['low'] == 98.0


def test_option_chain_service_accepts_string_payload(monkeypatch) -> None:
    monkeypatch.setattr(samco_module, 'StocknoteAPIPythonBridge', FakeBridge)
    service = OptionChainService(TTLCache())

    async def _fake_get_option_chain(symbol: str, expiry: str, strike_price: str | None = None) -> str:
        return '{"optionDetails":[{"strikePrice":"22100","optionType":"CE","openInterest":"150","lastTradedPrice":"110"}]}'

    async def _run() -> list[dict]:
        monkeypatch.setattr('services.option_chain_service.samco_client.get_option_chain', _fake_get_option_chain)
        return await service.get_option_chain('NIFTY', '2026-03-26')

    chain = asyncio.run(_run())
    assert chain[0]['call_oi'] == 150.0


def test_market_data_service_accepts_string_payload(monkeypatch) -> None:
    from services.market_data_service import MarketDataService

    async def _fake_index_quote(index_name: str) -> str:
        return '{"indexDetails":[{"spotPrice":"22456.5"}]}'

    service = MarketDataService(TTLCache())

    async def _run() -> float:
        monkeypatch.setattr('services.market_data_service.samco_client.index_quote', _fake_index_quote)
        return await service.get_nifty_spot()

    spot = asyncio.run(_run())
    assert spot == 22456.5

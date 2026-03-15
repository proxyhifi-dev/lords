from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from brokers import samco_client as samco_module  # noqa: E402
from brokers.samco_client import SamcoClient  # noqa: E402
from core.cache import TTLCache  # noqa: E402
from engine.candle_builder import CandleBuilder  # noqa: E402
from engine.scheduler import Scheduler  # noqa: E402
from main import app  # noqa: E402
from models import EngineState  # noqa: E402
from risk.risk_manager import RiskManager  # noqa: E402
from services.option_chain_service import OptionChainService  # noqa: E402


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

    def index_quote(self, index_name: str) -> dict:
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


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get('/health')
    assert response.status_code == 200
    payload = response.json()
    assert payload['status'] == 'ok'
    assert 'scheduler_running' in payload
    assert payload['interval_seconds'] >= 5


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
        return await service.get_option_chain('NIFTY', '2026-03-26')

    chain = asyncio.run(_run())
    assert chain[0]['strike_price'] == 22100.0
    assert chain[0]['call_oi'] == 100.0
    assert chain[0]['put_oi'] == 200.0


def test_scheduler_enforces_minimum_interval() -> None:
    scheduler = Scheduler()
    assert scheduler.interval_seconds >= 5


def test_candle_builder_opening_range_uses_first_six_candles() -> None:
    builder = CandleBuilder()
    ticks = [
        {'timestamp': '2026-03-26T09:15:10', 'price': 100},
        {'timestamp': '2026-03-26T09:16:00', 'price': 102},
        {'timestamp': '2026-03-26T09:20:01', 'price': 99},
        {'timestamp': '2026-03-26T09:25:01', 'price': 105},
        {'timestamp': '2026-03-26T09:30:01', 'price': 101},
        {'timestamp': '2026-03-26T09:35:01', 'price': 98},
        {'timestamp': '2026-03-26T09:40:01', 'price': 103},
    ]
    candles = builder.build_5min_candles(ticks)
    orb = builder.opening_range(candles)
    assert orb['high'] == 105.0
    assert orb['low'] == 98.0


def test_option_chain_atm_contract_selection(monkeypatch) -> None:
    monkeypatch.setattr(samco_module, 'StocknoteAPIPythonBridge', FakeBridge)
    service = OptionChainService(TTLCache())
    chain = [
        {'strike_price': 22050.0, 'call_ltp': 120.0, 'put_ltp': 100.0},
        {'strike_price': 22100.0, 'call_ltp': 100.0, 'put_ltp': 120.0},
        {'strike_price': 22150.0, 'call_ltp': 80.0, 'put_ltp': 140.0},
    ]
    contract = service.pick_option_contract(chain, 22112.0, 'CALL', 'NIFTY', '2026-03-26')
    assert contract['strike'] == 22100.0
    assert contract['premium'] == 100.0
    assert contract['option_symbol'] == 'NIFTY26MAR2622100CE'


def test_risk_manager_blocks_invalid_stop() -> None:
    decision = RiskManager().pre_trade_check(EngineState(), capital=100000, entry=100.0, stop=0.0)
    assert decision.allowed is False

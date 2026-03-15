from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from brokers import samco_client as samco_module  # noqa: E402
from brokers.samco_client import SamcoClient  # noqa: E402
from core.cache import TTLCache  # noqa: E402
from engine.scheduler import Scheduler  # noqa: E402
from main import app  # noqa: E402
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
    assert SamcoClient.to_expiry_code('2026-03-26') == '26MAR2026'


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

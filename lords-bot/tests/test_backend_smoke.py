from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from main import app  # noqa: E402
from brokers.samco_client import SamcoClient  # noqa: E402


def test_expiry_conversion() -> None:
    assert SamcoClient.to_expiry_code('2026-03-26') == '26MAR2026'


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get('/health')
    assert response.status_code == 200
    payload = response.json()
    assert payload['status'] == 'ok'
    assert 'scheduler_running' in payload
    assert 'interval_seconds' in payload

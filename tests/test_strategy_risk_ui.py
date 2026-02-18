import asyncio
import datetime as dt

from fastapi.testclient import TestClient

from lords_bot.app.risk_engine import RiskEngine
from lords_bot.strategies.orb_strategy import ORBStrategy
from lords_bot.ui import server as ui_server


class StubClient:
    def __init__(self):
        self.paused = False

    def is_trading_paused(self):
        return self.paused

    @property
    def trading_pause_remaining_seconds(self):
        return 0

    async def request(self, method, endpoint, **kwargs):
        if endpoint == "/quotes":
            return {"s": "ok", "d": [{"v": {"lp": 25000}}]}
        if endpoint == "/positions":
            return {"positions": []}
        return {"s": "ok"}


class StubOrderService:
    async def fetch_ltp(self, symbol):
        return 100.0

    async def place_order(self, payload):
        return {"id": "1"}

    async def confirm_fill_price(self, symbol, order_response):
        return 100.0

    def build_sl_target(self, entry_price, sl, target):
        return (90.0, 120.0)


class DummyTemplates:
    def __init__(self, *args, **kwargs):
        pass

    def TemplateResponse(self, template_name, context):
        return context


def test_orb_builder_from_ticks():
    s = ORBStrategy(StubClient())
    s.tick_state.samples = [25000, 25010]
    s.tick_state.high = 25010
    s.tick_state.low = 25000
    s.range_date = dt.date.today()
    s.range_high = 25010
    s.range_low = 25000
    s.tick_state.last_price = 25020
    out = asyncio.run(s.check_breakout())
    assert out and out["direction"] == "CALL"


def test_risk_daily_loss_limit_blocks():
    r = RiskEngine(StubClient(), StubOrderService(), "PAPER")
    r.last_trade_date = dt.date.today()
    r.daily_realized_pnl = -999999
    ok, reason = r.can_trade_now()
    assert ok is False
    assert reason == "max_daily_loss_hit"


def test_ui_monitor_safe_payload(monkeypatch):
    monkeypatch.setattr(ui_server, "Jinja2Templates", DummyTemplates)
    app = ui_server.create_ui_app(StubClient(), StubOrderService(), "PAPER")
    client = TestClient(app)
    r = client.get("/monitor")
    assert r.status_code == 200
    body = r.json()
    assert "capital" in body
    assert "risk_status" in body


def test_scan_safe_on_error(monkeypatch):
    monkeypatch.setattr(ui_server, "Jinja2Templates", DummyTemplates)
    app = ui_server.create_ui_app(StubClient(), StubOrderService(), "PAPER")

    async def bad_breakout(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(ORBStrategy, "check_breakout", bad_breakout)
    client = TestClient(app)
    r = client.post("/scan")
    assert r.status_code == 200
    assert r.json()["status"] == "error"

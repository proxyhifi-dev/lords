from backend.app.storage.trade_store import TradeStore
from backend.app.broker.samco_client import SamcoClient
from backend.app.engine.order_execution import OrderExecutionSequence

import asyncio


def test_trade_store_accepts_float_charges(tmp_path):
    f = tmp_path / 'trades.csv'
    store = TradeStore(str(f))
    store.append_trade({"symbol": "NIFTY", "pnl": 10.0, "charges": 12.5}, daily_pnl=10.0)
    rows = store.get_all_trades()
    assert rows[-1]["total_charges"] in ("12.5", "12.50")


def test_samco_healthcheck_bool_on_exception(monkeypatch):
    c = SamcoClient()
    async def bad(_):
        raise RuntimeError('x')
    monkeypatch.setattr(c, 'get_index_quote', bad)
    out = asyncio.run(c.healthcheck())
    assert out is False


def test_short_put_uses_short_put_premium():
    class B: pass
    class S: order_qty=1; ic_margin_required=1
    logs=[]
    class L:
        def info(self,*a,**k): logs.append(str(a[0]) if a else '')
        def error(self,*a,**k): pass
    seq = OrderExecutionSequence(B(), S(), L())

    calls=[]
    async def fake(**kwargs):
        calls.append(kwargs)
        return {'success': True, 'order_id': '1', 'filled_price': kwargs.get('price') or 0}
    seq._place_order_with_retry = fake

    asyncio.run(seq.enter_iron_condor_sequence(
        {'short_call':1,'long_call':2,'short_put':3,'long_put':4},
        {'short_call':10,'long_call':11,'short_put':12,'long_put':13}
    ))
    assert calls[3]['price'] == 12

"""Stop-loss orders must use SmartAPI's field names (replica-test S2, 2026-10-04).

The Quick-order form passed "SL" / "SL-M" straight through as `ordertype`
with variety NORMAL and put the trigger in `stoploss` (a ROBO/bracket field),
so every stop-loss order went out malformed. SmartAPI wants variety STOPLOSS,
ordertype STOPLOSS_LIMIT / STOPLOSS_MARKET, and the trigger in `triggerprice`.
No network: the HTTP post is captured.
"""
import pytest

import data.angel_fetcher as A


@pytest.fixture
def sent(monkeypatch):
    bodies = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"status": True, "data": {"orderid": "OID1"}}

    def _post(url, json=None, **_kw):
        bodies.append(json)
        return _Resp()

    monkeypatch.setattr(A, "_get_session", lambda: {"jwt": "j", "api_key": "k"})
    monkeypatch.setattr(A, "_get_token", lambda sym, s: "2885")
    monkeypatch.setattr(A._http, "post", _post)
    return bodies


def test_sl_limit_order_fields(sent):
    out = A.place_order("RELIANCE", 1, "SELL", order_type="SL", price=1140.0, trigger_price=1145.0)
    body = sent[-1]
    assert out["status"] == "placed"
    assert body["variety"] == "STOPLOSS" and body["ordertype"] == "STOPLOSS_LIMIT"
    assert body["triggerprice"] == "1145.0" and body["price"] == "1140.0"
    assert body["stoploss"] == "0"


def test_sl_market_order_fields(sent):
    A.place_order("RELIANCE", 1, "SELL", order_type="SL-M", trigger_price=1145.0)
    body = sent[-1]
    assert body["variety"] == "STOPLOSS" and body["ordertype"] == "STOPLOSS_MARKET"
    assert body["triggerprice"] == "1145.0" and body["price"] == "0"


@pytest.mark.parametrize("otype", ["MARKET", "LIMIT"])
def test_plain_orders_unchanged(sent, otype):
    A.place_order("RELIANCE", 1, "BUY", order_type=otype, price=1150.0 if otype == "LIMIT" else 0)
    body = sent[-1]
    assert body["variety"] == "NORMAL" and body["ordertype"] == otype
    assert body["triggerprice"] == "0"


@pytest.mark.parametrize("kw,msg", [
    ({"order_type": "SL", "price": 1140.0, "trigger_price": 0.0}, "trigger"),
    ({"order_type": "SL-M", "trigger_price": 0.0}, "trigger"),
    ({"order_type": "SL", "price": 0.0, "trigger_price": 1145.0}, "limit price"),
    ({"order_type": "LIMIT", "price": 0.0}, "limit price"),
    # SELL stop-limit: trigger must sit at or above the limit price
    ({"order_type": "SL", "price": 1150.0, "trigger_price": 1145.0}, "SELL stop-loss"),
])
def test_invalid_orders_rejected_before_sending(sent, kw, msg):
    out = A.place_order("RELIANCE", 1, "SELL", **kw)
    assert out["status"] == "invalid" and msg in out["message"]
    assert sent == []


def test_buy_stop_limit_trigger_must_not_exceed_limit(sent):
    out = A.place_order("RELIANCE", 1, "BUY", order_type="SL", price=1150.0, trigger_price=1155.0)
    assert out["status"] == "invalid" and "BUY stop-loss" in out["message"]


def test_modify_order_uses_same_mapping(sent):
    A.modify_order("OID1", "RELIANCE", 1, order_type="SL-M", trigger_price=1145.0)
    body = sent[-1]
    assert body["variety"] == "STOPLOSS" and body["ordertype"] == "STOPLOSS_MARKET"
    assert body["triggerprice"] == "1145.0" and body["stoploss"] == "0"

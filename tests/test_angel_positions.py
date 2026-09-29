"""get_positions parses SmartAPI's flat-list getPosition response (equity + F&O)."""
from data import angel_fetcher as af


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200
        self.text = "x"

    def json(self):
        return self._p


def _patch(monkeypatch, rows):
    monkeypatch.setattr(af, "_get_session", lambda: {"jwt": "j", "api_key": "k"})
    monkeypatch.setattr(af, "_safe_parse_json_response", lambda r: r.json())
    monkeypatch.setattr(af._http, "get", lambda *a, **k: _Resp({"status": True, "data": rows}))


def test_flat_list_splits_open_and_closed(monkeypatch):
    _patch(monkeypatch, [
        {"tradingsymbol": "NIFTY30SEP2624500CE", "exchange": "NFO", "producttype": "CARRYFORWARD",
         "netqty": "-75", "netprice": "120.5", "ltp": "100.0", "pnl": "1537.5",
         "expirydate": "30SEP2026", "strikeprice": "24500", "optiontype": "CE", "lotsize": "75"},
        {"tradingsymbol": "MRPL-EQ", "exchange": "NSE", "producttype": "INTRADAY",
         "netqty": "0", "netprice": "0", "ltp": "171.65", "pnl": "-47"},
    ])
    out = af.get_positions()
    assert len(out["net"]) == 1 and out["day"] == out["net"]
    fo = out["net"][0]
    assert fo["segment"] == "F&O" and fo["side"] == "SHORT" and fo["pnl"] == 1537.5
    assert fo["lot_size"] == 75 and fo["expiry"] == "30SEP2026"
    assert out["closed"][0]["symbol"] == "MRPL" and out["closed"][0]["pnl"] == -47.0


def test_empty_data(monkeypatch):
    _patch(monkeypatch, None)
    assert af.get_positions() == {"day": [], "net": [], "closed": []}

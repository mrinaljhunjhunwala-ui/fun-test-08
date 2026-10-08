"""FII/DII source chain + missing-data handling (audit #28). Network mocked."""
import pandas as pd

import analysis.fii_dii as fd
import analysis.score as sc

_PAYLOAD = [
    {"buyValue": "22987.97", "category": "DII", "date": "08-Oct-2026",
     "netValue": "10703.11", "sellValue": "12284.86"},
    {"buyValue": "15725.45", "category": "FII/FPI", "date": "08-Oct-2026",
     "netValue": "-12943.58", "sellValue": "28669.03"},
]


def test_parse_nse_payload_and_drop_half_rows():
    rows = fd._parse_nse_payload(_PAYLOAD)
    assert rows == [{"date": "2026-10-08", "dii_buy": 22987.97, "dii_sell": 12284.86,
                     "dii_net": 10703.11, "fii_buy": 15725.45, "fii_sell": 28669.03,
                     "fii_net": -12943.58}]
    assert fd._parse_nse_payload(_PAYLOAD[:1]) == []        # FII leg missing
    assert fd._parse_nse_payload({"error": "x"}) == []


def _wire(monkeypatch, primary, fallback, history=None):
    written = {}
    monkeypatch.setattr(fd, "ensure_schema", lambda: None)
    monkeypatch.setattr(fd, "load_history",
                        lambda days=90: history if history is not None else pd.DataFrame())
    monkeypatch.setattr(fd, "_fetch_nse", lambda: primary)
    monkeypatch.setattr(fd, "_fetch_nse_exchange_only", lambda: fallback)
    monkeypatch.setattr(fd, "_persist",
                        lambda rows, source: written.update(rows=rows, source=source) or len(rows))
    return written


def test_primary_used_when_available(monkeypatch):
    w = _wire(monkeypatch, fd._parse_nse_payload(_PAYLOAD), [])
    out = fd.refresh(force=True)
    assert out["source"] == "nse" and w["source"] == "nse"


def test_falls_back_to_exchange_only(monkeypatch):
    w = _wire(monkeypatch, [], fd._parse_nse_payload(_PAYLOAD))
    out = fd.refresh(force=True)
    assert out["source"] == "nse_exchange_only" and out["rows_written"] == 1
    assert w["source"] == "nse_exchange_only"


def test_fallback_never_overwrites_stored_date(monkeypatch):
    hist = pd.DataFrame({"date": ["2026-10-08"], "fii_net": [-1.0], "dii_net": [1.0]})
    w = _wire(monkeypatch, [], fd._parse_nse_payload(_PAYLOAD), history=hist)
    out = fd.refresh(force=True)
    assert out["source"] == "cache" and "rows" not in w


def test_both_down_reports_unavailable(monkeypatch):
    _wire(monkeypatch, [], [])
    assert fd.refresh(force=True)["source"] == "unavailable"


def test_nse_get_json_retries_after_prime(monkeypatch):
    calls = []

    class _R:
        def __init__(self, code):
            self.status_code = code

        def json(self):
            return _PAYLOAD

    class _S:
        headers: dict = {}

        def get(self, url, timeout=None):
            calls.append(url)
            if url == fd._NSE_HOME:
                raise fd.requests.ConnectionError("prime blocked")
            return _R(403 if len(calls) == 1 else 200)

    monkeypatch.setattr(fd.requests, "Session", _S)
    assert fd._nse_get_json(fd._NSE_REACT) == _PAYLOAD
    assert calls == [fd._NSE_REACT, fd._NSE_HOME, fd._NSE_REACT]


def _flows(monkeypatch, df):
    monkeypatch.setattr(fd, "load_history", lambda days=5: df)
    monkeypatch.setattr(sc, "_expected_last_session", lambda: pd.Timestamp("2026-10-08"))
    sc._FLOWS_CACHE.clear()
    try:
        return sc._get_flows_cached()
    finally:
        sc._FLOWS_CACHE.clear()


def test_nan_rows_are_missing_not_zero(monkeypatch):
    df = pd.DataFrame({"date": ["2026-10-08", "2026-10-07", "2026-10-06"],
                       "fii_net": [-100.0, None, -50.0], "dii_net": [10.0, 20.0, None]})
    assert _flows(monkeypatch, df) is None     # only 1 complete row -> flows missing


def test_complete_rows_sum_unchanged(monkeypatch):
    df = pd.DataFrame({"date": ["2026-10-08", "2026-10-07", "2026-10-06"],
                       "fii_net": [-100.0, -10.0, -50.0], "dii_net": [10.0, 20.0, 30.0]})
    assert _flows(monkeypatch, df) == {"fii_5d": -160.0, "dii_5d": 60.0, "n_days": 3}


def test_missing_flows_is_flagged_and_neutral():
    total, pts = sc._score_sentiment({"regime": "normal"}, None, flows_info=None)
    assert pts["flows_available"] is False
    assert total == 8.0          # vix 6 + unranked-sector mid 2: no flow penalty or bonus
    total_f, pts_f = sc._score_sentiment({"regime": "normal"}, None,
                                         flows_info={"fii_5d": -1.0, "dii_5d": 1.0})
    assert pts_f["flows_available"] is True and total_f == 5.0 + 1.5 + 1.5

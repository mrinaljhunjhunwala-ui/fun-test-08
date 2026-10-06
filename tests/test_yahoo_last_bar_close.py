"""Yahoo leaves the latest session's close null (6 Oct 2026, 12h after the
close) and carries the price only in meta.regularMarketPrice. The parser used
to drop that bar, so every daily history ended a session early and the ledger
backfill updated 0 of 207 rows. Offline."""
import datetime as dt

from data.fetcher import _parse_yahoo_chart


def _ts(d, h=3, m=45):
    return int(dt.datetime(*d, h, m, tzinfo=dt.timezone.utc).timestamp())


def _payload(last_close, meta_px=711.45, meta_day=(2026, 10, 6)):
    rows = [((2026, 10, 1), 708.4, 721.3, 707.1, 721.2, 37_389_857),
            ((2026, 10, 5), 731.95, 734.2, 701.25, 704.8, 66_056_690),
            ((2026, 10, 6), 705.6, 709.0, 701.0, last_close, 26_550_844)]
    return {"chart": {"result": [{
        "meta": {"regularMarketPrice": meta_px, "regularMarketTime": _ts(meta_day, 9, 44)},
        "timestamp": [_ts(r[0]) for r in rows],
        "indicators": {"quote": [{k: [r[i] for r in rows] for i, k in
                                  enumerate(("open", "high", "low", "close", "volume"), 1)}]},
    }], "error": None}}


def test_null_last_close_is_filled_from_meta():
    df = _parse_yahoo_chart(_payload(None), "HDFCBANK.NS", "1d")
    assert df.index[-1].date() == dt.date(2026, 10, 6)
    last = df.iloc[-1]
    assert last["Close"] == 711.45
    assert last["High"] == 711.45 and last["Low"] == 701.0   # range widened to hold the close
    assert last["Open"] == 705.6


def test_real_close_is_left_alone():
    df = _parse_yahoo_chart(_payload(710.0), "HDFCBANK.NS", "1d")
    assert df.iloc[-1]["Close"] == 710.0


def test_meta_from_another_day_does_not_fill():
    df = _parse_yahoo_chart(_payload(None, meta_day=(2026, 10, 7)), "HDFCBANK.NS", "1d")
    assert df.index[-1].date() == dt.date(2026, 10, 5)       # unchanged old behaviour


def test_vix_uses_meta_for_a_null_last_close(monkeypatch):
    import io
    import json
    import utils.vix as V
    body = json.dumps({"chart": {"result": [{
        "meta": {"regularMarketPrice": 14.1},
        "indicators": {"quote": [{"close": [15.0, 14.78, None]}]},
    }], "error": None}}).encode()

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    import data.fetcher as F

    class _Opener:
        def open(self, *a, **k):
            return _R(body)
    monkeypatch.setattr(V, "_VIX_CACHE", None)
    monkeypatch.setattr(F, "_get_yf_crumb", lambda: (_Opener(), ""))
    out = V.get_india_vix_regime()
    assert out["vix"] == 14.1

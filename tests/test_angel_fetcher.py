import json
import types
import pytest
import requests
from unittest.mock import Mock

import data.angel_fetcher as af


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class FakeSession:
    def __init__(self):
        self.posts = []
        self.get_calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json))
        # login endpoint
        if 'loginByPassword' in url:
            return FakeResponse(200, {"status": True, "data": {"jwtToken": "J", "feedToken": "F"}})
        if 'searchScrip' in url:
            return FakeResponse(200, {"data": [{"tradingsymbol": json.get("searchscrip"), "symboltoken": "123", "instrumenttype": "EQ"}]})
        if 'getCandleData' in url:
            return FakeResponse(200, {"data": [["2026-07-01 09:15", 100, 101, 99, 100, 1000]]})
        if 'quote' in url:
            return FakeResponse(200, {"data": {"fetched": [{"ltp": 100, "close": 99, "tradeVolume": 1000}]}})
        return FakeResponse(404)

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append(url)
        return FakeResponse(200, {"dummy": True})


@pytest.fixture(autouse=True)
def patch_session(monkeypatch):
    # Replace the module-level _http Session with our fake
    fake = FakeSession()
    monkeypatch.setattr(af, "_http", fake)
    af._SESSION["jwt"] = None
    af._LOGIN_BREAKER_FAILS[0] = 0
    af._LOGIN_BREAKER_TRIPPED_AT[0] = 0.0
    # These tests exercise the real _get_session()/login flow (asserting
    # _SESSION["jwt"] gets populated), which gates on _get_credentials()
    # returning 4 non-empty values before it ever calls _http.post() for
    # login — previously only satisfied if ANGEL_API_KEY/CLIENT_ID/
    # PASSWORD/TOTP_SECRET happened to be set as real env vars/secrets, so
    # these tests silently no-op'd (session stayed None) wherever those
    # weren't configured, including this CI. Fake but valid-shaped
    # credentials so the login path always actually runs in any environment.
    monkeypatch.setattr(af, "_get_credentials", lambda: {
        "api_key": "TESTKEY", "client_id": "TESTCLIENT",
        "password": "TESTPASS", "totp_secret": "JBSWY3DPEHPK3PXP",
    })
    yield fake


def test_login_and_fetch_historical_success(patch_session):
    # ensure login works and fetch_historical returns DataFrame-like data.
    # This test exercises the real _get_session() -> pyotp.TOTP(...) code
    # path; pyotp is an optional live-broker dep (see requirements.txt
    # comment on the pyotp line). Skip cleanly when it isn't installed
    # rather than fail hard - CI's ubuntu image has occasionally installed
    # requirements.txt without pyotp landing, causing a spurious red.
    pytest.importorskip("pyotp",
                        reason="Angel One login flow needs pyotp; skipped when absent")
    df = af.fetch_historical("RELIANCE.NS", period="5d", interval="1d")
    assert df is not None
    # check that session jwt got set
    assert af._SESSION["jwt"] is not None


def test_get_full_quote_success(patch_session):
    # Same optional-dep guard as test_login_and_fetch_historical_success.
    pytest.importorskip("pyotp",
                        reason="Angel One login flow needs pyotp; skipped when absent")
    q = af.get_full_quote("RELIANCE.NS")
    assert q is not None
    assert q["price"] == 100


def test_get_batch_quotes_success(patch_session):
    res = af.get_batch_quotes(["RELIANCE.NS", "TCS.NS"])
    assert isinstance(res, dict)
    # both keys present (FakeSession returns token for any searchScrip)
    assert "RELIANCE.NS" in res and "TCS.NS" in res


def test_login_failure(monkeypatch):
    # simulate login returning status false
    class BadSession(FakeSession):
        def post(self, url, json=None, headers=None, timeout=None):
            if 'loginByPassword' in url:
                return FakeResponse(200, {"status": False, "message": "bad creds"})
            return super().post(url, json=json, headers=headers, timeout=timeout)

    monkeypatch.setattr(af, "_http", BadSession())
    af._SESSION["jwt"] = None
    # login should fail and fetch_historical should return None
    assert af.fetch_historical("RELIANCE.NS") is None


def test_malformed_json_from_searchscrip(monkeypatch):
    class MalformSession(FakeSession):
        def post(self, url, json=None, headers=None, timeout=None):
            if 'searchScrip' in url:
                return FakeResponse(200, Exception("Expecting value"))
            return super().post(url, json=json, headers=headers, timeout=timeout)

    monkeypatch.setattr(af, "_http", MalformSession())
    af._SESSION["jwt"] = None
    # Clear the token cache — without this, an earlier test in this file
    # (test_login_and_fetch_historical_success / test_get_full_quote_success
    # / test_get_batch_quotes_success) already resolved and cached a real
    # "RELIANCE" -> token entry with a 6h TTL, so _get_token() here would
    # return that cached token without ever calling the mocked (malformed)
    # searchScrip endpoint — the scenario this test claims to exercise.
    af._TOKEN_CACHE.clear()
    # Token lookup fails but should be handled and return None
    assert af.get_full_quote("RELIANCE.NS") is None


def test_get_funds_handles_null_data(monkeypatch):
    # Angel One's getRMS can legitimately respond {"status": true, "data": null}
    # (e.g. no margin data available yet). get_funds() must degrate to zeros,
    # not crash with AttributeError: 'NoneType' object has no attribute 'get'
    # — regression test for a missing-parens operator-precedence bug where
    # `x.get("data") if isinstance(x, dict) else None or {}` only applies the
    # `or {}` fallback to the `else` branch, not to a None "data" value.
    monkeypatch.setattr(af, "_get_session", lambda: {"jwt": "J", "api_key": "K"})

    class NullDataSession:
        def get(self, url, headers=None, timeout=None):
            return FakeResponse(200, {"status": True, "data": None})

    monkeypatch.setattr(af, "_http", NullDataSession())
    funds = af.get_funds()
    assert funds == {
        "available_cash": 0.0,
        "used_margin": 0.0,
        "total_margin": 0.0,
        "collateral": 0.0,
        "m2m": 0.0,
    }


def test_get_profile_handles_null_data(monkeypatch):
    # Same bug, same fix, in get_profile() — see test_get_funds_handles_null_data.
    monkeypatch.setattr(af, "_get_session", lambda: {"jwt": "J", "api_key": "K"})

    class NullDataSession:
        def get(self, url, headers=None, timeout=None):
            return FakeResponse(200, {"status": True, "data": None})

    monkeypatch.setattr(af, "_http", NullDataSession())
    profile = af.get_profile()
    assert profile == {
        "name": "",
        "client_id": "",
        "email": "",
        "mobile": "",
        "exchanges": [],
        "products": [],
        "broker": "Angel One",
    }


def test_searchscrip_rate_limit_trips_breaker(monkeypatch):
    # simulate repeated failures to trip the breaker
    class FailSession(FakeSession):
        def post(self, url, json=None, headers=None, timeout=None):
            if 'searchScrip' in url:
                return FakeResponse(500, {})
            return super().post(url, json=json, headers=headers, timeout=timeout)

    s = FailSession()
    monkeypatch.setattr(af, "_http", s)

    # Reset breaker state too — it's module-level global state like
    # _TOKEN_CACHE, so an earlier test's real API calls (e.g.
    # test_malformed_json_from_searchscrip) can leave it non-zero.
    af._BREAKER_FAILS[0] = 0
    af._BREAKER_TRIPPED_AT[0] = 0.0

    # call _get_token several times to record failures. Must clear
    # _TOKEN_CACHE on EVERY iteration, not just once before the loop:
    # _get_token() caches a failed lookup for _TOKEN_FAIL_TTL (300s), so
    # without this, only the 1st call ever reaches the mocked searchScrip
    # endpoint — the other 5 just replay the cached failure and never call
    # _breaker_record_failure() again, so the counter never reaches
    # _BREAKER_THRESHOLD and the assertion below fails.
    for _ in range(af._BREAKER_THRESHOLD + 1):
        af._TOKEN_CACHE.clear()
        _ = af._get_token("RELIANCE", {"jwt": "J", "api_key": "K"})
    assert af._breaker_tripped()


# ── FIX AO-403: candle-API HTTP errors feed the breaker ─────────────────────
class _CandleSession(FakeSession):
    """Candle endpoint answers `status` (and `data`); everything else as FakeSession."""
    def __init__(self, status=403, data=None):
        super().__init__()
        self.status, self.data, self.candle_calls = status, data, 0

    def post(self, url, json=None, headers=None, timeout=None):
        if "getCandleData" in url:
            self.candle_calls += 1
            return FakeResponse(self.status, {"data": self.data} if self.status < 400 else {})
        return super().post(url, json=json, headers=headers, timeout=timeout)


def _candle_setup(monkeypatch, sess):
    monkeypatch.setattr(af, "_http", sess)
    monkeypatch.setattr(af, "_get_session", lambda: {"jwt": "J", "api_key": "K"})
    monkeypatch.setattr(af, "_get_token", lambda sym, session: "123")
    monkeypatch.setattr(af, "_hist_rate_limit", lambda: None)
    af._BREAKER_FAILS[0] = 0
    af._BREAKER_TRIPPED_AT[0] = 0.0
    af._HIST_SENT.clear()


def test_candle_403s_trip_breaker_and_stop_calls(monkeypatch):
    sess = _CandleSession(status=403)
    _candle_setup(monkeypatch, sess)
    for i in range(af._BREAKER_THRESHOLD):
        assert af.fetch_historical(f"T{i}.NS", period="1y") is None
    assert af._breaker_tripped()
    calls = sess.candle_calls
    with pytest.raises(af.AngelBusy):              # tripped: quiet skip for the caller
        af.fetch_historical("MORE.NS", period="1y")
    assert sess.candle_calls == calls          # tripped: no further candle requests
    af._BREAKER_FAILS[0] = 0
    af._BREAKER_TRIPPED_AT[0] = 0.0


def test_empty_200_is_not_a_breaker_failure(monkeypatch):
    sess = _CandleSession(status=200, data=[])
    _candle_setup(monkeypatch, sess)
    for i in range(af._BREAKER_THRESHOLD + 2):
        assert af.fetch_historical(f"ILLQ{i}.NS", period="1y") is None
    assert not af._breaker_tripped()


def test_candle_success_resets_failures(monkeypatch):
    sess = _CandleSession(status=403)
    _candle_setup(monkeypatch, sess)
    for i in range(af._BREAKER_THRESHOLD - 1):
        af.fetch_historical(f"F{i}.NS", period="1y")
    sess.status, sess.data = 200, [["2026-09-25T00:00:00+05:30", 1, 2, 0.5, 1.5, 100]]
    df = af.fetch_historical("OK.NS", period="1y")
    assert df is not None and not df.empty
    assert af._BREAKER_FAILS[0] == 0


# ── FIX AO-BULK: bulk scans skip Angel instead of queueing behind it ─────────
_OK_CANDLES = [["2026-09-25T00:00:00+05:30", 1, 2, 0.5, 1.5, 100]]


def test_candle_fetch_busy_while_another_in_flight(monkeypatch):
    sess = _CandleSession(status=200, data=_OK_CANDLES)
    _candle_setup(monkeypatch, sess)
    assert af._HIST_INFLIGHT.acquire(blocking=False)   # simulate a fetch in flight
    try:
        with pytest.raises(af.AngelBusy):
            af.fetch_historical("X.NS", period="1y")
        assert sess.candle_calls == 0                   # never queued, never called
    finally:
        af._HIST_INFLIGHT.release()
    assert af.fetch_historical("X.NS", period="1y") is not None


def test_candle_budget_spent_raises_busy(monkeypatch):
    sess = _CandleSession(status=200, data=_OK_CANDLES)
    _candle_setup(monkeypatch, sess)
    monkeypatch.setattr(af, "_HIST_PER_MIN", 3)
    for i in range(3):
        assert af.fetch_historical(f"B{i}.NS", period="1y") is not None
    with pytest.raises(af.AngelBusy):
        af.fetch_historical("B3.NS", period="1y")
    # 2y needs two windows: must reserve both, and busy doesn't trip the breaker
    af._HIST_SENT.clear()
    monkeypatch.setattr(af, "_HIST_PER_MIN", 1)
    with pytest.raises(af.AngelBusy):
        af.fetch_historical("TWO.NS", period="2y")
    assert not af._breaker_tripped()
    af._HIST_SENT.clear()

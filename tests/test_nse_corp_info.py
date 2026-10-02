import requests
import pytest
import types

import data.nse_corp_info as nci


class FakeResponse:
    def __init__(self, status=200, json_data=None, content=b""):
        self.status_code = status
        self._json = json_data
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


def test_get_corp_info_success(monkeypatch):
    # Simulate homepage GET then API GET returning a dict
    def fake_get(self, url, headers=None, timeout=None):
        if url.endswith("/"):
            return FakeResponse(200, {"ok": True})
        # API path
        return FakeResponse(200, {"latest_announcements": [{"symbol": "RELIANCE"}]})

    monkeypatch.setattr(requests.Session, "get", fake_get, raising=True)
    # reset session singleton to ensure a fresh session is used
    nci._session_singleton = None

    res = nci.get_corp_info("RELIANCE.NS", use_cache=False)
    assert isinstance(res, dict)
    assert "latest_announcements" in res
    diag = nci.get_last_diagnostic("RELIANCE.NS")
    assert diag is not None and diag.get("ok") is True


def test_get_corp_info_blocked_403(monkeypatch):
    # Simulate homepage GET then API returning 403 so get_json retries then raises _NseFetchError
    def fake_get(self, url, headers=None, timeout=None):
        return FakeResponse(403, None)

    monkeypatch.setattr(requests.Session, "get", fake_get, raising=True)
    nci._session_singleton = None

    res = nci.get_corp_info("RELIANCE.NS", use_cache=False)
    assert res == {}
    diag = nci.get_last_diagnostic("RELIANCE.NS")
    assert diag is not None
    assert diag.get("ok") is False
    assert diag.get("status_code") == 403


def test_get_corp_info_malformed_json(monkeypatch):
    def fake_get(self, url, headers=None, timeout=None):
        # Return a JSON that's not a dict (e.g., a list)
        if url.endswith("/"):
            return FakeResponse(200, {"ok": True})
        return FakeResponse(200, [1, 2, 3])

    monkeypatch.setattr(requests.Session, "get", fake_get, raising=True)
    nci._session_singleton = None

    res = nci.get_corp_info("RELIANCE.NS", use_cache=False)
    assert res == {}
    diag = nci.get_last_diagnostic("RELIANCE.NS")
    assert diag is not None and diag.get("ok") is False


def test_get_corp_info_timeout(monkeypatch):
    def fake_get(self, url, headers=None, timeout=None):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(requests.Session, "get", fake_get, raising=True)
    nci._session_singleton = None

    res = nci.get_corp_info("RELIANCE.NS", use_cache=False)
    assert res == {}
    diag = nci.get_last_diagnostic("RELIANCE.NS")
    assert diag is not None and diag.get("ok") is False


def test_get_corp_info_hits_top_corp_info_endpoint(monkeypatch):
    """URL regression guard - FIX PROV-2026-09-06.

    NSE retired /api/corporate-info/{sym}; the correct current endpoint is
    /api/top-corp-info?symbol=X&market=cash. This test pins the URL so the
    fetcher can't silently drift back to the retired path (which was the
    exact silent failure the 2026-09-06 data-provenance audit caught).
    """
    urls_seen: list[str] = []

    def fake_get(self, url, headers=None, timeout=None):
        urls_seen.append(url)
        if url.endswith("/"):
            return FakeResponse(200, {"ok": True})
        return FakeResponse(200, {"latest_announcements": {"data": []}})

    monkeypatch.setattr(requests.Session, "get", fake_get, raising=True)
    nci._session_singleton = None

    nci.get_corp_info("RELIANCE.NS", use_cache=False)

    api_calls = [u for u in urls_seen if not u.endswith("/")]
    assert api_calls, "no API URL was called"
    api_url = api_calls[0]
    assert "/api/top-corp-info" in api_url, (
        f"expected /api/top-corp-info, got {api_url!r}"
    )
    assert "symbol=RELIANCE" in api_url
    assert "market=cash" in api_url
    assert "/api/corporate-info" not in api_url, (
        "retired /api/corporate-info path must not be used"
    )


# ── Audit 2026-10-03: timeouts were reported as "status 443" (the port) ──────
def test_timeout_has_no_status_code():
    import requests
    from data.nse_corp_info import _extract_status_code_from_exception as _sc
    exc = requests.ReadTimeout("HTTPSConnectionPool(host='www.nseindia.com', port=443): "
                               "Read timed out. (read timeout=20)")
    assert _sc(exc) is None
    assert _sc(Exception("connect to www.nseindia.com:443 refused")) is None
    assert _sc(Exception("403 Client Error: Forbidden for url")) == 403


def test_session_uses_a_browser_user_agent():
    # NSE's WAF tarpits the old custom "nse-smart-investor/1.0" agent.
    import data.nse_corp_info as m
    m._session_singleton = None
    try:
        ua = m._ensure_session().headers["User-Agent"]
    finally:
        m._session_singleton = None
    assert ua.startswith("Mozilla/5.0") and "Chrome/" in ua

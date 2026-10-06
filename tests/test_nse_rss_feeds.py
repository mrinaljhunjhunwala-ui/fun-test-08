import requests
import pytest

import data.nse_rss_feeds as rf


class FakeResponse:
    def __init__(self, status=200, content=b""):
        self.status_code = status
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


SAMPLE_FEED = b"""<?xml version='1.0'?><rss><channel><item><title>Related Party: RELIANCE</title><link>http://x</link><description>Desc</description><pubDate>Tue, 01 Jul 2026</pubDate></item></channel></rss>"""


def test_fetch_feed_success(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return FakeResponse(200, SAMPLE_FEED)

    monkeypatch.setattr(requests, "get", fake_get, raising=True)
    rf._feed_cache = rf._feed_cache.__class__(ttl_seconds=1, name="nse_rss_test")

    items = rf.fetch_feed("related_party_transactions", use_cache=False)
    assert isinstance(items, list)
    assert items and "Related Party" in items[0]["title"]
    diag = rf.get_last_diagnostic("related_party_transactions")
    assert diag and diag.get("ok") is True


def test_fetch_feed_parse_error(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return FakeResponse(200, b"<rss><broken></rss>")

    monkeypatch.setattr(requests, "get", fake_get, raising=True)
    rf._feed_cache = rf._feed_cache.__class__(ttl_seconds=1, name="nse_rss_test")

    items = rf.fetch_feed("related_party_transactions", use_cache=False)
    assert items == []
    diag = rf.get_last_diagnostic("related_party_transactions")
    assert diag and diag.get("ok") is False


def test_unknown_category_returns_empty():
    out = rf.fetch_feed("no_such_category")
    assert out == []


def test_get_items_for_company_filters(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return FakeResponse(200, SAMPLE_FEED)

    monkeypatch.setattr(requests, "get", fake_get, raising=True)
    rf._feed_cache = rf._feed_cache.__class__(ttl_seconds=1, name="nse_rss_test")

    matched = rf.get_items_for_company("related_party_transactions", "RELIANCE.NS")
    assert isinstance(matched, list)
    assert matched and "RELIANCE" in matched[0]["title"].upper()


# --- Audit 2026-10-07: empty / stale feeds must not read as "ok" ----------

EMPTY_FEED = b"<?xml version='1.0'?><rss><channel><title>x</title></channel></rss>"


def _feed_with_date(pub):
    return (b"<rss><channel><item><title>Insider: RELIANCE</title><link>http://x</link>"
            b"<description>d</description><pubDate>" + pub.encode() + b"</pubDate></item>"
            b"</channel></rss>")


def _fresh_cache():
    rf._feed_cache = rf._feed_cache.__class__(ttl_seconds=1, name="nse_rss_test")


def test_empty_feed_reports_empty_not_ok(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None:
                        FakeResponse(200, EMPTY_FEED))
    _fresh_cache()
    assert rf.fetch_feed("corporate_governance", use_cache=False) == []
    diag = rf.get_last_diagnostic("corporate_governance")
    assert diag["ok"] is False and diag["status"] == "empty"
    assert "0 items" in diag["reason"] and "ok (" not in diag["reason"]
    assert "corporate_governance" in rf.get_feed_gaps()


def test_insider_falls_back_to_alternate_filename(monkeypatch):
    import datetime as dt
    fresh = dt.datetime.now().strftime("%d-%b-%Y %H:%M")
    seen = []

    def fake_get(url, headers=None, timeout=None):
        seen.append(url)
        if url.endswith("/InsiderTrading.xml"):
            return FakeResponse(200, EMPTY_FEED)
        return FakeResponse(200, _feed_with_date(fresh))

    monkeypatch.setattr(requests, "get", fake_get)
    _fresh_cache()
    items = rf.fetch_feed("insider_trading", use_cache=False)
    assert items and seen[-1].endswith("/Insider_Trading.xml")
    diag = rf.get_last_diagnostic("insider_trading")
    assert diag["ok"] is True and diag["file"] == "Insider_Trading.xml"
    assert "insider_trading" not in rf.get_feed_gaps()


def test_stale_feed_returns_items_but_flags_stale(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None:
                        FakeResponse(200, _feed_with_date("02-May-2020 16:46")))
    _fresh_cache()
    items = rf.fetch_feed("insider_trading", use_cache=False)
    assert items
    diag = rf.get_last_diagnostic("insider_trading")
    assert diag["ok"] is False and diag["status"] == "stale"
    assert rf.get_feed_gaps()["insider_trading"].startswith("stale")


def test_empty_feed_is_not_cached(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return FakeResponse(200, EMPTY_FEED)

    monkeypatch.setattr(requests, "get", fake_get)
    _fresh_cache()
    rf.fetch_feed("sast_regulation_29")
    rf.fetch_feed("sast_regulation_29")
    assert len(calls) == 2

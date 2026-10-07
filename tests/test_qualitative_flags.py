"""
QF1 regression tests — qualitative flags (governance/regulatory/narrative
layer alongside the composite score). No network: all NSE payloads are
synthetic dicts; persistence uses an in-memory fake standing in for
trade_store.kv_get/kv_set.

Run:  py -m pytest tests/test_qualitative_flags.py -q
"""
import datetime as _dt
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from analysis.qualitative_flags import (              # noqa: E402
    FlagCategory, FlagSentiment, QualitativeFlag,
    build_auto_flags, load_flags, manual_flag,
    parse_announcement_flags, parse_corporate_action_flags,
    parse_news_flags, parse_rss_flags, parse_shareholding_flags,
    refresh_all_flags, save_flags, summarize_flags,
)
import data.news_feed as _news_feed_mod              # noqa: E402
import data.nse_rss_feeds as _nse_rss_feeds_mod       # noqa: E402


@pytest.fixture(autouse=True)
def _reset_module_level_caches():
    """nse_rss_feeds._feed_cache and news_feed._cache are process-global
    TTLCache singletons (12h / 6h TTL) with no reset hook. Any test in this
    file that calls fetch_feed()/fetch_news() with the default use_cache=True
    leaves state behind that can silently bypass a later test's requests.get
    mock (a cache HIT never reaches the mocked function). Clearing both
    before every test makes each test's mock authoritative regardless of
    execution order — cheap insurance against exactly the kind of flake this
    module's tests are meant to prevent."""
    _nse_rss_feeds_mod._feed_cache.clear()
    _news_feed_mod._cache.clear()
    yield
    _nse_rss_feeds_mod._feed_cache.clear()
    _news_feed_mod._cache.clear()


@pytest.fixture(autouse=True)
def _no_live_nse_rss(monkeypatch):
    """refresh_all_flags() fetches live NSE RSS when rss_items_by_category is
    None. Unmocked, a real filing for the test ticker leaked an extra flag
    into assertions (CI failure 2026-09-24). Default every test in this file
    to "no RSS items"; tests that care monkeypatch it themselves."""
    monkeypatch.setattr(_nse_rss_feeds_mod, "get_all_relevant_items",
                        lambda *a, **k: {})


# ── fake kv store (mirrors trade_store.kv_get/kv_set signatures exactly) ──
class _FakeKv:
    def __init__(self):
        self.store: dict[str, object] = {}

    def get(self, key, default=None, user_id="default"):
        return self.store.get((user_id, key), default)

    def set(self, key, value, user_id="default"):
        self.store[(user_id, key)] = value
        return True


# ── QualitativeFlag round-trip ─────────────────────────────────────────────

def test_flag_to_dict_and_back_roundtrips():
    f = manual_flag("ABDL.NS", FlagCategory.NARRATIVE, FlagSentiment.GREEN, "JV announced")
    restored = QualitativeFlag.from_dict(f.to_dict())
    assert restored.ticker == f.ticker
    assert restored.category == FlagCategory.NARRATIVE
    assert restored.sentiment == FlagSentiment.GREEN
    assert restored.headline == "JV announced"


def test_flag_expiry_inactive_after_date():
    f = manual_flag("ABDL.NS", FlagCategory.MACRO, FlagSentiment.AMBER,
                     "Budget sensitive", expiry="2020-01-01")
    assert f.is_active(as_of=__import__("datetime").date(2020, 1, 2)) is False


def test_flag_without_expiry_always_active():
    f = manual_flag("ABDL.NS", FlagCategory.NARRATIVE, FlagSentiment.GREEN, "JV")
    assert f.is_active() is True


# ── shareholding pattern parsing ────────────────────────────────────────────

def test_shareholding_pledge_increase_flags_red():
    corp_info = {
        "shareholdings_patterns": {
            "data": {
                "2026-06-30": [{"pr_and_prgrp": "45.0", "pledged_pct": "18.0"}],
                "2026-03-31": [{"pr_and_prgrp": "45.0", "pledged_pct": "12.0"}],
            }
        }
    }
    flags = parse_shareholding_flags("ABDL.NS", corp_info)
    pledge_flags = [f for f in flags if "pledge" in f.headline.lower()]
    assert len(pledge_flags) == 1
    assert pledge_flags[0].sentiment == FlagSentiment.RED
    assert pledge_flags[0].category == FlagCategory.GOVERNANCE


def test_shareholding_promoter_holding_decrease_flags_red():
    corp_info = {
        "shareholdings_patterns": {
            "data": {
                "2026-06-30": [{"pr_and_prgrp": "40.0"}],
                "2026-03-31": [{"pr_and_prgrp": "45.0"}],
            }
        }
    }
    flags = parse_shareholding_flags("ABDL.NS", corp_info)
    assert len(flags) == 1
    assert flags[0].sentiment == FlagSentiment.RED
    assert "Promoter holding" in flags[0].headline


def test_shareholding_no_relevant_fields_returns_empty():
    corp_info = {
        "shareholdings_patterns": {
            "data": {
                "2026-06-30": [{"public_val": "55.0"}],
                "2026-03-31": [{"public_val": "54.0"}],
            }
        }
    }
    assert parse_shareholding_flags("ABDL.NS", corp_info) == []


def test_shareholding_single_date_returns_empty():
    corp_info = {"shareholdings_patterns": {"data": {"2026-06-30": [{"pr_and_prgrp": "45.0"}]}}}
    assert parse_shareholding_flags("ABDL.NS", corp_info) == []


def test_shareholding_missing_key_returns_empty():
    assert parse_shareholding_flags("ABDL.NS", {}) == []


def test_shareholding_small_change_not_flagged():
    """Deltas under 0.5pp are noise, not signal — should not flag."""
    corp_info = {
        "shareholdings_patterns": {
            "data": {
                "2026-06-30": [{"pr_and_prgrp": "45.1"}],
                "2026-03-31": [{"pr_and_prgrp": "45.0"}],
            }
        }
    }
    assert parse_shareholding_flags("ABDL.NS", corp_info) == []


# ── corporate action parsing ────────────────────────────────────────────────

def test_corporate_action_buyback_flags_green():
    corp_info = {"corporate_actions": {"data": [
        {"symbol": "ABDL", "exdate": "2026-07-01", "purpose": "Buyback of Shares"}
    ]}}
    flags = parse_corporate_action_flags("ABDL.NS", corp_info)
    assert len(flags) == 1
    assert flags[0].sentiment == FlagSentiment.GREEN
    assert flags[0].category == FlagCategory.CORPORATE_ACTION


def test_corporate_action_unclassified_defaults_amber():
    corp_info = {"corporate_actions": {"data": [
        {"symbol": "ABDL", "exdate": "2026-07-01", "purpose": "Annual General Meeting"}
    ]}}
    flags = parse_corporate_action_flags("ABDL.NS", corp_info)
    assert flags[0].sentiment == FlagSentiment.AMBER


def test_corporate_action_empty_purpose_skipped():
    corp_info = {"corporate_actions": {"data": [{"symbol": "ABDL", "exdate": "2026-07-01", "purpose": ""}]}}
    assert parse_corporate_action_flags("ABDL.NS", corp_info) == []


# ── announcement parsing ────────────────────────────────────────────────────

def test_announcement_negative_keyword_flags_red():
    corp_info = {"latest_announcements": {"data": [
        {"symbol": "ABDL", "broadcastdate": "2026-07-01",
         "subject": "Resignation of Independent Director"}
    ]}}
    flags = parse_announcement_flags("ABDL.NS", corp_info)
    assert flags[0].sentiment == FlagSentiment.RED


def test_announcement_respects_max_items():
    corp_info = {"latest_announcements": {"data": [
        {"symbol": "ABDL", "broadcastdate": "2026-07-01", "subject": f"Update {i}"}
        for i in range(10)
    ]}}
    flags = parse_announcement_flags("ABDL.NS", corp_info, max_items=3)
    assert len(flags) == 3


# ── build_auto_flags orchestration ──────────────────────────────────────────

def test_build_auto_flags_empty_corp_info_returns_empty():
    assert build_auto_flags("ABDL.NS", {}) == []


def test_build_auto_flags_combines_all_sources():
    corp_info = {
        "shareholdings_patterns": {"data": {
            "2026-06-30": [{"pledged_pct": "18.0"}],
            "2026-03-31": [{"pledged_pct": "10.0"}],
        }},
        "corporate_actions": {"data": [
            {"symbol": "ABDL", "exdate": "2026-07-01", "purpose": "Buyback of Shares"}
        ]},
        "latest_announcements": {"data": [
            {"symbol": "ABDL", "broadcastdate": "2026-07-01", "subject": "Q1 results announced"}
        ]},
    }
    flags = build_auto_flags("ABDL.NS", corp_info)
    categories = {f.category for f in flags}
    assert FlagCategory.GOVERNANCE in categories
    assert FlagCategory.CORPORATE_ACTION in categories
    assert FlagCategory.ANNOUNCEMENT in categories


# ── persistence (fake kv, matching real trade_store signature) ─────────────

def test_save_and_load_flags_roundtrip():
    kv = _FakeKv()
    flags = [manual_flag("ABDL.NS", FlagCategory.NARRATIVE, FlagSentiment.GREEN, "JV")]
    assert save_flags("ABDL.NS", flags, kv.set) is True
    loaded = load_flags("ABDL.NS", kv.get)
    assert len(loaded) == 1
    assert loaded[0].headline == "JV"


def test_load_flags_missing_ticker_returns_empty():
    kv = _FakeKv()
    assert load_flags("NOPE.NS", kv.get) == []


def test_load_flags_filters_expired():
    kv = _FakeKv()
    expired = manual_flag("ABDL.NS", FlagCategory.MACRO, FlagSentiment.AMBER,
                           "Old budget flag", expiry="2020-01-01")
    active = manual_flag("ABDL.NS", FlagCategory.NARRATIVE, FlagSentiment.GREEN, "JV")
    save_flags("ABDL.NS", [expired, active], kv.set)
    loaded = load_flags("ABDL.NS", kv.get)
    assert len(loaded) == 1
    assert loaded[0].headline == "JV"


def test_refresh_all_flags_preserves_manual_and_merges_fresh():
    kv = _FakeKv()
    manual = manual_flag("ABDL.NS", FlagCategory.NARRATIVE, FlagSentiment.GREEN,
                          "Ranveer Singh JV")
    save_flags("ABDL.NS", [manual], kv.set)

    corp_info = {"corporate_actions": {"data": [
        {"symbol": "ABDL", "exdate": "2026-07-01", "purpose": "Buyback of Shares"}
    ]}}
    merged = refresh_all_flags("ABDL.NS", kv.get, kv.set, corp_info=corp_info)
    headlines = {f.headline for f in merged}
    assert "Ranveer Singh JV" in headlines
    assert any("Buyback" in h for h in headlines)


def test_refresh_all_flags_degrades_gracefully_on_fetch_failure(monkeypatch):
    """If the NSE corp_info fetcher raises, refresh should not blow up the
    caller — it should fall back to an empty auto-flag set and keep manual
    flags. Isolated to the corp_info path only (news/RSS are passed as
    already-empty, not left as None) so this doesn't depend on live network
    for the other two independent sources — see
    test_refresh_all_flags_degrades_gracefully_when_news_fetch_fails and
    ..._when_rss_fetch_fails for those paths individually."""
    kv = _FakeKv()
    manual = manual_flag("ABDL.NS", FlagCategory.NARRATIVE, FlagSentiment.GREEN, "JV")
    save_flags("ABDL.NS", [manual], kv.set)

    def _boom(_ticker):
        raise ConnectionError("NSE unreachable")

    import data.nse_corp_info as nci
    monkeypatch.setattr(nci, "get_corp_info", _boom)

    merged = refresh_all_flags(
        "ABDL.NS", kv.get, kv.set, corp_info=None,
        news_items=[], rss_items_by_category={},
    )
    assert len(merged) == 1
    assert merged[0].headline == "JV"


def test_refresh_all_flags_falls_back_to_bse_when_nse_empty(monkeypatch):
    """FIX FLAGS-BSE1: NSE and BSE run independent WAFs — when NSE's
    corp_info comes back empty (e.g. blocked), refresh_all_flags should try
    data/bse_corp_info.py next, and flags built from it must be labeled
    "BSE: ..." (not mislabeled as NSE-sourced)."""
    kv = _FakeKv()

    import data.nse_corp_info as nci
    import data.bse_corp_info as bci
    monkeypatch.setattr(nci, "get_corp_info", lambda ticker, use_cache=True: {})
    monkeypatch.setattr(bci, "get_corp_info", lambda ticker, use_cache=True: {
        "latest_announcements": {"data": [
            {"subject": "Board approves buyback", "broadcastdate": "2026-07-30"}]},
        "corporate_actions": {"data": []},
    })

    merged = refresh_all_flags(
        "SOMETICKER.NS", kv.get, kv.set, corp_info=None,
        news_items=[], rss_items_by_category={},
    )
    sources = [f.source for f in merged]
    assert any(s.startswith("BSE:") for s in sources), sources
    assert not any(s.startswith("NSE top-corp-info:") for s in sources), sources


def test_refresh_all_flags_skips_bse_when_nse_succeeds(monkeypatch):
    """The BSE fallback should only fire when NSE is truly empty — it must
    not be called at all when NSE already returned data, so a working NSE
    source never pays the extra BSE round-trip."""
    kv = _FakeKv()

    import data.nse_corp_info as nci
    import data.bse_corp_info as bci
    monkeypatch.setattr(nci, "get_corp_info", lambda ticker, use_cache=True: {
        "latest_announcements": {"data": [
            {"subject": "NSE-sourced announcement", "broadcastdate": "2026-07-30"}]},
        "corporate_actions": {"data": []},
    })

    def _bse_should_not_be_called(ticker, use_cache=True):
        raise AssertionError("BSE fallback should not be called when NSE succeeded")
    monkeypatch.setattr(bci, "get_corp_info", _bse_should_not_be_called)

    merged = refresh_all_flags(
        "SOMETICKER2.NS", kv.get, kv.set, corp_info=None,
        news_items=[], rss_items_by_category={},
    )
    sources = [f.source for f in merged]
    assert any(s.startswith("NSE top-corp-info:") for s in sources), sources


# ── summarize_flags ──────────────────────────────────────────────────────

def test_summarize_flags_counts_by_sentiment():
    flags = [
        manual_flag("X.NS", FlagCategory.NARRATIVE, FlagSentiment.GREEN, "a"),
        manual_flag("X.NS", FlagCategory.GOVERNANCE, FlagSentiment.RED, "b"),
        manual_flag("X.NS", FlagCategory.MACRO, FlagSentiment.AMBER, "c"),
        manual_flag("X.NS", FlagCategory.NARRATIVE, FlagSentiment.GREEN, "d"),
    ]
    counts = summarize_flags(flags)
    assert counts == {"green": 2, "red": 1, "amber": 1}


# ── news-based flags (QF4) ─────────────────────────────────────────────────

def test_news_flags_negative_keyword_flags_red():
    recent = (_dt.date.today() - _dt.timedelta(days=3)).isoformat()
    news_items = [
        {"title": "Company faces show cause notice from regulator",
         "link": "http://x", "pub_date": recent, "source": "Moneycontrol"},
    ]
    flags = parse_news_flags("ABDL.NS", news_items)
    assert len(flags) == 1
    assert flags[0].sentiment == FlagSentiment.RED
    assert flags[0].source == "News: Moneycontrol"


def test_news_flags_positive_keyword_flags_green():
    news_items = [
        {"title": "Company announces buyback of shares", "source": "ET"},
    ]
    flags = parse_news_flags("ABDL.NS", news_items)
    assert flags[0].sentiment == FlagSentiment.GREEN


def test_news_flags_unmatched_defaults_amber():
    news_items = [{"title": "Company opens new office in Pune", "source": "ET"}]
    flags = parse_news_flags("ABDL.NS", news_items)
    assert flags[0].sentiment == FlagSentiment.AMBER


def test_news_flags_empty_title_skipped():
    assert parse_news_flags("ABDL.NS", [{"title": "", "source": "ET"}]) == []


def test_news_flags_respects_max_items():
    items = [{"title": f"Update {i}", "source": "ET"} for i in range(20)]
    flags = parse_news_flags("ABDL.NS", items, max_items=5)
    assert len(flags) == 5


def test_news_flags_missing_source_defaults_google_news():
    flags = parse_news_flags("ABDL.NS", [{"title": "Some headline"}])
    assert flags[0].source == "News: Google News"


# ── FIX STALE1 — recency filtering/sorting for news + RSS flags ───────────

def test_news_flags_drops_items_older_than_30_days():
    stale = (_dt.date.today() - _dt.timedelta(days=45)).strftime("%a, %d %b %Y")
    fresh = (_dt.date.today() - _dt.timedelta(days=2)).strftime("%a, %d %b %Y")
    news_items = [
        {"title": "Old news nobody should see", "pub_date": stale, "source": "ET"},
        {"title": "Recent relevant update", "pub_date": fresh, "source": "ET"},
    ]
    flags = parse_news_flags("ABDL.NS", news_items)
    assert len(flags) == 1
    assert flags[0].headline == "Recent relevant update"


def test_news_flags_sorts_newest_first():
    older = (_dt.date.today() - _dt.timedelta(days=10)).strftime("%a, %d %b %Y")
    newer = (_dt.date.today() - _dt.timedelta(days=1)).strftime("%a, %d %b %Y")
    news_items = [
        {"title": "Older item", "pub_date": older, "source": "ET"},
        {"title": "Newer item", "pub_date": newer, "source": "ET"},
    ]
    flags = parse_news_flags("ABDL.NS", news_items)
    assert [f.headline for f in flags] == ["Newer item", "Older item"]


def test_news_flags_undated_items_kept_not_dropped():
    """An item with no/unparseable pub_date must never be silently dropped —
    only positively-confirmed-old items get dropped."""
    news_items = [{"title": "No date given", "source": "ET"}]
    flags = parse_news_flags("ABDL.NS", news_items)
    assert len(flags) == 1
    assert flags[0].headline == "No date given"


def test_rss_flags_drops_items_older_than_30_days():
    stale = (_dt.date.today() - _dt.timedelta(days=60)).strftime("%a, %d %b %Y")
    fresh = (_dt.date.today() - _dt.timedelta(days=1)).strftime("%a, %d %b %Y")
    items = {"related_party_transactions": [
        {"title": "Old RPT disclosure", "description": "", "pub_date": stale},
        {"title": "Recent RPT disclosure", "description": "", "pub_date": fresh},
    ]}
    flags = parse_rss_flags("ABDL.NS", items)
    assert len(flags) == 1
    assert flags[0].headline == "Recent RPT disclosure"


def test_build_auto_flags_combines_nse_and_news_independently():
    corp_info = {"corporate_actions": {"data": [
        {"symbol": "ABDL", "exdate": "2026-07-01", "purpose": "Buyback of Shares"}
    ]}}
    news_items = [{"title": "Resignation of CFO", "source": "ET"}]
    flags = build_auto_flags("ABDL.NS", corp_info, news_items)
    sentiments = {f.sentiment for f in flags}
    assert FlagSentiment.GREEN in sentiments   # from corp action
    assert FlagSentiment.RED in sentiments     # from news


def test_build_auto_flags_news_works_when_nse_empty():
    """The key resilience property: NSE returning {} (e.g. blocked) must
    not suppress news-derived flags."""
    news_items = [{"title": "Company announces dividend", "source": "ET"}]
    flags = build_auto_flags("ABDL.NS", {}, news_items)
    assert len(flags) == 1
    assert flags[0].sentiment == FlagSentiment.GREEN


def test_build_auto_flags_nse_works_when_news_empty():
    corp_info = {"corporate_actions": {"data": [
        {"symbol": "ABDL", "exdate": "2026-07-01", "purpose": "Buyback of Shares"}
    ]}}
    flags = build_auto_flags("ABDL.NS", corp_info, news_items=None)
    assert len(flags) == 1


def test_refresh_all_flags_uses_passed_news_items():
    kv = _FakeKv()
    news_items = [{"title": "Promoter pledge increased sharply", "source": "ET"}]
    merged = refresh_all_flags(
        "ABDL.NS", kv.get, kv.set, corp_info={}, news_items=news_items,
    )
    assert any("pledge" in f.headline.lower() for f in merged)


def test_refresh_all_flags_fetches_news_when_not_provided(monkeypatch):
    """When news_items isn't passed explicitly, refresh_all_flags should
    call data.news_feed.fetch_news itself."""
    kv = _FakeKv()

    def _fake_fetch_news(ticker, company_name=None):
        return [{"title": "Company announces buyback of shares", "source": "ET"}]

    import data.news_feed as nf
    monkeypatch.setattr(nf, "fetch_news", _fake_fetch_news)

    merged = refresh_all_flags("ABDL.NS", kv.get, kv.set, corp_info={})
    assert any(f.sentiment == FlagSentiment.GREEN for f in merged)


def test_refresh_all_flags_degrades_gracefully_when_news_fetch_fails(monkeypatch):
    kv = _FakeKv()
    manual = manual_flag("ABDL.NS", FlagCategory.NARRATIVE, FlagSentiment.GREEN, "JV")
    save_flags("ABDL.NS", [manual], kv.set)

    def _boom(ticker, company_name=None):
        raise ConnectionError("news feed unreachable")

    import data.news_feed as nf
    monkeypatch.setattr(nf, "fetch_news", _boom)

    merged = refresh_all_flags("ABDL.NS", kv.get, kv.set, corp_info={})
    assert len(merged) == 1
    assert merged[0].headline == "JV"


# ── data/news_feed.py RSS parsing (no live network) ─────────────────────────

_SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Google News</title>
<item>
<title>Allied Blenders announces buyback of shares - Moneycontrol</title>
<link>https://news.google.com/rss/articles/xyz1</link>
<pubDate>Mon, 01 Jul 2026 10:00:00 GMT</pubDate>
<source url="https://moneycontrol.com">Moneycontrol</source>
</item>
<item>
<title>Allied Blenders Q1 results announced - Economic Times</title>
<link>https://news.google.com/rss/articles/xyz2</link>
<pubDate>Sun, 30 Jun 2026 08:00:00 GMT</pubDate>
<source url="https://economictimes.com">Economic Times</source>
</item>
</channel>
</rss>"""


def test_news_feed_fetch_parses_rss_items(monkeypatch):
    import data.news_feed as nf

    class _FakeResp:
        content = _SAMPLE_RSS.encode("utf-8")
        def raise_for_status(self): pass

    monkeypatch.setattr(nf.requests, "get", lambda *a, **k: _FakeResp())
    items = nf.fetch_news("ABDL.NS", company_name="Allied Blenders", use_cache=False)
    assert len(items) == 2
    assert "buyback" in items[0]["title"].lower()
    assert items[0]["source"] == "Moneycontrol"
    diag = nf.get_last_diagnostic("ABDL.NS")
    assert diag["ok"] is True


def test_news_feed_fetch_handles_request_failure(monkeypatch):
    import data.news_feed as nf
    import requests as _requests

    def _boom(*a, **k):
        raise _requests.ConnectionError("no route to host")

    monkeypatch.setattr(nf.requests, "get", _boom)
    items = nf.fetch_news("ABDL.NS", use_cache=False)
    assert items == []
    diag = nf.get_last_diagnostic("ABDL.NS")
    assert diag["ok"] is False


def test_news_feed_build_query_prefers_company_name():
    import data.news_feed as nf
    q = nf._build_query("Allied Blenders and Distillers", "ABDL.NS")
    assert "Allied Blenders" in q
    assert "NSE" in q


def test_news_feed_build_query_falls_back_to_symbol():
    import data.news_feed as nf
    q = nf._build_query(None, "ABDL.NS")
    assert "ABDL" in q


# ── RSS feed flags (QF5) ─────────────────────────────────────────────────

def test_rss_flags_encumbrance_defaults_red():
    items = {"reason_for_encumbrance": [
        {"title": "ABDL - Reason for encumbrance disclosed", "description": "Loan against shares"}
    ]}
    flags = parse_rss_flags("ABDL.NS", items)
    assert len(flags) == 1
    assert flags[0].sentiment == FlagSentiment.RED
    assert flags[0].category == FlagCategory.GOVERNANCE
    assert "NSE RSS" in flags[0].source


def test_rss_flags_related_party_defaults_amber():
    items = {"related_party_transactions": [
        {"title": "ABDL - Related party transaction disclosure", "description": ""}
    ]}
    flags = parse_rss_flags("ABDL.NS", items)
    assert flags[0].sentiment == FlagSentiment.AMBER


def test_rss_flags_keyword_override_forces_red():
    """Even a category that defaults AMBER should escalate to RED if the
    title itself contains an unambiguous negative keyword."""
    items = {"corporate_governance": [
        {"title": "ABDL - Resignation of Independent Director", "description": ""}
    ]}
    flags = parse_rss_flags("ABDL.NS", items)
    assert flags[0].sentiment == FlagSentiment.RED


def test_rss_flags_unknown_category_ignored():
    items = {"not_a_real_category": [{"title": "Something", "description": ""}]}
    assert parse_rss_flags("ABDL.NS", items) == []


def test_rss_flags_empty_dict_returns_empty():
    assert parse_rss_flags("ABDL.NS", {}) == []


def test_build_auto_flags_combines_all_three_sources():
    corp_info = {"corporate_actions": {"data": [
        {"symbol": "ABDL", "exdate": "2026-07-01", "purpose": "Buyback of Shares"}
    ]}}
    news_items = [{"title": "Resignation of CFO", "source": "ET"}]
    rss_items = {"reason_for_encumbrance": [
        {"title": "ABDL - Promoter pledge disclosed", "description": ""}
    ]}
    flags = build_auto_flags("ABDL.NS", corp_info, news_items, rss_items)
    sources = {f.source.split(":")[0].strip() for f in flags}
    assert len(flags) == 3


def test_refresh_all_flags_uses_passed_rss_items():
    kv = _FakeKv()
    rss_items = {"reason_for_encumbrance": [
        {"title": "ABDL - encumbrance notice", "description": ""}
    ]}
    merged = refresh_all_flags(
        "ABDL.NS", kv.get, kv.set, corp_info={}, news_items=[],
        rss_items_by_category=rss_items,
    )
    assert any(f.sentiment == FlagSentiment.RED for f in merged)


def test_refresh_all_flags_degrades_gracefully_when_rss_fetch_fails(monkeypatch):
    kv = _FakeKv()
    manual = manual_flag("ABDL.NS", FlagCategory.NARRATIVE, FlagSentiment.GREEN, "JV")
    save_flags("ABDL.NS", [manual], kv.set)

    def _boom(ticker, company_name=None):
        raise ConnectionError("rss feed host unreachable")

    import data.nse_rss_feeds as rf
    monkeypatch.setattr(rf, "get_all_relevant_items", _boom)

    merged = refresh_all_flags("ABDL.NS", kv.get, kv.set, corp_info={}, news_items=[])
    assert len(merged) == 1
    assert merged[0].headline == "JV"


# ── data/nse_rss_feeds.py parsing (no live network) ─────────────────────────

_SAMPLE_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>NSE Related Party Transactions</title>
<item>
<title>Allied Blenders and Distillers Limited - Related Party Transaction</title>
<link>https://nseindia.com/some/link</link>
<description>Disclosure under Regulation 23</description>
<pubDate>Mon, 01 Jul 2026 10:00:00 GMT</pubDate>
</item>
<item>
<title>Reliance Industries Limited - Related Party Transaction</title>
<link>https://nseindia.com/other/link</link>
<description>Disclosure under Regulation 23</description>
<pubDate>Sun, 30 Jun 2026 08:00:00 GMT</pubDate>
</item>
</channel>
</rss>"""


def test_nse_rss_feeds_fetch_parses_items(monkeypatch):
    import data.nse_rss_feeds as rf

    class _FakeResp:
        content = _SAMPLE_FEED_XML.encode("utf-8")
        def raise_for_status(self): pass

    monkeypatch.setattr(rf.requests, "get", lambda *a, **k: _FakeResp())
    items = rf.fetch_feed("related_party_transactions", use_cache=False)
    assert len(items) == 2
    diag = rf.get_last_diagnostic("related_party_transactions")
    # Fixture dates are fixed (Jul 2026), so the feed reads "stale" once they
    # age past nse_rss_feeds._STALE_DAYS; items are still parsed either way.
    assert diag["status"] in ("ok", "stale") and diag["items"] == 2


def test_nse_rss_feeds_filters_by_company(monkeypatch):
    import data.nse_rss_feeds as rf

    class _FakeResp:
        content = _SAMPLE_FEED_XML.encode("utf-8")
        def raise_for_status(self): pass

    monkeypatch.setattr(rf.requests, "get", lambda *a, **k: _FakeResp())
    matched = rf.get_items_for_company(
        "related_party_transactions", "ABDL.NS",
        company_name="Allied Blenders and Distillers",
    )
    assert len(matched) == 1
    assert "Allied Blenders" in matched[0]["title"]


def test_nse_rss_feeds_handles_request_failure(monkeypatch):
    import data.nse_rss_feeds as rf
    import requests as _requests

    def _boom(*a, **k):
        raise _requests.ConnectionError("no route to host")

    monkeypatch.setattr(rf.requests, "get", _boom)
    items = rf.fetch_feed("related_party_transactions", use_cache=False)
    assert items == []
    diag = rf.get_last_diagnostic("related_party_transactions")
    assert diag["ok"] is False


def test_nse_rss_feeds_unknown_category_returns_empty():
    import data.nse_rss_feeds as rf
    assert rf.fetch_feed("not_a_real_category") == []


# --- Audit 2026-10-07: empty RSS feeds are "no data", not "all clear" -----

def test_describe_rss_gaps_wording():
    from analysis.qualitative_flags import describe_rss_gaps
    lines = describe_rss_gaps({
        "insider_trading": "empty — InsiderTrading.xml returned HTTP 200 with 0 items",
        "corporate_governance": "stale — newest item is 2026-05-02",
        "sast_regulation_29": "feed fetch failed: Timeout",
    })
    joined = " | ".join(lines)
    assert "insider-trading feed returned no data" in joined
    assert "corporate-governance feed is stale" in joined
    assert "SAST Reg 29 feed was unavailable" in joined
    assert all("not an all-clear" in ln for ln in lines)
    assert describe_rss_gaps({}) == [] and describe_rss_gaps(None) == []


def test_data_health_rss_probe_degrades_on_empty_feed(monkeypatch):
    from dashboard.shared import data_health as dh
    diags = {
        "related_party_transactions": {"ok": True, "status": "ok", "at": "2026-10-07T10:00:00",
                                       "reason": "ok (20 items)"},
        "insider_trading": {"ok": False, "status": "empty", "at": "2026-10-07T10:00:01",
                            "reason": "empty"},
    }
    monkeypatch.setattr(_nse_rss_feeds_mod, "get_last_diagnostic", lambda c: diags.get(c))
    check = dh.probe_nse_rss()
    assert check.status == dh.STATUS_DEGRADED
    assert "insider_trading: empty" in check.note

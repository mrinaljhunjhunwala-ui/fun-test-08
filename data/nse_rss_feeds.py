"""
data/nse_rss_feeds.py — QF5: NSE's official RSS syndication feeds.

WHY THIS EXISTS
    data/nse_corp_info.py hits nseindia.com's dynamic JSON API
    (www.nseindia.com/api/*), which sits behind an aggressive WAF that
    frequently blocks cloud/datacenter IPs (see that module's "KNOWN
    OPERATIONAL LIMITATION"). NSE ALSO publishes official RSS feeds on a
    completely different host — nsearchives.nseindia.com — a static
    content/archive subdomain built for third-party syndication. This is
    NOT a scrape of an undocumented endpoint: NSE's own "RSS Feeds" page
    (nseindia.com/static/rss-feed) publishes these URLs explicitly for
    external consumption, so there's no ToS ambiguity the way there would
    be scraping Screener.in or similar. In testing, this host responded
    successfully where the JSON API host did not — consistent with static-
    archive subdomains generally having a much lighter WAF profile than
    dynamic API hosts.

WHAT THIS COVERS THAT NOTHING ELSE IN THIS APP DOES
    Several of these feeds are the actual "external factors" / red-flag
    category the whole qualitative-flags effort has been chasing, sourced
    directly from the regulatory disclosure itself rather than a third
    party's interpretation of it (e.g. Screener's Red Flags section):
      - Related Party Transactions — the single most-cited governance
        red flag category
      - Reason For Encumbrance — WHY shares were pledged, not just that
        they were (a promoter pledging to fund a personal loan reads very
        differently from pledging to fund a rights issue)
      - SAST Regulation 29 / 31 — substantial acquisition of shares /
        continuing disclosure of promoter holding changes (takeover-
        relevant, and a second independent check on promoter holding
        trend alongside the shareholding-pattern parsing already in
        analysis/qualitative_flags.py)
      - Corporate Governance — governance-specific filings distinct from
        the general announcements feed already covered by nse_corp_info.py

IMPORTANT — these are MARKET-WIDE feeds (every listed company in one XML
    file), not per-ticker. Each feed is fetched and cached ONCE (not once
    per ticker), then filtered client-side for the company being looked
    up. This is far more efficient than a per-ticker fetch would be, and
    is exactly why these feeds exist as a syndication product in the
    first place — one feed serves every consumer.

WHAT THIS DOES NOT DO
    * Does not raise on failure — same discipline as every other fetcher
      in this pipeline; a broken feed must never block the rest of the
      qualitative-flags pipeline.
    * Does not attempt company-name disambiguation beyond substring
      matching — if two listed companies share a very similar name this
      could occasionally cross-match; treat matches as "worth a human
      read", same posture as every other auto-detected flag in this app.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import requests

from analysis.fundamentals.cache import TTLCache

_log = logging.getLogger("data.nse_rss_feeds")

_BASE = "https://nsearchives.nseindia.com/content/RSS"
_TIMEOUT = 10
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, text/xml, */*",
}

# Category -> feed filename, limited to the categories that plausibly carry
# decision-relevant "external factor" signal. NSE publishes more (Annual
# Reports, Voting Results, Share Transfers, etc.) — add here if useful later.
FEEDS: Dict[str, str] = {
    "related_party_transactions": "Related_Party_Trans.xml",
    "reason_for_encumbrance": "Sast_ReasonForEncumbrance.xml",
    "sast_regulation_29": "Sast_Regulation29.xml",
    "sast_regulation_31": "Sast_Regulation31.xml",
    "corporate_governance": "Corporate_Governance.xml",
    "insider_trading": "InsiderTrading.xml",
}

# Alternate filenames tried, in order, when the primary returns 0 items.
# Audit 2026-10-07: InsiderTrading.xml (still the name linked from NSE's RSS
# page) answers HTTP 200 with an empty <channel>, while Insider_Trading.xml
# carries items. Corporate_Governance.xml is likewise empty and no live
# replacement was found, so it reports "empty" until NSE repopulates it.
_ALT_FEEDS: Dict[str, tuple] = {
    "insider_trading": ("Insider_Trading.xml",),
}

# A feed whose newest item is older than this is reported "stale" — its
# items are still returned, but "no matches" must not read as "no events".
_STALE_DAYS = 30

# Diagnostic statuses: "ok" (items, fresh), "stale", "empty" (HTTP 200 but
# zero items), "error" (request/parse failure). Only "ok" sets ok=True.
STATUS_OK, STATUS_STALE, STATUS_EMPTY, STATUS_ERROR = "ok", "stale", "empty", "error"

_feed_cache = TTLCache(ttl_seconds=12 * 60 * 60, name="nse_rss_feeds")
_last_diagnostic: Dict[str, Dict[str, Any]] = {}


def get_last_diagnostic(category: str) -> Optional[Dict[str, Any]]:
    return _last_diagnostic.get(category)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def fetch_feed(category: str, use_cache: bool = True) -> List[Dict[str, str]]:
    """Fetch + parse one NSE RSS feed (market-wide, all companies).

    Returns a list of {"title", "link", "description", "pub_date"} dicts,
    or [] on any failure — never raises. This is cached PER FEED (not per
    ticker) since one fetch serves every company lookup that day.
    """
    if category not in FEEDS:
        _log.warning("fetch_feed: unknown category %s", category)
        return []

    cache_key = f"feed|{category}"
    if use_cache:
        cached = _feed_cache.get(cache_key)
        if cached is not None:
            return cached

    filenames = (FEEDS[category],) + tuple(_ALT_FEEDS.get(category, ()))
    diag: Dict[str, Any] = {}
    items: List[Dict[str, str]] = []
    for filename in filenames:
        items, diag = _fetch_one(category, filename)
        if items:
            break
    _last_diagnostic[category] = diag
    # Cache only real content: an empty/failed feed is retried next call
    # instead of pinning "nothing" for 12h.
    if use_cache and items:
        _feed_cache.set(cache_key, items)
    return items


def _parse_pub_date(raw: str) -> Optional[_dt.datetime]:
    raw = (raw or "").strip()
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S", "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S %Z", "%d-%b-%Y"):
        try:
            return _dt.datetime.strptime(raw, fmt).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _fetch_one(category: str, filename: str):
    """Fetch + parse one feed file. Returns (items, diagnostic)."""
    url = f"{_BASE}/{filename}"
    at = _dt.datetime.now().isoformat(timespec="seconds")
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except requests.RequestException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        _log.warning("fetch_feed(%s): request failed: %s", category, e)
        return [], {
            "ok": False, "status": STATUS_ERROR, "status_code": status, "at": at,
            "file": filename, "items": 0,
            "reason": f"feed fetch failed: {type(e).__name__}: {e}",
        }
    except ET.ParseError as e:
        _log.warning("fetch_feed(%s): RSS parse failed: %s", category, e)
        return [], {
            "ok": False, "status": STATUS_ERROR, "status_code": 200, "at": at,
            "file": filename, "items": 0,
            "reason": f"got a response but couldn't parse RSS XML: {e}",
        }

    items: List[Dict[str, str]] = []
    for item in root.findall(".//item"):
        title = _strip_html(item.findtext("title") or "")
        if not title:
            continue
        items.append({
            "title": title,
            "link": item.findtext("link") or "",
            "description": _strip_html(item.findtext("description") or ""),
            "pub_date": item.findtext("pubDate") or "",
        })

    base = {"status_code": 200, "at": at, "file": filename, "items": len(items)}
    if not items:
        _log.warning("fetch_feed(%s): %s returned HTTP 200 with 0 items", category, filename)
        return [], {**base, "ok": False, "status": STATUS_EMPTY,
                    "reason": f"empty — {filename} returned HTTP 200 with 0 items"}

    dates = [d for d in (_parse_pub_date(i["pub_date"]) for i in items) if d]
    newest = max(dates) if dates else None
    if newest is not None and (_dt.datetime.now() - newest).days > _STALE_DAYS:
        return items, {**base, "ok": False, "status": STATUS_STALE,
                       "newest": newest.date().isoformat(),
                       "reason": f"stale — newest item in {filename} is "
                                 f"{newest.date().isoformat()} ({len(items)} items)"}
    return items, {**base, "ok": True, "status": STATUS_OK,
                   "newest": newest.date().isoformat() if newest else None,
                   "reason": f"ok ({len(items)} items)"}


def get_feed_gaps() -> Dict[str, str]:
    """{category: reason} for every feed whose last fetch was NOT "ok"
    (empty, stale or failed). A category absent from get_all_relevant_items()
    only means "no events" if it is also absent from here."""
    return {cat: str(d.get("reason") or d.get("status") or "unavailable")
            for cat, d in _last_diagnostic.items()
            if cat in FEEDS and not d.get("ok")}


def _matches_company(item: Dict[str, str], symbol: str, company_name: Optional[str]) -> bool:
    haystack = f"{item.get('title', '')} {item.get('description', '')}".upper()
    if symbol and symbol.upper() in haystack:
        return True
    if company_name:
        # Require a reasonably specific match — the first two "words" of
        # the company name together, not a single common word, to cut down
        # on false positives from generic terms.
        words = [w for w in re.split(r"\s+", company_name.upper()) if len(w) > 2]
        if len(words) >= 2 and f"{words[0]} {words[1]}" in haystack:
            return True
        if len(words) == 1 and words[0] in haystack:
            return True
    return False


def get_items_for_company(
    category: str, ticker: str, company_name: Optional[str] = None,
    max_items: int = 5,
) -> List[Dict[str, str]]:
    """Filter one feed's cached items down to those mentioning this company.
    Fetches the feed (cached market-wide) if not already cached this cycle.
    """
    symbol = ticker.replace(".NS", "").replace(".BO", "").upper()
    items = fetch_feed(category)
    matched = [it for it in items if _matches_company(it, symbol, company_name)]
    return matched[:max_items]


def get_all_relevant_items(
    ticker: str, company_name: Optional[str] = None,
) -> Dict[str, List[Dict[str, str]]]:
    """Convenience wrapper: filtered items across every category in FEEDS,
    keyed by category name. Categories with no matches are omitted rather
    than included as empty lists, so callers can just check truthiness.
    """
    out: Dict[str, List[Dict[str, str]]] = {}
    for category in FEEDS:
        matched = get_items_for_company(category, ticker, company_name)
        if matched:
            out[category] = matched
    return out


def get_cache_stats() -> dict:
    return _feed_cache.stats()

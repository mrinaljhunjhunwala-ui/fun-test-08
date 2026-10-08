"""
utils/news.py — Stock & market news via Google News RSS + ET RSS feeds.

Why RSS instead of yfinance .news:
  - Zero rate-limiting  (no API key, pure HTTP)
  - ~20 articles vs yfinance's ~6
  - Fresher: headlines appear within minutes of publication
  - No extra dependencies — uses Python stdlib (urllib + ElementTree)

Architecture:
  get_stock_news(ticker)  → Google News RSS search for that company
  get_market_news()       → ET Markets + Business Standard + Nifty RSS feeds
  Both fall back to yfinance .news if RSS returns nothing.
"""

from __future__ import annotations

import email.utils
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, List

_log = logging.getLogger("news")


# ── Ticker → human-friendly search term map ──────────────────────────────────
_TICKER_SEARCH = {
    "RELIANCE":   "Reliance Industries",
    "TCS":        "TCS Tata Consultancy Services",
    "HDFCBANK":   "HDFC Bank",
    "ICICIBANK":  "ICICI Bank",
    "BHARTIARTL": "Bharti Airtel",
    "INFY":       "Infosys",
    "SBIN":       "State Bank India SBI",
    "HINDUNILVR": "Hindustan Unilever HUL",
    "ITC":        "ITC Limited",
    "LT":         "Larsen Toubro",
    "BAJFINANCE": "Bajaj Finance",
    "HCLTECH":    "HCL Technologies",
    "MARUTI":     "Maruti Suzuki",
    "SUNPHARMA":  "Sun Pharma",
    "ADANIENT":   "Adani Enterprises",
    "KOTAKBANK":  "Kotak Mahindra Bank",
    "TITAN":      "Titan Company",
    "ONGC":       "ONGC Oil Natural Gas Corporation",
    "NTPC":       "NTPC Limited power",
    "AXISBANK":   "Axis Bank",
    "WIPRO":      "Wipro",
    "ULTRACEMCO": "UltraTech Cement",
    "ASIANPAINT": "Asian Paints",
    "BAJAJFINSV": "Bajaj Finserv",
    "POWERGRID":  "Power Grid Corporation",
    "MM":         "Mahindra M&M",
    "NESTLEIND":  "Nestle India",
    "JSWSTEEL":   "JSW Steel",
    "TMPV":       "Tata Motors Passenger Vehicle",
    "TATASTEEL":  "Tata Steel",
    "TECHM":      "Tech Mahindra",
    "GRASIM":     "Grasim Industries",
    "BPCL":       "BPCL Bharat Petroleum",
    "ADANIPORTS": "Adani Ports",
    "CIPLA":      "Cipla",
    "BRITANNIA":  "Britannia Industries",
    "EICHERMOT":  "Eicher Motors Royal Enfield",
    "DRREDDY":    "Dr Reddys Laboratories",
    "HINDALCO":   "Hindalco Industries",
    "COALINDIA":  "Coal India",
    "DIVISLAB":   "Divi's Laboratories",
    "TATACONSUM": "Tata Consumer Products",
    "SBILIFE":    "SBI Life Insurance",
    "APOLLOHOSP": "Apollo Hospitals",
    "HDFCLIFE":   "HDFC Life Insurance",
    "INDUSINDBK": "IndusInd Bank",
    "HEROMOTOCO": "Hero MotoCorp",
    "BAJAJAUTO":  "Bajaj Auto",
    "ETERNAL":    "Eternal Zomato food delivery",
    "SHRIRAMFIN": "Shriram Finance",
    # Portfolio stocks
    "BALRAMCHIN": "Balrampur Chini Mills sugar",
    "VEDL":       "Vedanta Limited",
    "IDFCFIRSTB": "IDFC First Bank",
    "XCHANGING":  "Xchanging Technology",
    "BAJAJHIND":  "Bajaj Hindusthan Sugar",
    "DHANBANK":   "Dhanlaxmi Bank",
}

# ── Market-level RSS feeds — (source name, url). Broad set of reliable Indian
#    financial publishers; unreachable ones are skipped gracefully. ────────────
_MARKET_FEEDS = [
    ("Economic Times",      "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2811036.cms"),
    ("Economic Times",      "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Business Standard",   "https://www.business-standard.com/rss/markets-106.rss"),
    ("Livemint",            "https://www.livemint.com/rss/markets"),
    ("Hindu BusinessLine",  "https://www.thehindubusinessline.com/markets/feeder/default.rss"),
    ("NDTV Profit",         "https://feeds.feedburner.com/ndtvprofit-latest"),
    ("Moneycontrol",        "https://www.moneycontrol.com/rss/MCtopnews.xml"),
    ("Financial Express",   "https://www.financialexpress.com/market/feed/"),
]

_RSS_TIMEOUT = 8   # seconds per feed

# Normalise publisher names so the same source isn't listed twice
_SOURCE_ALIASES = {
    "the economic times":     "Economic Times",
    "economictimes.com":      "Economic Times",
    "businessline":           "Hindu BusinessLine",
    "the hindu businessline": "Hindu BusinessLine",
    "mint":                   "Livemint",
    "ndtv profit":            "NDTV Profit",
    "moneycontrol.com":       "Moneycontrol",
    "business standard":      "Business Standard",
}


def _norm_source(name: str) -> str:
    n = (name or "").strip()
    return _SOURCE_ALIASES.get(n.lower(), n)


def _fetch_rss(url: str, max_items: int = 10, source_name: str = None) -> List[Dict]:
    """
    Fetch and parse one RSS feed.  Uses stdlib only — no third-party packages.
    Returns list of dicts: title, publisher, link, time, raw_time, sentiment.

    source_name: if given, used as the publisher (clean name for direct feeds);
                 otherwise the article's <source> tag or channel title is used.
    """
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent":
                          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
        )
        with urllib.request.urlopen(req, timeout=_RSS_TIMEOUT) as resp:
            data = resp.read()

        root = ET.fromstring(data)
        channel = root.find("channel")
        if channel is None:
            # Some feeds put items directly under root (Atom-ish)
            channel = root

        items = []
        for item in channel.findall("item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            if not title:
                continue

            link = item.findtext("link") or "#"
            pub_date = item.findtext("pubDate") or ""

            # Publisher: explicit source_name (direct feeds) > <source> tag > channel
            if source_name:
                publisher = source_name
            else:
                src = item.find("source")
                publisher = src.text.strip() if (src is not None and src.text) else ""
                if not publisher:
                    ch_title = channel.findtext("title") or ""
                    publisher = ch_title.split(" - ")[0].split("|")[0].strip()[:40]
            publisher = _norm_source(publisher)

            # Parse RFC-2822 date → epoch
            try:
                ts = email.utils.parsedate_to_datetime(pub_date)
                time_str = ts.strftime("%d %b %H:%M")
                raw_time = ts.timestamp()
            except Exception as _e:
                _log.debug("news.%s degraded: %s", "_fetch_rss", _e)
                time_str = "—"
                raw_time = 0

            items.append({
                "title":     title,
                "publisher": publisher,
                "link":      link,
                "time":      time_str,
                "raw_time":  raw_time,
                "sentiment": _quick_sentiment(title),
            })
        return items

    except Exception as e:
        _log.warning("news RSS fetch/parse failed: %s", e)  # empty list masks a real failure
        return []


def _strip_clean(item: Dict) -> Dict:
    """Remove internal-only raw_time key before returning to callers."""
    return {k: v for k, v in item.items() if k != "raw_time"}


def get_stock_news(ticker: str, max_articles: int = 6) -> List[Dict]:
    """
    Fetch recent news for one NSE ticker.
    Primary source: Google News RSS (no key, fast, ~20 articles).
    Fallback: yfinance .news (slow, limited, rate-limited).

    Returns list of dicts: title, publisher, link, time, sentiment.
    """
    sym = ticker.replace(".NS", "").replace(".BO", "")
    # Normalise ticker names like "BAJAJ-AUTO" → "BAJAJAUTO" for map lookup
    sym_key = sym.replace("-", "").replace("&", "")
    search_term = _TICKER_SEARCH.get(sym_key, sym_key) + " NSE India stock"

    url = (
        "https://news.google.com/rss/search?"
        + urllib.parse.urlencode({"q": search_term, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
    )

    raw = _fetch_rss(url, max_items=max_articles * 2)

    # Deduplicate + sort newest-first + trim
    seen: set = set()
    result: List[Dict] = []
    for item in sorted(raw, key=lambda x: -x["raw_time"]):
        if item["title"] not in seen and len(result) < max_articles:
            seen.add(item["title"])
            result.append(_strip_clean(item))

    # Fallback: yfinance if RSS returned nothing (e.g. network blocked)
    if not result:
        result = _yfinance_stock_fallback(ticker, max_articles)

    return result


def get_market_news(max_articles: int = 8) -> List[Dict]:
    """
    Fetch broad Indian market news.
    Sources: ET Markets RSS + Business Standard RSS + Google News Nifty search.
    Fallback: yfinance on ^NSEI + RELIANCE.
    """
    all_items: List[Dict] = []

    # Direct publisher feeds (each tagged with its clean source name)
    for src_name, feed_url in _MARKET_FEEDS:
        all_items.extend(_fetch_rss(feed_url, max_items=5, source_name=src_name))

    # Google News search — aggregates ALL reliable publishers, real source names
    for _q in ("Nifty 50 BSE Sensex India stock market",
               "Indian stock market news today"):
        g_url = ("https://news.google.com/rss/search?"
                 + urllib.parse.urlencode({"q": _q, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}))
        all_items.extend(_fetch_rss(g_url, max_items=6))

    # Deduplicate + sort newest-first
    seen: set = set()
    unique: List[Dict] = []
    for item in sorted(all_items, key=lambda x: -x["raw_time"]):
        if item["title"] not in seen:
            seen.add(item["title"])
            unique.append(_strip_clean(item))

    if not unique:
        unique = _yfinance_market_fallback(max_articles)

    return unique[:max_articles]


# ── yfinance fallbacks ────────────────────────────────────────────────────────

def _flatten_yf_news_item(item: dict) -> Dict:
    """Return {title, publisher, link, ts} from a yfinance ``.news`` item.

    yfinance 0.2.x returned flat dicts (title / publisher / link /
    providerPublishTime epoch). yfinance >=0.2.48 and every 1.x release
    nest everything under ``content`` (title, provider.displayName,
    canonicalUrl.url, pubDate ISO string). Both shapes are accepted.
    """
    if not isinstance(item, dict):
        return {"title": "", "publisher": "", "link": "#", "ts": 0}
    content = item.get("content")
    if isinstance(content, dict):
        prov = content.get("provider") or {}
        url = ((content.get("canonicalUrl") or {}).get("url")
               or (content.get("clickThroughUrl") or {}).get("url") or "#")
        ts = 0
        pub = content.get("pubDate") or content.get("displayTime")
        if pub:
            try:
                ts = datetime.fromisoformat(str(pub).replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = 0
        return {"title": content.get("title", "") or "",
                "publisher": prov.get("displayName", "") if isinstance(prov, dict) else "",
                "link": url, "ts": ts}
    return {"title": item.get("title", "") or "", "publisher": item.get("publisher", ""),
            "link": item.get("link", "#"), "ts": item.get("providerPublishTime", 0)}

def _yfinance_stock_fallback(ticker: str, max_articles: int) -> List[Dict]:
    """Use yfinance .news as a last-resort fallback for a single ticker."""
    try:
        import yfinance as yf
        sym = ticker if ticker.endswith(".NS") else f"{ticker}.NS"
        raw = yf.Ticker(sym).news or []
        result = []
        for item in (_flatten_yf_news_item(i) for i in raw[:max_articles]):
            title = item.get("title", "")
            if not title:
                continue
            ts = item.get("ts", 0)
            try:
                time_str = datetime.fromtimestamp(ts).strftime("%d %b %H:%M")
            except Exception as _e:
                _log.debug("news.%s degraded: %s", "_yfinance_stock_fallback", _e)
                time_str = "—"
            result.append({
                "title":     title,
                "publisher": item.get("publisher", ""),
                "link":      item.get("link", "#"),
                "time":      time_str,
                "sentiment": _quick_sentiment(title),
            })
        return result
    except Exception as e:
        _log.warning("news fetch failed: %s", e)
        return []


def _yfinance_market_fallback(max_articles: int) -> List[Dict]:
    """Use yfinance .news on index + blue chips as fallback for market news."""
    results = []
    seen: set = set()
    try:
        import yfinance as yf
        for sym in ["^NSEI", "RELIANCE.NS", "TCS.NS"]:
            try:
                for item in (_flatten_yf_news_item(i) for i in (yf.Ticker(sym).news or [])[:3]):
                    title = item.get("title", "")
                    if not title or title in seen:
                        continue
                    seen.add(title)
                    ts = item.get("ts", 0)
                    try:
                        time_str = datetime.fromtimestamp(ts).strftime("%d %b %H:%M")
                    except Exception as _e:
                        _log.debug("news.%s degraded: %s", "_yfinance_market_fallback", _e)
                        time_str = "—"
                    results.append({
                        "title":     title,
                        "publisher": item.get("publisher", ""),
                        "link":      item.get("link", "#"),
                        "time":      time_str,
                        "sentiment": _quick_sentiment(title),
                    })
            except Exception as e:
                _log.debug("yfinance news failed for %s: %s", sym, e)  # try next symbol
                continue
    except Exception as e:
        _log.debug("yfinance market-news fallback unavailable: %s", e)
    return results[:max_articles]


# ── Sentiment helper ──────────────────────────────────────────────────────────

_POSITIVE = {
    "surge", "rally", "gain", "rise", "jump", "profit", "beat", "record",
    "high", "up", "growth", "strong", "upgrade", "buy", "bullish",
    "outperform", "boost", "win", "positive", "award", "order", "recovery",
    "rebounds", "climbs", "soars", "hits", "tops", "gains",
}
_NEGATIVE = {
    "fall", "drop", "decline", "loss", "miss", "cut", "down", "weak",
    "sell", "bearish", "downgrade", "crash", "slump", "concern",
    "risk", "penalty", "fine", "probe", "fraud", "delay", "warning",
    "tumbles", "slides", "sinks", "plunges", "falls", "drops",
}

# BUGFIX: title.lower().split() left trailing/leading punctuation glued onto
# words (e.g. "profit!", "growth,", "record-high"), so those tokens never
# matched _POSITIVE/_NEGATIVE even though the root word was present. This
# regex pulls out plain alphabetic tokens instead, splitting on any non-letter
# (so "record-high" also yields "record" and "high" separately).
_WORD_RE = re.compile(r"[a-z]+")


def _quick_sentiment(title: str) -> str:
    words = set(_WORD_RE.findall(title.lower()))
    pos = len(words & _POSITIVE)
    neg = len(words & _NEGATIVE)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"

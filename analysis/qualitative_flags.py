"""
analysis/qualitative_flags.py — QF1

CompositeScore (technical/momentum/volume/sentiment — see analysis/score.py)
can only ever see price and volume. It structurally cannot know that a
company's promoter just increased pledging, that a state raised excise
duty overnight, or that a brand JV with a celebrity just launched. This
module adds that missing layer WITHOUT blending it into the composite
number — flags are shown alongside the score, not folded into it, so the
score stays honest about what it can and cannot see (same discipline as
dashboard/shared/disclosures.py's methodology notices).

Two flag sources:
  1. AUTO-DETECTED — parsed from data/nse_corp_info.py's top-corp-info
     payload (announcements, corporate actions, shareholding pattern).
     Conservative by design: keyword-matched, not NLP-classified, so
     sentiment defaults to AMBER unless a keyword is unambiguous.
  2. MANUAL — analyst-entered, for anything auto-detection can't reliably
     judge: celebrity JV / brand catalysts, premiumisation progress,
     state excise-policy watch, Union Budget sensitivity.

PERSISTENCE — uses the real trade_store signatures:
    kv_get(key: str, default: Any = None, user_id: str = "default") -> Any
    kv_set(key: str, value: Any, user_id: str = "default") -> bool
Pass the actual functions in from trade_store at the call site, e.g.:
    from trade_store import kv_get, kv_set
    flags = load_flags(ticker, kv_get)
    refresh_all_flags(ticker, kv_get, kv_set)

REFRESH CADENCE — daily, not real-time. These are announcement/filing-
driven events, not tick-driven. Run once at pre-open, e.g. alongside the
existing Top Picks background scan in 02_command_centre.py.

This module never touches analysis/score.py or CompositeScore.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

_log = logging.getLogger("analysis.qualitative_flags")

KvGet = Callable[..., Any]     # kv_get(key, default=None, user_id="default")
KvSet = Callable[..., bool]    # kv_set(key, value, user_id="default")


class FlagSentiment(str, Enum):
    GREEN = "green"    # positive factor
    RED = "red"        # negative / risk factor
    AMBER = "amber"    # mixed, unresolved, or "watch"


class FlagCategory(str, Enum):
    REGULATORY = "regulatory"              # excise policy, price approval cycles
    CORPORATE_ACTION = "corporate_action"  # QIP, buyback, debt repayment, rights issue
    GOVERNANCE = "governance"              # promoter holding / pledging changes
    NARRATIVE = "narrative"                # JV, celebrity tie-up, premium launch
    INPUT_COST = "input_cost"              # ENA/grain/glass/commodity pressure
    MACRO = "macro"                        # Union Budget, rural consumption, policy
    ANNOUNCEMENT = "announcement"          # keyword-matched NSE disclosure, uncategorized


@dataclass
class QualitativeFlag:
    ticker: str
    category: FlagCategory
    sentiment: FlagSentiment
    headline: str                 # short human-readable summary
    source: str                   # e.g. "NSE top-corp-info" or "Manual entry"
    date: str                     # ISO date the underlying event/filing occurred
    detected_at: str              # ISO datetime this flag was generated/refreshed
    expiry: Optional[str] = None  # ISO date after which this flag drops from view
    detail: Optional[str] = None  # optional longer note
    is_manual: bool = False       # True if hand-entered rather than auto-fetched

    def to_dict(self) -> dict:
        d = asdict(self)
        d["category"] = self.category.value
        d["sentiment"] = self.sentiment.value
        return d

    @staticmethod
    def from_dict(d: dict) -> "QualitativeFlag":
        d = dict(d)
        d["category"] = FlagCategory(d["category"])
        d["sentiment"] = FlagSentiment(d["sentiment"])
        return QualitativeFlag(**d)

    def is_active(self, as_of: Optional[_dt.date] = None) -> bool:
        if not self.expiry:
            return True
        as_of = as_of or _dt.date.today()
        try:
            return _dt.date.fromisoformat(self.expiry) >= as_of
        except ValueError:
            return True


# ---------------------------------------------------------------------------
# Curated, manually-maintained watch dict for state-level regulatory context.
# NOT auto-scraped — state excise policy news is not structured data
# anywhere. Update periodically, same spirit as the Vedanta/Tata Motors
# demerger entries added to STOCK_SEARCH_MAP in data/universe.py.
# ---------------------------------------------------------------------------
STATE_EXCISE_WATCH: dict[str, dict] = {
    # "ABDL.NS": {
    #     "state": "AP", "note": "Excise policy revision under review",
    #     "sentiment": FlagSentiment.AMBER, "date": "2026-06-01",
    # },
}


def build_regulatory_flags(ticker: str) -> list[QualitativeFlag]:
    """Curated state excise / regulatory watch entries for a ticker."""
    entry = STATE_EXCISE_WATCH.get(ticker)
    if not entry:
        return []
    now = _dt.datetime.now().isoformat()
    return [QualitativeFlag(
        ticker=ticker,
        category=FlagCategory.REGULATORY,
        sentiment=entry.get("sentiment", FlagSentiment.AMBER),
        headline=f"{entry['state']} excise/regulatory watch: {entry['note']}",
        source="Manual watchlist (analyst-curated)",
        date=entry.get("date", now[:10]),
        detected_at=now,
        is_manual=True,
    )]


def manual_flag(
    ticker: str,
    category: FlagCategory,
    sentiment: FlagSentiment,
    headline: str,
    source: str = "Manual entry",
    detail: Optional[str] = None,
    expiry: Optional[str] = None,
) -> QualitativeFlag:
    """Hand-add a narrative/catalyst flag from the UI — e.g. a celebrity JV
    announcement, or an update on a premium-launch's traction."""
    now = _dt.datetime.now()
    return QualitativeFlag(
        ticker=ticker, category=category, sentiment=sentiment, headline=headline,
        source=source, date=now.date().isoformat(), detected_at=now.isoformat(),
        detail=detail, expiry=expiry, is_manual=True,
    )


# ---------------------------------------------------------------------------
# Auto-detection from NSE's top-corp-info payload (data/nse_corp_info.py)
# ---------------------------------------------------------------------------

# Conservative keyword sentiment map for corporate actions / announcements.
# Deliberately short and unambiguous — anything not matched stays AMBER
# rather than guessing, per the codebase's no-silent-certainty discipline
# (see analysis/fundamentals/valuation_decision.py's confidence tiers).
_NEGATIVE_KEYWORDS = (
    "pledge", "encumbrance", "default", "resignation", "resign",
    "downgrade", "delay in payment", "delisting", "insolvency",
    "winding up", "fraud", "show cause", "penalty",
)
_POSITIVE_KEYWORDS = (
    "buyback", "bonus issue", "dividend", "credit rating upgrade",
    "upgrade", "stock split", "debt repayment", "debt reduction",
    "amalgamation approved",
)

# FIX STALE1 — news/RSS items previously had no recency filtering or sort at
# all: parse_news_flags / parse_rss_flags just took the first N items in
# whatever order the fetchers returned them, with no date shown downstream
# (dashboard/pages/20_deep_dive.py) and no expiry set (QualitativeFlag.
# is_active() is a no-op for these — expiry is never populated for news/RSS
# flags), so a stale headline could sit there indefinitely with zero
# recency signal. Fix: drop anything older than NEWS_MAX_AGE_DAYS and sort
# what's left newest-first, before any max_items cap is applied.
NEWS_MAX_AGE_DAYS = 30

# Date formats this module actually needs to parse: RSS pubDate truncated to
# 16 chars ("Thu, 03 Jul 2026" — see parse_news_flags/parse_rss_flags below),
# ISO ("2026-07-01"), and the NSE-style formats already handled in
# parse_shareholding_flags.
_FLAG_DATE_FORMATS = ("%a, %d %b %Y", "%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y")


def _parse_flag_date(raw: str) -> Optional[_dt.date]:
    """Best-effort parse. Returns None (never raises) on anything
    unrecognized — callers must treat that as "unknown age", not "old",
    since a parsing gap here should never silently drop real news."""
    raw = (raw or "").strip()[:16]
    for fmt in _FLAG_DATE_FORMATS:
        try:
            return _dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _drop_stale_and_sort(
    items: list[dict], date_field: str = "pub_date",
    max_age_days: int = NEWS_MAX_AGE_DAYS,
) -> list[dict]:
    """Drop items positively older than max_age_days and sort the rest
    newest-first. Items with a missing/unparseable date are KEPT (never
    dropped on our own parsing failure) but placed after the dated ones,
    since we can't vouch for their recency either way — same discipline as
    the AMBER-not-guessing rule for sentiment classification above."""
    today = _dt.date.today()
    dated: list[tuple[_dt.date, dict]] = []
    undated: list[dict] = []
    for item in items or []:
        d = _parse_flag_date(str((item or {}).get(date_field) or ""))
        if d is None:
            undated.append(item)
            continue
        if (today - d).days > max_age_days:
            continue
        dated.append((d, item))
    dated.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in dated] + undated


def _classify_keyword_sentiment(text: str) -> FlagSentiment:
    low = text.lower()
    if any(k in low for k in _NEGATIVE_KEYWORDS):
        return FlagSentiment.RED
    if any(k in low for k in _POSITIVE_KEYWORDS):
        return FlagSentiment.GREEN
    return FlagSentiment.AMBER


def parse_announcement_flags(
    ticker: str, corp_info: dict, max_items: int = 5,
    source_label: str = "NSE top-corp-info",
) -> list[QualitativeFlag]:
    """Turn latest_announcements into flags. Keyword-classified only —
    treat AMBER results as "needs a human read", not "neutral".

    source_label defaults to the NSE label for backward compatibility;
    refresh_all_flags() passes "BSE" when corp_info came from the BSE
    fallback (data/bse_corp_info.py) instead of NSE, so flags are never
    mislabeled as coming from a source that didn't actually supply them."""
    flags: list[QualitativeFlag] = []
    now = _dt.datetime.now().isoformat()
    items = (corp_info.get("latest_announcements") or {}).get("data") or []
    for item in items[:max_items]:
        subject = str(item.get("subject") or "").strip()
        if not subject:
            continue
        bdate = str(item.get("broadcastdate") or now[:10])
        flags.append(QualitativeFlag(
            ticker=ticker,
            category=FlagCategory.ANNOUNCEMENT,
            sentiment=_classify_keyword_sentiment(subject),
            headline=subject[:180],
            source=f"{source_label}: latest_announcements",
            date=bdate,
            detected_at=now,
        ))
    return flags


def parse_corporate_action_flags(
    ticker: str, corp_info: dict, max_items: int = 5,
    source_label: str = "NSE top-corp-info",
) -> list[QualitativeFlag]:
    """Turn corporate_actions (buyback/rights/QIP/dividend/etc.) into flags.
    `purpose` is the free-text field for the action type.

    source_label — see parse_announcement_flags docstring."""
    flags: list[QualitativeFlag] = []
    now = _dt.datetime.now().isoformat()
    items = (corp_info.get("corporate_actions") or {}).get("data") or []
    for item in items[:max_items]:
        purpose = str(item.get("purpose") or "").strip()
        if not purpose:
            continue
        exdate = str(item.get("exdate") or now[:10])
        flags.append(QualitativeFlag(
            ticker=ticker,
            category=FlagCategory.CORPORATE_ACTION,
            sentiment=_classify_keyword_sentiment(purpose),
            headline=purpose[:180],
            source=f"{source_label}: corporate_actions",
            date=exdate,
            detected_at=now,
        ))
    return flags


def parse_shareholding_flags(ticker: str, corp_info: dict) -> list[QualitativeFlag]:
    """Compare the two most recent shareholding-pattern dates for promoter
    holding / pledge-related fields.

    shareholdings_patterns is {date_str: [ {field: value, ...}, ... ]} with
    NSE-defined field names that are not guaranteed stable across endpoint
    versions. Rather than hardcode one field name, this searches every
    field name in each record for "promoter" or "pledg" (case-insensitive)
    and compares values across the two newest dates. If neither substring
    is found in the payload, this returns [] — it does NOT invent a flag
    from absent data (see module docstring: pledge % may not be present in
    this endpoint at all; that's a known, documented gap, not a bug here).
    """
    flags: list[QualitativeFlag] = []
    now = _dt.datetime.now().isoformat()
    patterns = (corp_info.get("shareholdings_patterns") or {}).get("data") or {}
    if not isinstance(patterns, dict) or len(patterns) < 2:
        return flags

    # Dates as given by NSE — sort descending, assume ISO-ish or DD-MM-YYYY
    # strings; fall back to raw string sort if unparseable.
    def _parse_date(s: str) -> _dt.date:
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y"):
            try:
                return _dt.datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return _dt.date.min

    dates_sorted = sorted(patterns.keys(), key=_parse_date, reverse=True)
    latest_date, prior_date = dates_sorted[0], dates_sorted[1]
    latest_rows = patterns.get(latest_date) or []
    prior_rows = patterns.get(prior_date) or []
    if not latest_rows or not prior_rows:
        return flags

    def _extract(rows: list[dict], needles: tuple[str, ...]) -> Optional[float]:
        for row in rows:
            for k, v in row.items():
                kl = k.lower()
                if any(n in kl for n in needles):
                    try:
                        return float(str(v).replace("%", "").strip())
                    except (ValueError, TypeError):
                        continue
        return None

    # NSE's actual field naming is inconsistent across endpoints/versions —
    # "pr_and_prgrp" (the confirmed real field for promoter+promoter-group
    # holding %) does not contain the substring "promoter", so match on
    # both the human word and NSE's own abbreviation.
    _PROMOTER_NEEDLES = ("promoter", "pr_and_prgrp", "pr_grp")
    _PLEDGE_NEEDLES = ("pledg", "encumbra")

    for needles, label, is_pledge in (
        (_PROMOTER_NEEDLES, "Promoter holding", False),
        (_PLEDGE_NEEDLES, "Promoter pledge", True),
    ):
        cur = _extract(latest_rows, needles)
        prior = _extract(prior_rows, needles)
        if cur is None or prior is None:
            continue
        delta = cur - prior
        if abs(delta) < 0.5:
            continue
        if is_pledge:
            sentiment = FlagSentiment.RED if delta > 0 else FlagSentiment.GREEN
        else:  # promoter holding: rising is good, falling is a caution
            sentiment = FlagSentiment.GREEN if delta > 0 else FlagSentiment.RED
        flags.append(QualitativeFlag(
            ticker=ticker,
            category=FlagCategory.GOVERNANCE,
            sentiment=sentiment,
            headline=f"{label} moved {prior:.1f}% -> {cur:.1f}% "
                     f"({prior_date} -> {latest_date})",
            source="NSE top-corp-info: shareholdings_patterns",
            date=latest_date,
            detected_at=now,
        ))
    return flags


def parse_news_flags(
    ticker: str, news_items: list[dict], max_items: int = 8
) -> list[QualitativeFlag]:
    """Turn Google News headlines (data/news_feed.py) into flags. Same
    conservative keyword classification as parse_announcement_flags — an
    unmatched headline stays AMBER, not a confident guess. Tagged with a
    distinct source string so the UI can show these came from news, not
    an NSE filing (a headline is a weaker signal than a structured filing
    and should read that way).
    """
    flags: list[QualitativeFlag] = []
    now = _dt.datetime.now().isoformat()
    recent_items = _drop_stale_and_sort(news_items, date_field="pub_date")
    for item in recent_items[:max_items]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        source = item.get("source") or "Google News"
        pub_date = str(item.get("pub_date") or "")[:16] or now[:10]
        flags.append(QualitativeFlag(
            ticker=ticker,
            category=FlagCategory.ANNOUNCEMENT,
            sentiment=_classify_keyword_sentiment(title),
            headline=title[:180],
            source=f"News: {source}",
            date=pub_date,
            detected_at=now,
            detail=item.get("link") or None,
        ))
    return flags


def parse_rss_flags(ticker: str, rss_items_by_category: dict) -> list[QualitativeFlag]:
    """Turn NSE's official RSS feed matches (data/nse_rss_feeds.py) into
    flags. Sentiment defaults are category-aware but still conservative —
    these are regulator-mandated disclosures, not proof of wrongdoing, so
    most default to AMBER ("worth a human read") rather than an automatic
    RED/GREEN judgment call this app isn't positioned to make.
    """
    flags: list[QualitativeFlag] = []
    now = _dt.datetime.now().isoformat()

    # Reason For Encumbrance is the one category treated as RED by default —
    # a share pledge being newly disclosed is itself the signal, regardless
    # of the stated reason (the reason is useful context, shown in `detail`,
    # but doesn't change the fact that a pledge exists).
    _category_defaults = {
        "related_party_transactions": (FlagCategory.GOVERNANCE, FlagSentiment.AMBER,
                                        "NSE RSS: Related Party Transactions"),
        "reason_for_encumbrance": (FlagCategory.GOVERNANCE, FlagSentiment.RED,
                                    "NSE RSS: Reason For Encumbrance (pledge)"),
        "sast_regulation_29": (FlagCategory.GOVERNANCE, FlagSentiment.AMBER,
                                "NSE RSS: SAST Regulation 29 (substantial acquisition)"),
        "sast_regulation_31": (FlagCategory.GOVERNANCE, FlagSentiment.AMBER,
                                "NSE RSS: SAST Regulation 31 (continuing disclosure)"),
        "corporate_governance": (FlagCategory.GOVERNANCE, FlagSentiment.AMBER,
                                  "NSE RSS: Corporate Governance filing"),
        "insider_trading": (FlagCategory.GOVERNANCE, FlagSentiment.AMBER,
                             "NSE RSS: Insider Trading disclosure"),
    }

    for category, items in (rss_items_by_category or {}).items():
        category_cfg = _category_defaults.get(category)
        if not category_cfg:
            continue
        cat, default_sentiment, source_label = category_cfg
        for item in _drop_stale_and_sort(items, date_field="pub_date"):
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            # Still run the keyword classifier over the title — an
            # unambiguous negative keyword (e.g. "default", "resignation"
            # showing up inside a governance filing title) should override
            # the category default, same discipline as announcement/news
            # parsing. Otherwise fall back to the category's default.
            keyword_sentiment = _classify_keyword_sentiment(title)
            sentiment = (keyword_sentiment if keyword_sentiment == FlagSentiment.RED
                        else default_sentiment)
            pub_date = str(item.get("pub_date") or "")[:16] or now[:10]
            flags.append(QualitativeFlag(
                ticker=ticker,
                category=cat,
                sentiment=sentiment,
                headline=title[:180],
                source=source_label,
                date=pub_date,
                detected_at=now,
                detail=item.get("description") or None,
            ))
    return flags


def build_auto_flags(
    ticker: str, corp_info: dict, news_items: Optional[list] = None,
    rss_items_by_category: Optional[dict] = None,
    corp_info_source_label: str = "NSE top-corp-info",
) -> list[QualitativeFlag]:
    """Run all auto-detectors against fetched data. Three independent
    sources, each may be {}/[]/None on its own failure without suppressing
    the others: corp_info (data/nse_corp_info.py — WAF-prone JSON API, or
    data/bse_corp_info.py as a fallback — see corp_info_source_label),
    news_items (data/news_feed.py — Google News RSS), rss_items_by_category
    (data/nse_rss_feeds.py — NSE's own official syndication feeds, a
    different NSE subdomain with a much lighter WAF profile than corp_info).
    """
    flags: list[QualitativeFlag] = []
    if corp_info:
        flags.extend(parse_shareholding_flags(ticker, corp_info))
        flags.extend(parse_corporate_action_flags(ticker, corp_info,
                                                   source_label=corp_info_source_label))
        flags.extend(parse_announcement_flags(ticker, corp_info,
                                              source_label=corp_info_source_label))
    if news_items:
        flags.extend(parse_news_flags(ticker, news_items))
    if rss_items_by_category:
        flags.extend(parse_rss_flags(ticker, rss_items_by_category))
    return flags


# ---------------------------------------------------------------------------
# Persistence — matches trade_store.kv_get / kv_set exactly.
# ---------------------------------------------------------------------------

def _flags_key(ticker: str) -> str:
    return f"qualitative_flags:{ticker}"


def save_flags(ticker: str, flags: list[QualitativeFlag], kv_set: KvSet,
               user_id: str = "default") -> bool:
    payload = [f.to_dict() for f in flags]
    return kv_set(_flags_key(ticker), payload, user_id)


def load_flags(ticker: str, kv_get: KvGet, as_of: Optional[_dt.date] = None,
                user_id: str = "default") -> list[QualitativeFlag]:
    """Returns only currently-active (non-expired) flags."""
    raw = kv_get(_flags_key(ticker), [], user_id)
    if not raw:
        return []
    try:
        flags = [QualitativeFlag.from_dict(d) for d in raw]
    except (KeyError, ValueError) as e:
        _log.warning("load_flags(%s): corrupt stored payload, discarding: %s", ticker, e)
        return []
    return [f for f in flags if f.is_active(as_of)]


def refresh_all_flags(
    ticker: str, kv_get: KvGet, kv_set: KvSet,
    corp_info: Optional[dict] = None,
    news_items: Optional[list] = None,
    rss_items_by_category: Optional[dict] = None,
    company_name: Optional[str] = None,
    user_id: str = "default",
) -> list[QualitativeFlag]:
    """Rebuild the flag set for a ticker: fetch fresh auto-flags from THREE
    independent sources — NSE's JSON API (data/nse_corp_info.py), Google
    News (data/news_feed.py), and NSE's official RSS syndication feeds
    (data/nse_rss_feeds.py) — merge with any existing manual flags (manual
    flags are preserved, not overwritten — they're analyst judgment calls,
    not something a refresh should clobber).

    All three sources are fetched independently and each degrades to empty
    on its own failure — if the JSON API is blocked (common on cloud hosts)
    this still returns news- and RSS-derived flags, and so on for any
    combination. No fetch failure raises; a flag refresh failing should
    never block the rest of the page.
    """
    existing = load_flags(ticker, kv_get, user_id=user_id)
    manual = [f for f in existing if f.is_manual]

    corp_info_source_label = "NSE top-corp-info"
    if corp_info is None:
        try:
            from data.nse_corp_info import get_corp_info
            corp_info = get_corp_info(ticker)
        except Exception as e:
            _log.warning("refresh_all_flags(%s): nse_corp_info fetch failed: %s", ticker, e)
            corp_info = {}

        # FIX FLAGS-BSE1: NSE and BSE run independent WAFs — a block on one
        # says nothing about the other. Only tried when NSE came back truly
        # empty (not merely "no announcements today"), so this never doubles
        # up work when NSE is working fine.
        if not corp_info:
            try:
                from data.bse_corp_info import get_corp_info as get_bse_corp_info
                bse_info = get_bse_corp_info(ticker)
                if bse_info:
                    corp_info = bse_info
                    corp_info_source_label = "BSE"
            except Exception as e:
                _log.warning("refresh_all_flags(%s): bse_corp_info fallback failed: %s",
                            ticker, e)

    if news_items is None:
        try:
            from data.news_feed import fetch_news
            news_items = fetch_news(ticker, company_name=company_name)
        except Exception as e:
            _log.warning("refresh_all_flags(%s): news_feed fetch failed: %s", ticker, e)
            news_items = []

    if rss_items_by_category is None:
        try:
            from data.nse_rss_feeds import get_all_relevant_items
            rss_items_by_category = get_all_relevant_items(ticker, company_name=company_name)
        except Exception as e:
            _log.warning("refresh_all_flags(%s): nse_rss_feeds fetch failed: %s", ticker, e)
            rss_items_by_category = {}

    fresh: list[QualitativeFlag] = []
    fresh.extend(build_regulatory_flags(ticker))
    fresh.extend(build_auto_flags(ticker, corp_info, news_items, rss_items_by_category,
                                  corp_info_source_label=corp_info_source_label))

    merged = manual + fresh
    save_flags(ticker, merged, kv_set, user_id=user_id)
    return merged


_RSS_FEED_LABELS = {
    "related_party_transactions": "related-party-transactions",
    "reason_for_encumbrance": "pledge (encumbrance)",
    "sast_regulation_29": "SAST Reg 29",
    "sast_regulation_31": "SAST Reg 31",
    "corporate_governance": "corporate-governance",
    "insider_trading": "insider-trading",
}


def describe_rss_gaps(gaps: Optional[dict]) -> list[str]:
    """Turn {category: reason} (data.nse_rss_feeds.get_feed_gaps) into
    user-facing lines, so a feed that returned nothing reads as "no data",
    never as an implied all-clear for that category."""
    lines = []
    for category, reason in sorted((gaps or {}).items()):
        label = _RSS_FEED_LABELS.get(category, category.replace("_", " "))
        reason = str(reason or "")
        if reason.startswith("stale"):
            what = "is stale"
        elif reason.startswith("empty"):
            what = "returned no data"
        else:
            what = "was unavailable"
        lines.append(f"NSE {label} feed {what} — no {label} flags here is not an "
                     f"all-clear ({reason})")
    return lines


def get_rss_feed_gaps() -> dict:
    """{category: reason} for NSE RSS feeds whose last fetch was empty,
    stale or failed. Never raises."""
    try:
        from data.nse_rss_feeds import get_feed_gaps
        return get_feed_gaps()
    except Exception as e:
        _log.warning("get_rss_feed_gaps: %s", e)
        return {}


def summarize_flags(flags: list[QualitativeFlag]) -> dict:
    """Quick counts for a badge/summary row, e.g. '2 green, 1 red, 1 amber'."""
    out = {"green": 0, "red": 0, "amber": 0}
    for f in flags:
        out[f.sentiment.value] += 1
    return out

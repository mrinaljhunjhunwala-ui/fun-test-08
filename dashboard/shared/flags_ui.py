"""
dashboard/shared/flags_ui.py — QF2

Streamlit-facing layer for analysis/qualitative_flags.py. Two entry points:

  render_flag_strip(ticker)
      Full panel for Analyze Stock — badge summary, expandable per-flag
      list, "refresh now" button, and a manual "add analyst note" form for
      narrative/regulatory factors that can't be auto-detected.

  get_cached_flags(ticker) -> list[QualitativeFlag]
      Cached lookup for use in compact contexts (Tomorrow's Watchlist,
      Top Picks cards) where you only want a small badge, not the full
      panel. Cached at the Streamlit layer (not just nse_corp_info's own
      24h cache) so rendering N cards in one page load doesn't trigger N
      redundant refresh_all_flags() calls in the same session.

WHY A SEPARATE MODULE FROM qualitative_flags.py:
    qualitative_flags.py is pure logic — no Streamlit, no trade_store
    import — testable in isolation (see tests/test_qualitative_flags.py).
    This module is the Streamlit/UI adapter, wired to the real
    trade_store.kv_get/kv_set here, once, in one place.

IMPORTANT — refresh cost and where NOT to use this:
    Each *first* refresh_all_flags() call for a ticker hits NSE's
    top-corp-info endpoint (data/nse_corp_info.py), which does a session
    bootstrap + HTTP round trip. That's fine for a handful of tickers
    (Analyze Stock: 1 ticker; Tomorrow's Watchlist/Top Picks: the already-
    shortlisted top ~15 per bucket) but would be far too slow AND risks
    NSE's WAF rate-limiting/blocking the session if called for every
    ticker in a full universe scan (100s of stocks). Do NOT call this
    inside the wide scanning pass in dashboard/shared/cache.py's
    _tomorrow_watchlist() / _home_top_picks() — only call it, per item,
    on the already-filtered, already-ranked shortlist that gets rendered.
"""
from __future__ import annotations

import html as _html
from typing import Optional
from urllib.parse import urlparse

import streamlit as st

import trade_store as _store
from analysis.qualitative_flags import (
    FlagCategory,
    FlagSentiment,
    QualitativeFlag,
    load_flags,
    manual_flag,
    refresh_all_flags,
    save_flags,
    summarize_flags,
)
from data.nse_corp_info import get_last_diagnostic  # QF3: surface fetch diagnostics

_SENTIMENT_COLOR = {
    FlagSentiment.GREEN: "#6faa84",
    FlagSentiment.RED:   "#c77070",
    FlagSentiment.AMBER: "#f9a825",
}
_SENTIMENT_DOT = {
    FlagSentiment.GREEN: "🟢",
    FlagSentiment.RED:   "🔴",
    FlagSentiment.AMBER: "🟡",
}

_CATEGORY_LABEL = {
    FlagCategory.REGULATORY:       "Regulatory",
    FlagCategory.CORPORATE_ACTION: "Corporate Action",
    FlagCategory.GOVERNANCE:       "Governance",
    FlagCategory.NARRATIVE:        "Narrative / Brand",
    FlagCategory.INPUT_COST:       "Input Cost",
    FlagCategory.MACRO:            "Macro",
    FlagCategory.ANNOUNCEMENT:     "Announcement",
}


@st.cache_data(ttl=6 * 60 * 60, show_spinner=False)
def get_cached_flags(ticker: str, company_name: Optional[str] = None) -> list[dict]:
    """Cached, Streamlit-safe lookup. Returns list[dict] (not dataclasses —
    st.cache_data needs hashable/picklable return values across reruns).
    Cache TTL is 6h: shorter than nse_corp_info's own 24h raw-payload cache,
    so a stale Streamlit cache entry still gets refreshed same-day if the
    user reopens the page later; nse_corp_info's own cache prevents that
    refresh from re-hitting NSE if the raw payload is still fresh.

    Pulls from THREE independent sources under the hood (see
    analysis/qualitative_flags.refresh_all_flags): NSE's JSON API, Google
    News, and NSE's official RSS syndication feeds — each degrades on its
    own failure without suppressing the others.

    company_name improves news/RSS-search accuracy — pass it when
    available (e.g. cs.company_name from the score/fundamentals result);
    falls back to the bare ticker symbol if not given.
    """
    try:
        flags = refresh_all_flags(
            ticker, _store.kv_get, _store.kv_set, company_name=company_name
        )
    except Exception:
        # Flags are a nice-to-have overlay — a failure here must never
        # break the page that's actually trying to show the score/chart.
        flags = load_flags(ticker, _store.kv_get)
    return [f.to_dict() for f in flags]


def _to_objects(raw: list[dict]) -> list[QualitativeFlag]:
    out = []
    for d in raw:
        try:
            out.append(QualitativeFlag.from_dict(d))
        except (KeyError, ValueError):
            continue
    return out


def _is_web_url(value: str) -> bool:
    """True only for http(s) links — a feed-supplied javascript:/data: URL must
    never become a clickable href."""
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _flag_card_html(f: QualitativeFlag) -> str:
    """One flag card for render_flag_strip.

    News-sourced flags (parse_news_flags) store the article URL in `detail`,
    not a description — dumping that raw URL as text stretched the whole
    card/page width for long Google News redirect links, so the headline links
    to it instead (same pattern as the news tab in 19_quality_watch.py).
    RSS/announcement flags put a real description in `detail`, shown as text.

    FIX FLAG-XSS (audit 2026-10-03): headline / detail / source come from RSS,
    exchange announcements and manual notes and land in unsafe_allow_html —
    they are escaped, and only http(s) URLs become links.
    """
    color = _SENTIMENT_COLOR[f.sentiment]
    cat_label = _CATEGORY_LABEL.get(f.category, f.category.value)
    manual_tag = " · analyst note" if f.is_manual else ""
    headline = _html.escape(f.headline or "")
    detail = f.detail or ""
    is_news = (f.source or "").startswith("News:")
    if is_news and _is_web_url(detail):
        headline_html = (
            f'<a href="{_html.escape(detail.strip(), quote=True)}" target="_blank" rel="noopener" '
            f'style="font-size:13px;color:#eee;text-decoration:underline">'
            f'{headline}</a>'
        )
        detail_html = ""
    else:
        headline_html = f'<span style="font-size:13px;color:#eee">{headline}</span>'
        detail_html = (
            f'<br><span style="font-size:11px;color:#999">{_html.escape(detail)}</span>'
            if detail and not is_news else ""
        )
    return (
        f'<div style="border-left:3px solid {color};padding:6px 10px;'
        f'margin:4px 0;background:#181818;border-radius:4px;'
        f'word-wrap:break-word;overflow-wrap:break-word;max-width:100%">'
        f'<span style="font-size:11px;color:{color};font-weight:700">'
        f'{cat_label}{manual_tag} · {_html.escape(str(f.date))}</span><br>'
        f'{headline_html}'
        f'{detail_html}'
        f'<br><span style="font-size:10px;color:#666">{_html.escape(f.source or "")}</span>'
        '</div>'
    )


def render_flag_badge_html(ticker: str, company_name: Optional[str] = None) -> str:
    """Compact inline HTML chip for embedding inside an existing card's
    HTML string (Tomorrow's Watchlist / Top Picks style). Returns "" if
    there are no active flags — callers should treat that as "nothing to
    show", not as a failure.
    """
    raw = get_cached_flags(ticker, company_name)
    flags = _to_objects(raw)
    if not flags:
        return ""
    counts = summarize_flags(flags)
    parts = []
    if counts["red"]:
        parts.append(f'{_SENTIMENT_DOT[FlagSentiment.RED]} {counts["red"]}')
    if counts["amber"]:
        parts.append(f'{_SENTIMENT_DOT[FlagSentiment.AMBER]} {counts["amber"]}')
    if counts["green"]:
        parts.append(f'{_SENTIMENT_DOT[FlagSentiment.GREEN]} {counts["green"]}')
    label = " · ".join(parts)
    return (
        f'<span style="font-size:11px;color:#ccc;margin-left:8px;'
        f'padding:2px 6px;background:#1a1a1a;border-radius:8px" '
        f'title="Qualitative flags — see Analyze Stock for detail">{label}</span>'
    )


def render_flag_strip(ticker: str, company_name: Optional[str] = None) -> None:
    """Full panel for Analyze Stock. Call this after the score hero
    section — it is deliberately visually separate from the composite
    score card, not blended into it.
    """
    raw = get_cached_flags(ticker, company_name)
    flags = _to_objects(raw)
    counts = summarize_flags(flags)

    st.markdown("---")
    header_col, refresh_col = st.columns([5, 1])
    with header_col:
        st.markdown("##### 🏳️ Qualitative Flags")
        st.caption(
            "Regulatory, governance, and narrative factors the score above "
            "can't see — from NSE filings + recent news, updated daily, "
            "plus anything you add manually below."
        )
    with refresh_col:
        if st.button("🔄 Refresh", key=f"qf_refresh_{ticker}"):
            get_cached_flags.clear()
            st.rerun()

    if not flags:
        nse_diag, news_diag = None, None
        try:
            nse_diag = get_last_diagnostic(ticker)
        except Exception:
            pass
        try:
            from data.news_feed import get_last_diagnostic as _news_diag
            news_diag = _news_diag(ticker)
        except Exception:
            pass

        nse_blocked = nse_diag and not nse_diag.get("ok")
        news_blocked = news_diag and not news_diag.get("ok")
        if nse_blocked or news_blocked:
            lines = []
            if nse_blocked:
                lines.append(f"**NSE API fetch:** {nse_diag.get('reason', 'unknown error')}")
            if news_blocked:
                lines.append(f"**News fetch:** {news_diag.get('reason', 'unknown error')}")
            st.warning(
                "⚠️ Auto-fetch had trouble for this ticker:\n\n" + "\n\n".join(lines) +
                "\n\nNSE's official RSS feeds (a separate, usually more "
                "reliable channel) are checked automatically too — this "
                "warning means the other source(s) had trouble, not "
                "necessarily everything. This is a fetch problem, not "
                "necessarily an absence of real flags — add a manual note "
                "below if you know of something relevant in the meantime."
            )
        else:
            st.info(
                "No flags on record for this ticker yet — nothing turned up "
                "across NSE's API, NSE's RSS feeds, or recent news. This is "
                "not the same as \"all clear\"; add a manual note below if "
                "you know of something the auto-scan wouldn't catch (e.g. "
                "a brand JV, or a state excise policy change)."
            )
    else:
        badge_line = "  ".join(
            f'{_SENTIMENT_DOT[f.sentiment]}' for f in
            sorted(flags, key=lambda x: x.sentiment.value)
        )
        st.markdown(
            f"**{counts['red']} red · {counts['amber']} amber · {counts['green']} green**"
        )
        for f in sorted(flags, key=lambda x: (x.sentiment != FlagSentiment.RED,
                                               x.sentiment != FlagSentiment.AMBER)):
            st.markdown(_flag_card_html(f), unsafe_allow_html=True)

    with st.expander("➕ Add analyst note (manual flag)"):
        st.caption(
            "Use this for things the auto-scan structurally can't judge: "
            "a celebrity/brand JV, premiumisation launch progress, or a "
            "state excise policy development you've read about."
        )
        c1, c2 = st.columns(2)
        with c1:
            _cat = st.selectbox(
                "Category", options=list(FlagCategory),
                format_func=lambda c: _CATEGORY_LABEL.get(c, c.value),
                key=f"qf_cat_{ticker}",
            )
        with c2:
            _sent = st.selectbox(
                "Sentiment", options=list(FlagSentiment),
                format_func=lambda s: s.value.capitalize(),
                key=f"qf_sent_{ticker}",
            )
        _headline = st.text_input("Headline (short)", key=f"qf_headline_{ticker}")
        _detail = st.text_area("Detail (optional)", key=f"qf_detail_{ticker}", height=68)
        _expiry = st.date_input(
            "Auto-expire on (optional)", value=None, key=f"qf_expiry_{ticker}",
            help="e.g. set to the Union Budget date for a budget-sensitivity note.",
        )
        if st.button("Save note", key=f"qf_save_{ticker}"):
            if not _headline.strip():
                st.warning("Headline can't be empty.")
            else:
                new_flag = manual_flag(
                    ticker=ticker, category=_cat, sentiment=_sent,
                    headline=_headline.strip(), detail=_detail.strip() or None,
                    expiry=_expiry.isoformat() if _expiry else None,
                )
                existing = load_flags(ticker, _store.kv_get)
                save_flags(ticker, existing + [new_flag], _store.kv_set)
                get_cached_flags.clear()
                st.success("Saved. Refresh above to see it in the list.")

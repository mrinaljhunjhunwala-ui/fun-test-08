"""
dashboard/shared/ui_components.py — shared visual building blocks.

Sprint B: the app renders very similar UI on 6+ pages (Command Centre Top
Picks, My Watchlist, Analyze Stock hero, Deep Dive, Tomorrow's Watchlist,
etc.) but each page hand-rolled its own HTML with subtly-different colors,
font sizes, and inconsistent inclusion of freshness / confidence / cost
context. This module is the single place all of them can pull from so the
same concept LOOKS the same everywhere without a mega-component refactor.

Every function returns a self-contained HTML string. Callers stamp it into
st.markdown(..., unsafe_allow_html=True). Nothing here touches Streamlit —
these are pure string builders, testable in isolation.
"""
from __future__ import annotations
import math

from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Single source of truth for verdict / action / regime colors
# ─────────────────────────────────────────────────────────────────────────────
# The previous code had slightly-different green / amber / red per page; users
# saw the same STRONG BUY as one shade of green on Top Picks and a different
# shade on Watchlist. These constants are the palette every page should pull
# from. Colors chosen for AA-contrast against a dark background (#0d1526).

COLORS = {
    "STRONG BUY": "#6faa84",   # teal-green — strongest bull
    "BUY":        "#4CAF50",   # solid green
    "WATCH":      "#2196F3",   # blue (was WATCHLIST in some places)
    "WATCHLIST":  "#2196F3",
    "HOLD":       "#9E9E9E",   # neutral grey
    "CAUTION":    "#FF9800",   # amber
    "EXIT":       "#c77070",   # solid red
    "AVOID":      "#B71C1C",   # deeper red (final-verdict veto)
    "UNAVAILABLE": "#555555",
}

REGIME_COLORS = {
    "trend_up":   "#6faa84",
    "trend_down": "#c77070",
    "range":      "#FFC107",
    "risk_off":   "#B71C1C",
    "unknown":    "#666666",
}

REGIME_EMOJI = {
    "trend_up": "📈", "trend_down": "📉", "range": "⇄",
    "risk_off": "🚨", "unknown": "❓",
}

REGIME_NOTES = {
    # 20-day per-week rank correlation (IC) of the composite with forward returns,
    # 10y replay 2017-07 -> 2026-07 with current scoring (incl. #198's bear-regime
    # Technical/RS neutral). docs/SCORE_EFFICACY_BEAR_2026-09-28.md
    "trend_up":
        "Trending up. The score has ranked stocks best in this regime "
        "(20-day rank correlation ≈ +0.04)",
    "trend_down":
        "Trending down. Momentum reads a 5-day reversal here, and Technical and RS are "
        "held neutral; rankings have been mildly positive (20-day rank correlation ≈ +0.07)",
    "range":
        "Range-bound. The score has had little ranking power in this regime "
        "(20-day rank correlation ≈ 0)",
    "risk_off":
        "Risk-off (VIX ≥ 22). Technical and RS are held neutral in this regime; rankings have "
        "been mildly positive (20-day rank correlation ≈ +0.03)",
    "unknown":
        "Regime undetermined. Data unavailable",
}


def action_color(action_or_verdict: str) -> str:
    """Look up the canonical color for a verdict / action label."""
    return COLORS.get((action_or_verdict or "").upper(), COLORS["HOLD"])


# ─────────────────────────────────────────────────────────────────────────────
# Verdict pill (used on Top Picks cards, Analyze Stock banner, Watchlist rows)
# ─────────────────────────────────────────────────────────────────────────────

def verdict_pill(verdict: str, horizon: str = "medium",
                 confidence: str = "medium",
                 conviction: Optional[int] = None,
                 primary_reason: str = "",
                 compact: bool = True) -> str:
    """
    A colored pill showing "VERDICT: BUY" (or STRONG BUY / WATCH / HOLD /
    AVOID) with a hover tooltip carrying horizon, confidence, conviction
    and primary reason. `compact=True` returns the small inline pill used
    on cards; False returns a fuller banner for page hero areas.
    """
    c = action_color(verdict)
    tip_bits = [f"{horizon}-term lens", f"{confidence} confidence"]
    if conviction is not None:
        tip_bits.append(f"conviction {conviction}/100")
    tip = " · ".join(tip_bits)
    if primary_reason:
        tip = f"{tip}. {primary_reason}"
    if compact:
        return (
            f'<span style="background:{c}22;color:{c};border:1px solid {c};'
            f'border-radius:5px;padding:1px 7px;font-size:10px;font-weight:700;'
            f'margin-left:6px" title="{tip}">'
            f'VERDICT: {verdict}</span>'
        )
    return (
        f'<div style="display:inline-block;background:{c}22;color:{c};'
        f'border:1px solid {c};border-radius:6px;padding:4px 10px;'
        f'font-size:13px;font-weight:700" title="{tip}">'
        f'{verdict}</div>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Freshness stamp (single "scored HH:MM · live price as of now" line)
# ─────────────────────────────────────────────────────────────────────────────

def freshness_stamp(scored_at: str = "",
                    live_ok: bool = True) -> str:
    """
    Small footer line showing when the numbers on the card were computed
    and whether live-price data is available. Kept intentionally quiet
    (small font, low-contrast color) so it's readable without competing
    with the primary card content.
    """
    scored_txt = scored_at or "unknown"
    live_txt = "live price as of now" if live_ok else "live price unavailable — showing last close"
    return (
        f'<div style="font-size:10px;color:#555;margin-top:3px">'
        f'📊 Scored at {scored_txt} · 💹 {live_txt}'
        f'</div>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# DT1 · Source pill — "via <source>" tag for data-heavy cards
# DT2 · data_as_of  — canonical "Data as of ..." freshness stamp
# Backlog UI_UX_BACKLOG.md · both 🟨 P2 · S. Every card that loads
# externally-sourced data should carry both: WHERE the data came from and
# WHEN it was fetched. Prior state had inconsistent, hand-rolled variants
# on ~10 pages; these are the single source of truth.
# ─────────────────────────────────────────────────────────────────────────────

_SOURCE_META = {
    # Known sources with a short, readable label + tone. Colour maps to the
    # data-provider's semantic weight, not the numbers themselves — Angel One
    # (broker, high trust for live prices) reads as accent; scraped/aggregator
    # sources read as dim.
    "nse":       {"label": "NSE",       "tone": "accent"},
    "bse":       {"label": "BSE",       "tone": "accent"},
    "angel_one": {"label": "Angel One", "tone": "accent"},
    "yfinance":  {"label": "Yahoo",     "tone": "dim"},
    "yahoo":     {"label": "Yahoo",     "tone": "dim"},
    "stooq":     {"label": "Stooq",     "tone": "dim"},
    "screener":  {"label": "Screener",  "tone": "dim"},
    "rss":       {"label": "RSS feed",  "tone": "dim"},
    "google":    {"label": "Google",    "tone": "dim"},
    "cache":     {"label": "cache",     "tone": "faint"},
    "manual":    {"label": "manual entry", "tone": "faint"},
}

_SOURCE_TONE_COLOR = {
    "accent": "#c88a3f",
    "dim":    "#8b8e97",
    "faint":  "#5f636c",
}


def source_pill(source: str,
                ttl_hint: str = "",
                extra_title: str = "") -> str:
    """A compact "via <source>" pill for data-heavy cards (DT1).

    source: a key from `_SOURCE_META` (case-insensitive) OR a free-form
        string. Unknown values render as the plain string with the dim
        tone — the pill never fails or omits itself for an unknown source,
        so callers can always emit one.
    ttl_hint: optional short string ("60s cache", "5m cache", "TTL 3600s")
        shown on hover — helps advanced users know how stale the value can
        be. Kept out of the visible pill to save horizontal space.
    extra_title: optional additional hover text appended after ttl_hint.

    Renders as a small chip: `<span>via NSE</span>`. Callers stamp with
    st.markdown(..., unsafe_allow_html=True) inside any container.
    """
    key = (source or "").strip().lower().replace(" ", "_").replace("-", "_")
    meta = _SOURCE_META.get(key)
    if meta is None:
        label = (source or "unknown").strip() or "unknown"
        tone = "dim"
    else:
        label = meta["label"]
        tone = meta["tone"]
    colour = _SOURCE_TONE_COLOR.get(tone, _SOURCE_TONE_COLOR["dim"])

    title_bits = [f"Data source: {label}"]
    if ttl_hint:
        title_bits.append(ttl_hint)
    if extra_title:
        title_bits.append(extra_title)
    title = " · ".join(title_bits)

    return (
        f'<span title="{title}" '
        f'style="display:inline-block;font-family:\'JetBrains Mono\',monospace;'
        f'font-size:10px;letter-spacing:.4px;color:{colour};'
        f'border:1px solid {colour}55;background:{colour}12;'
        f'border-radius:4px;padding:1px 6px;margin-left:6px;'
        f'vertical-align:baseline">'
        f'via {label}</span>'
    )


def data_as_of(when: str = "",
               source: str = "",
               ttl_hint: str = "") -> str:
    """Canonical "Data as of <when>" freshness stamp (DT2).

    when: display-ready timestamp string. Callers format however they want
        ("14:32 IST", "2026-09-19 09:15", "3 min ago"); this helper does
        NOT do the formatting — that's a page decision (some surfaces want
        relative time, others absolute).
    source: optional — when passed, appends a `source_pill()` to the same
        line so a card can carry both DT1 + DT2 in one stamp.
    ttl_hint: forwarded to `source_pill()` when source is set.

    Returns raw HTML. Sits at the bottom of a card, subdued.
    """
    when_txt = (when or "").strip() or "unknown"
    pill_html = source_pill(source, ttl_hint=ttl_hint) if source else ""
    return (
        f'<div style="font-size:10px;color:#8b8e97;'
        f'font-family:\'JetBrains Mono\',monospace;margin-top:4px">'
        f'Data as of {when_txt}{pill_html}'
        f'</div>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Risk-reward line with gross + cost-adjusted variants
# ─────────────────────────────────────────────────────────────────────────────

def rr_line(rr_gross: float, rr_net: float,
            cost_pct: float = 0.30) -> str:
    """
    The "R:R X.X:1 gross, Y.Y:1 net of ~Z% costs" line every card should
    carry so gross ratios don't overstate expected edge.
    """
    return (
        f'<div style="font-size:11px;color:#888;margin-top:2px">'
        f'R:R <span style="color:#fff">{rr_gross:.1f}:1</span> gross, '
        f'<span style="color:#ffb300">{rr_net:.1f}:1 net of ~{cost_pct:.2f}% costs</span>'
        f'</div>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Regime badge — use the SAME visual on every page that needs the context
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Sprint 1.3 — panel() and stat() shared components
# Replace the three parallel systems the audit flagged (design.py .card-*,
# inline glass-panel divs, _pto_cell helper). Both source colour from
# design.py CSS custom properties by name, not raw hex — Task 1.6 hook
# rejects hex in page files.
# ─────────────────────────────────────────────────────────────────────────────

_PANEL_TONE_ACCENT = {
    "neutral": "var(--hairline)",
    "info":    "var(--accent)",
    "bull":    "var(--bull)",
    "bear":    "var(--bear)",
    "amber":   "var(--amber)",
    "violet":  "var(--violet)",
}

_PANEL_KINDS = {
    # §9.2 Dealing Room texture layers -- flat/glass panels float above the
    # pure-black ground as a 2% white lift, not a heavier tinted rectangle.
    # sunken stays on --sunken for secondary surfaces (data-health inners,
    # code snippets, etc.) where a slight recess reads correctly.
    "flat":   {"bg": "var(--card-lift)", "radius": "var(--r-base)",
               "border": "1px solid var(--hairline)"},
    "glass":  {"bg": "var(--card-lift)", "radius": "var(--r-soft)",
               "border": "1px solid var(--hairline)"},
    "sunken": {"bg": "var(--sunken)",    "radius": "var(--r-base)",
               "border": "1px solid var(--hairline-soft)"},
}


def panel(body_html: str,
          kind: str = "flat",
          tone: str = "neutral",
          title: str = "",
          margin: str = "6px 0") -> str:
    """Container for a card / hero / metric group.

    kind: 'flat' (default cards), 'glass' (hero panels), 'sunken' (secondary)
    tone: 'neutral' (default), 'info', 'bull', 'bear', 'amber', 'violet' —
          adds a 3-px semantic rail on the left when non-neutral.
    title: optional eyebrow label rendered in the mono utility face.

    Returns raw HTML — caller stamps via st.markdown(..., unsafe_allow_html=True).
    """
    style = _PANEL_KINDS.get(kind, _PANEL_KINDS["flat"])
    rail_color = _PANEL_TONE_ACCENT.get(tone, _PANEL_TONE_ACCENT["neutral"])
    rail_css = (f"border-left:3px solid {rail_color};"
                if tone != "neutral" else "")
    title_html = (
        f'<div style="font-family:var(--font-mono);font-size:10px;'
        f'letter-spacing:1.2px;text-transform:uppercase;color:var(--dim);'
        f'font-weight:600;margin-bottom:8px">{title}</div>'
        if title else ""
    )
    return (
        f'<div style="background:{style["bg"]};border:{style["border"]};'
        f'{rail_css}border-radius:{style["radius"]};'
        f'padding:14px 18px;margin:{margin}">'
        f'{title_html}{body_html}</div>'
    )


_STAT_TONE_COLOR = {
    "neutral": "var(--ink)",
    "bull":    "var(--bull)",
    "bear":    "var(--bear)",
    "amber":   "var(--amber)",
    "accent":  "var(--accent)",
    "dim":     "var(--dim)",
}


def loading_skeleton(kind: str = "card", count: int = 1) -> str:
    """Return CSS-shimmer skeleton HTML for slow surfaces (F6).

    Pages that trigger a >1s network/compute call (scoring a ticker, running
    the deep-confirmation pass, fetching news, portfolio-fit assessment)
    render one of these into an `st.empty()` placeholder BEFORE the slow
    work starts, then clear the placeholder when the real content arrives.
    Users see a shape-of-what's-coming preview instead of a naked spinner
    over a blank page — the top-4 slowest surfaces in the app.

    Kinds:
      - 'card'  : one glass-panel card with 3 short lines (default)
      - 'hero'  : tall verdict-card shape — big title + 4 tile grid
      - 'chart' : 280px chart-shaped block
      - 'table' : 6-row table shape
      - 'text'  : 3 short paragraph lines
    Set `count` >1 to repeat the same shape (e.g. 3 news cards).

    The shimmer honours prefers-reduced-motion via CSS in design.py.
    """
    def _line(w_pct: int, h: int = 12, mt: int = 8) -> str:
        return (f'<span class="cc-skel" style="height:{h}px;width:{w_pct}%;'
                f'margin-top:{mt}px"></span>')

    def _card() -> str:
        return ('<div class="cc-skel-card">'
                f'{_line(38, 14, 0)}'
                f'{_line(72, 12, 10)}'
                f'{_line(56, 12, 6)}'
                '</div>')

    def _hero() -> str:
        tiles = "".join(
            f'<div style="flex:1;min-width:120px">{_line(60, 10, 0)}'
            f'{_line(80, 22, 8)}</div>'
            for _ in range(4)
        )
        return ('<div class="cc-skel-card" style="padding:18px 22px">'
                f'{_line(48, 22, 0)}'
                f'{_line(72, 14, 10)}'
                f'<div style="display:flex;gap:18px;margin-top:18px;'
                'flex-wrap:wrap">'
                f'{tiles}'
                '</div></div>')

    def _chart() -> str:
        return ('<div class="cc-skel-card" style="padding:12px 14px">'
                f'{_line(30, 12, 0)}'
                '<span class="cc-skel" style="display:block;height:260px;'
                'width:100%;margin-top:10px;border-radius:8px"></span>'
                '</div>')

    def _table() -> str:
        rows = "".join(
            f'<div style="display:flex;gap:12px;margin-top:8px">'
            f'{_line(20, 12, 0)}{_line(14, 12, 0)}'
            f'{_line(16, 12, 0)}{_line(18, 12, 0)}</div>'
            for _ in range(6)
        )
        return f'<div class="cc-skel-card">{rows}</div>'

    def _text() -> str:
        return ('<div class="cc-skel-card">'
                f'{_line(80, 12, 0)}'
                f'{_line(92, 12, 8)}'
                f'{_line(70, 12, 8)}'
                '</div>')

    builder = {"card": _card, "hero": _hero, "chart": _chart,
               "table": _table, "text": _text}.get(kind, _card)
    return "".join(builder() for _ in range(max(1, int(count))))


def stat(label: str, value: str,
         delta: str = "",
         delta_positive: Optional[bool] = None,
         sub: str = "",
         tone: str = "neutral",
         align: str = "left") -> str:
    """Single-value stat (label + big number + optional delta and sub-line).

    Replaces st.metric / .metric-box / _pto_cell / _glass_metric etc.
    Numeric value renders in the mono face for column-friendly alignment.

    tone: 'neutral' | 'bull' | 'bear' | 'amber' | 'accent' | 'dim'
    delta_positive: True → green arrow, False → red, None → grey (no arrow)
    """
    value_color = _STAT_TONE_COLOR.get(tone, _STAT_TONE_COLOR["neutral"])
    delta_html = ""
    if delta:
        if delta_positive is True:
            _c, _sym = "var(--bull)", "▲"
        elif delta_positive is False:
            _c, _sym = "var(--bear)", "▼"
        else:
            _c, _sym = "var(--dim)", "•"
        delta_html = (
            f'<div style="font-family:var(--font-mono);font-size:12px;'
            f'color:{_c};font-weight:600;margin-top:4px">'
            f'{_sym} {delta}</div>'
        )
    sub_html = (
        f'<div style="font-size:11px;color:var(--dim);margin-top:3px">{sub}</div>'
        if sub else ""
    )
    return (
        f'<div style="text-align:{align}">'
        f'<div style="font-family:var(--font-mono);font-size:10px;'
        f'letter-spacing:1.2px;text-transform:uppercase;color:var(--dim);'
        f'font-weight:600">{label}</div>'
        f'<div style="font-family:var(--font-mono);font-size:23px;'
        f'font-weight:700;color:{value_color};margin-top:4px;'
        f'letter-spacing:-.3px">{value}</div>'
        f'{delta_html}{sub_html}</div>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sprint 1.4 — Verdict Card hero for Analyze Stock
# Audit's #1 finding: "So what should I do?" was never the loudest thing on
# the page. This is that one thing. Renders action, conviction score, size
# in rupees, R multiple, horizon, and (when armed) the F&O positioning
# regime — all in a single at-a-glance panel above every other on-page
# section. Pure string builder; caller composes portfolio_ctx if available.
# ─────────────────────────────────────────────────────────────────────────────

def verdict_card(cs, portfolio_ctx: Optional[dict] = None,
                 capital_per_trade: float = 100_000.0) -> str:
    """Build the top-of-page verdict card for Analyze Stock.

    cs is a CompositeScore instance. portfolio_ctx is optional — pass a dict
    like {'shares_held': 40, 'avg_price': 2500.0} to render a position line.
    capital_per_trade drives the suggested share count from risk-per-trade.

    Only reads fields that CompositeScore always populates + the four new
    fields shipped in Recs 1-6: rs_score, positioning_score, is_fno,
    momentum_fallback.
    """
    action    = getattr(cs, "action", "HOLD") or "HOLD"
    score     = float(getattr(cs, "score", 0.0) or 0.0)
    grade     = getattr(cs, "grade", "F") or "F"
    horizon   = getattr(cs, "horizon", "") or ""
    entry     = float(getattr(cs, "entry", 0.0) or 0.0)
    sl        = float(getattr(cs, "stop_loss", 0.0) or 0.0)
    tp        = float(getattr(cs, "target", 0.0) or 0.0)
    rr        = float(getattr(cs, "risk_reward", 0.0) or 0.0)
    rs_score  = getattr(cs, "rs_score", None)
    pos_score = getattr(cs, "positioning_score", None)
    is_fno    = bool(getattr(cs, "is_fno", False))
    ticker    = str(getattr(cs, "ticker", "")).replace(".NS", "") or "—"

    tone = "bull" if action in ("STRONG BUY", "BUY") else \
           "bear" if action in ("EXIT", "AVOID") else \
           "amber" if action in ("CAUTION",) else "info"
    action_color_ = action_color(action)

    # Risk-per-trade sizing: 1% of capital, at least 1 share, capped when
    # entry is unavailable.
    per_share_risk = max(entry - sl, 0.01) if entry > 0 else 0.0
    if per_share_risk > 0 and entry > 0:
        risk_budget = capital_per_trade * 0.01
        shares      = max(1, int(risk_budget / per_share_risk))
        position_rs = shares * entry
    else:
        shares      = 0
        position_rs = 0.0

    # Header: ticker + action pill + grade + horizon
    # NB: F&O chip is precomputed outside the f-string. Python 3.11 rejects
    # backslashes inside f-string expressions (PEP 701 relaxed this in 3.12);
    # CI runs on 3.11 so we keep the escape-free form.
    fno_chip = (
        ' · <span style="background:var(--tint-accent);color:var(--accent);'
        'border:1px solid var(--accent);border-radius:4px;padding:2px 6px;'
        'font-size:10px;font-weight:600;letter-spacing:.5px">F&amp;O</span>'
        if is_fno else ""
    )
    horizon_txt = horizon or "-"
    header = (
        f'<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;'
        f'margin-bottom:14px">'
        f'<span style="font-size:22px;font-weight:700;color:var(--ink);'
        f'letter-spacing:-.3px">{ticker}</span>'
        f'<span style="background:{action_color_}22;color:{action_color_};'
        f'border:1px solid {action_color_};border-radius:6px;padding:4px 12px;'
        f'font-size:13px;font-weight:700;letter-spacing:.5px">{action}</span>'
        f'<span style="color:var(--dim);font-family:var(--font-mono);'
        f'font-size:12px">Grade {grade} · {horizon_txt}</span>'
        f'{fno_chip}'
        f'</div>'
    )

    # Left cluster: conviction score (big) + score bar
    # FIX POS2-UI (Rec 6 design 6b): F&O ticker with the Positioning pillar
    # active scores against a 100-pt cap instead of 90 (the extra 10 comes
    # from the positioning overlay). Show the real ceiling so the fraction
    # reads honestly. Non-F&O or pillar-inactive tickers keep the 90 cap.
    _cap = 100.0 if pos_score is not None else 90.0
    score_pct = max(0.0, min(1.0, score / _cap))
    conviction = (
        f'<div style="text-align:left">'
        f'<div style="font-family:var(--font-mono);font-size:10px;'
        f'letter-spacing:1.2px;text-transform:uppercase;color:var(--dim);'
        f'font-weight:600">Conviction</div>'
        f'<div style="font-family:var(--font-mono);font-size:44px;'
        f'font-weight:700;color:{action_color_};line-height:1;letter-spacing:-1px;'
        f'margin-top:6px">{score:.0f}<span style="font-size:16px;color:var(--dim);'
        f'font-weight:500">/{int(_cap)}</span></div>'
        f'<div style="margin-top:8px;height:4px;background:var(--hairline);'
        f'border-radius:2px;overflow:hidden;width:140px">'
        f'<div style="height:100%;width:{score_pct*100:.1f}%;'
        f'background:{action_color_}"></div></div></div>'
    )

    # Right cluster: entry / stop / target / R:R / size — all as stat()
    def _fmt_rs(x: float) -> str:
        return f"Rs.{x:,.2f}"
    sl_pct = ((sl / entry - 1) * 100) if entry > 0 else 0.0
    tp_pct = ((tp / entry - 1) * 100) if entry > 0 else 0.0
    trade = (
        '<div style="display:grid;grid-template-columns:repeat(5, minmax(90px,1fr));'
        'gap:14px 22px;flex:1">'
        + stat("Entry",  _fmt_rs(entry), tone="neutral")
        + stat("Stop",   _fmt_rs(sl),
               sub=f"{sl_pct:+.1f}%", tone="bear")
        + stat("Target", _fmt_rs(tp),
               sub=f"{tp_pct:+.1f}%", tone="bull")
        + stat("R:R",    f"{rr:.1f}x", tone="accent")
        + stat("Suggested size", f"{shares} sh"
               if shares else "—",
               sub=(_fmt_rs(position_rs) if position_rs else "risk-budget 1%"),
               tone="neutral")
        + '</div>'
    )

    body = (
        f'{header}'
        f'<div style="display:flex;gap:28px;align-items:flex-start;flex-wrap:wrap">'
        f'{conviction}{trade}</div>'
    )

    # Optional secondary row: RS + positioning + portfolio position
    footer_bits = []
    if rs_score is not None:
        _rs_tone = "bull" if rs_score >= 70 else "amber" if rs_score >= 40 else "bear"
        footer_bits.append(stat("RS vs Nifty", f"{rs_score:.0f}",
                                sub="0-100 percentile", tone=_rs_tone))
    if pos_score is not None:
        _pos_tone = ("bull" if pos_score >= 7 else
                     "amber" if pos_score >= 4 else "bear")
        footer_bits.append(stat("Positioning", f"{pos_score:.1f}/10",
                                sub="OI · PCR · MP · FII", tone=_pos_tone))
    # FIX OVERLAY1 (Task 3.3) - TQS x valuation sidecar. Rendered as a footer
    # stat next to RS / Positioning, deliberately NOT next to the big 0-90
    # conviction number, so it reads as a secondary quality-x-value lens
    # rather than a competing headline. Never blended into cs.score.
    overlay_score = getattr(cs, "overlay_score", None)
    if overlay_score is not None:
        _ov_tone = ("bull"  if overlay_score >= 70 else
                    "amber" if overlay_score >= 45 else "bear")
        footer_bits.append(stat("Quality x Value", f"{overlay_score}/100",
                                sub="TQS x valuation posture", tone=_ov_tone))
    if portfolio_ctx:
        _q = int(portfolio_ctx.get("shares_held", 0) or 0)
        _a = float(portfolio_ctx.get("avg_price", 0) or 0.0)
        if _q:
            pnl = (entry - _a) * _q if entry > 0 else 0.0
            footer_bits.append(stat("Your position", f"{_q} sh @ Rs.{_a:,.2f}",
                                    sub=f"P/L Rs.{pnl:+,.0f}",
                                    tone="bull" if pnl >= 0 else "bear"))
    if footer_bits:
        body += (
            '<div style="margin-top:16px;padding-top:14px;'
            'border-top:1px solid var(--hairline-soft);'
            'display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));'
            'gap:14px 20px">'
            + "".join(footer_bits)
            + '</div>'
        )

    # DT1 + DT2 · attribution stamp on the verdict card. The composite
    # score is scored from data/fetcher.py's tiered pipeline (Angel One →
    # Stooq → Yahoo) with a warm in-process cache — surface WHERE and
    # WHEN so users know how stale the number can be.
    _ts = str(getattr(cs, "timestamp", "") or "").strip()
    _src = str(getattr(cs, "source", "") or "").strip() or "yfinance"
    _when = _ts[11:16] + " IST" if len(_ts) >= 16 else (_ts or "unknown")
    body += (
        '<div style="margin-top:14px;padding-top:10px;'
        'border-top:1px solid var(--hairline-soft)">'
        + data_as_of(_when, source=_src, ttl_hint="score cache 5 min")
        + '</div>'
    )

    return panel(body, kind="glass", tone=tone, margin="12px 0")


def regime_badge(label: str = "unknown",
                 confidence: str = "low",
                 compact: bool = False) -> str:
    """
    Standardized market-regime badge. `compact=True` returns a small inline
    pill (good for page-title rows). False returns the full-width banner
    used on Command Centre.
    """
    c = REGIME_COLORS.get(label, REGIME_COLORS["unknown"])
    e = REGIME_EMOJI.get(label, REGIME_EMOJI["unknown"])
    note = REGIME_NOTES.get(label, REGIME_NOTES["unknown"])
    pretty = label.replace("_", " ").title()

    if compact:
        return (
            f'<span style="background:{c}15;color:{c};border:1px solid {c};'
            f'border-radius:5px;padding:2px 8px;font-size:11px;font-weight:700" '
            f'title="{note}">{e} {pretty} · {confidence}</span>'
        )
    return (
        f'<div style="background:linear-gradient(90deg,{c}15,{c}05);'
        f'border-left:4px solid {c};border-radius:8px;padding:9px 14px;'
        f'margin:6px 0 12px 0;display:flex;justify-content:space-between;'
        f'align-items:center;flex-wrap:wrap">'
        f'<span><span style="font-size:15px">{e} '
        f'<b style="color:{c}">{pretty}</b></span> '
        f'<span style="font-size:11px;color:#888;margin-left:8px">'
        f'{confidence.title()} confidence</span></span>'
        f'<span style="font-size:12px;color:#bbb">{note}</span>'
        f'</div>'
    )


# ═══════════════════════════════════════════════════════════════════════════
# UI/UX 2026-09 · chip vocabulary + hero verdict card
# See docs/UI_UX_DESIGN_2026-09.md §4 (cross-cutting rules).
# Four chip shapes, never mixed in one row:
#   chip_tag    -- squared, neutral labels ("Nifty 50", "Delivery 42%")
#   chip_pill   -- rounded, semantic state ("Constructive", "Overheated")
#   chip_delta  -- mono, numeric change ("+1.24%", "-36.65")
#   (tabs are native st.tabs -- no helper)
# ═══════════════════════════════════════════════════════════════════════════

def chip_tag(label: str) -> str:
    """Squared neutral tag. Uppercase, letter-spaced, muted."""
    return (
        f'<span style="display:inline-flex;align-items:center;'
        f'padding:2px 8px;border:1px solid var(--hairline);border-radius:3px;'
        f'font-size:10px;color:var(--dim);text-transform:uppercase;'
        f'letter-spacing:0.08em;font-weight:500;'
        f'margin-right:4px;line-height:1.5">{label}</span>'
    )


def chip_pill(label: str, tone: str = "neutral", title: str = "") -> str:
    """Rounded semantic-state pill. tone: good/warn/bad/accent/neutral.

    Optional `title` renders as a hover tooltip via the standard HTML title
    attribute -- lets a chip carry secondary context (rationale, confidence,
    horizon) without forcing it into the visible label.
    """
    palette = {
        "good":    ("var(--bull)",   "var(--tint-bull)",   "rgba(111,170,132,.4)"),
        "warn":    ("var(--amber)",  "var(--tint-amber)",  "rgba(214,166,76,.4)"),
        "bad":     ("var(--bear)",   "var(--tint-bear)",   "rgba(199,112,112,.4)"),
        "accent":  ("var(--accent)", "var(--tint-accent)", "rgba(200,138,63,.4)"),
        "neutral": ("var(--dim)",    "rgba(255,255,255,.06)", "var(--hairline)"),
    }
    fg, bg, border = palette.get(tone, palette["neutral"])
    title_attr = f' title="{title}"' if title else ""
    return (
        f'<span style="display:inline-flex;align-items:center;'
        f'padding:3px 10px;border-radius:999px;background:{bg};color:{fg};'
        f'border:1px solid {border};font-size:11px;font-weight:600;'
        f'letter-spacing:0.02em;margin-right:4px"{title_attr}>{label}</span>'
    )


def fmt_inr(value: float, decimals: int = 0) -> str:
    """Format a numeric value with Indian lakh/crore digit grouping.

    Python's own ``{:,.0f}`` uses international thousand-separators
    (``1,234,567``). Indian numeric convention groups the last three digits
    then pairs after that (``12,34,567`` — twelve lakh thirty-four thousand
    five hundred sixty-seven). Applied here for P&L displays and any other
    surface where an Indian audience will read the number aloud.

    Returns the unsigned magnitude formatted with a `,` group separator —
    callers own the sign and prefix (₹, +/-, ▲/▼) via surrounding markup.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    neg = v < 0
    v = abs(v)
    if decimals > 0:
        int_part, _, frac_part = f"{v:.{decimals}f}".partition(".")
    else:
        int_part = f"{v:.0f}"
        frac_part = ""
    if len(int_part) <= 3:
        grouped = int_part
    else:
        last3 = int_part[-3:]
        rest = int_part[:-3]
        # Insert commas every 2 digits from the right into `rest`.
        chunks = []
        while len(rest) > 2:
            chunks.append(rest[-2:])
            rest = rest[:-2]
        if rest:
            chunks.append(rest)
        grouped = ",".join(reversed(chunks)) + "," + last3
    out = grouped + (f".{frac_part}" if frac_part else "")
    return f"-{out}" if neg else out


def ticker_hover_wrap(display_label: str,
                      sparkline_svg: str = "",
                      price: Optional[float] = None,
                      chg_pct: Optional[float] = None,
                      score: Optional[float] = None,
                      sector: str = "",
                      ) -> str:
    """UX2 · Wrap a ticker label in a hover preview.

    Renders as ``<span class="ticker-hover">LABEL<span class="ticker-hover-card">
    sparkline + price + delta + optional score chip + optional sector
    </span></span>``.

    Anchors to whatever text the caller passes as ``display_label`` — usually
    the ticker's clean symbol ("RELIANCE") but any HTML-safe string works.
    The preview is a pure-CSS ``:hover`` tooltip (see design.py UX2 block);
    all data is baked into the span at render time — no JS, no lazy fetch,
    no custom Streamlit component.

    Every field is optional; missing fields collapse cleanly. If nothing
    beyond the label is worth showing, this still returns a functional
    hover wrapper — it just shows the sector line (if any) and nothing else.

    Callers that do NOT want the hover behaviour at a given site should stop
    calling this helper — there is no ``enabled=False`` flag. Keeps the HTML
    surface predictable and the caller in control of the data dependencies
    (a ``sparkline_svg`` needs a ``_sparkline_svg(_sparkline_closes(t))``
    call — spec that at the caller site, not here).
    """
    def _finite(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    price, chg_pct, score = _finite(price), _finite(chg_pct), _finite(score)
    rows_html = ""
    if price is not None or chg_pct is not None:
        _p_txt = f"₹{price:,.2f}" if price is not None else "—"
        if chg_pct is not None:
            _d_col = "var(--bull, #6faa84)" if chg_pct >= 0 else "var(--bear, #c77070)"
            _d_arr = "▲" if chg_pct >= 0 else "▼"
            _d_html = (f'<span class="thc-delta" style="color:{_d_col}">'
                       f'{_d_arr} {abs(chg_pct):.2f}%</span>')
        else:
            _d_html = ""
        rows_html += (
            f'<div class="thc-row">'
            f'<span class="thc-price">{_p_txt}</span>{_d_html}'
            f'</div>'
        )
    if score is not None:
        if score >= 65:
            _s_col, _s_bg = "var(--bull, #6faa84)", "var(--tint-bull, rgba(111,170,132,.16))"
        elif score >= 45:
            _s_col, _s_bg = "var(--amber, #d6a64c)", "var(--tint-amber, rgba(214,166,76,.16))"
        else:
            _s_col, _s_bg = "var(--bear, #c77070)", "var(--tint-bear, rgba(199,112,112,.16))"
        rows_html += (
            f'<div class="thc-row">'
            f'<span class="thc-sym">Score</span>'
            f'<span class="thc-score" style="background:{_s_bg};color:{_s_col}">'
            f'{score:.0f}/90</span>'
            f'</div>'
        )
    if sparkline_svg:
        rows_html += sparkline_svg
    if sector:
        rows_html += f'<div class="thc-sector">{sector}</div>'
    header = f'<div class="thc-row"><span class="thc-sym">{display_label}</span></div>'
    return (
        f'<span class="ticker-hover" tabindex="0">'
        f'{display_label}'
        f'<span class="ticker-hover-card" role="tooltip">'
        f'{header}{rows_html}'
        f'</span>'
        f'</span>'
    )


def chip_delta(value: float, unit: str = "%") -> str:
    """Mono numeric-change chip. Auto-signed. Bull >0, bear <0, dim ==0."""
    if value > 0:
        color, arrow, sign = "var(--bull)", "▲", "+"
    elif value < 0:
        color, arrow, sign = "var(--bear)", "▼", ""
    else:
        color, arrow, sign = "var(--dim)", "▪", ""
    return (
        f'<span style="font-family:var(--font-mono);font-size:12px;'
        f'font-weight:500;color:{color};letter-spacing:-.01em;'
        f'margin-right:6px">{arrow} {sign}{value:.2f}{unit}</span>'
    )


def _hero_pillars(pillars: Optional[list]) -> str:
    """Pillar bars for the hero's score column: (name, value, max) tuples."""
    rows = []
    for name, value, cap in pillars or []:
        try:
            v, c = float(value or 0.0), float(cap or 0.0)
        except (TypeError, ValueError):
            continue
        share = max(0.0, min(1.0, v / c)) if c > 0 else 0.0
        fill = ("var(--accent)" if share >= 0.5 else
                "var(--amber)" if share >= 0.3 else "var(--bear)")
        rows.append(
            f'<div class="hv-pillar"><span class="hv-pl-n">{name}</span>'
            f'<span class="hv-pl-bar"><span class="hv-pl-fill" '
            f'style="width:{share * 100:.0f}%;background:{fill}"></span></span>'
            f'<span class="hv-pl-v">{v:.0f} / {c:.0f}</span></div>'
        )
    return f'<div class="hv-pillars">{"".join(rows)}</div>' if rows else ""


def hero_verdict(posture: str,
                 posture_qualifier: str = "",
                 composite: Optional[float] = None,
                 max_score: float = 90,
                 kicker: str = "Posture · this session",
                 why: str = "",
                 tone: str = "accent",
                 pillars: Optional[list] = None) -> str:
    """Editorial verdict hero card (mockup Variant A -- serif italic posture).

    Two-column card. LEFT: kicker, serif italic posture noun, muted
    qualifier, why paragraph. RIGHT: big mono composite score with
    /max denominator. tone: accent / good / warn / bad.

    pillars: optional (name, value, max) tuples drawn as bars under the
    score (artboard 02). A bar under half its max turns amber, under 30%
    bear red, so a weak pillar stands out without reading the numbers.
    """
    tone_color = {
        "accent": "var(--accent)",
        "good":   "var(--bull)",
        "warn":   "var(--amber)",
        "bad":    "var(--bear)",
    }.get(tone, "var(--accent)")

    q_html = (
        f' <span style="color:var(--dim);font-style:italic;font-weight:400">'
        f'{posture_qualifier}</span>'
        if posture_qualifier else ""
    )

    score_html = ""
    if composite is not None:
        pct = max(0.0, min(1.0, composite / max_score))
        score_html = (
            f'<div style="padding:22px 26px;background:var(--sunken);'
            f'border-left:1px solid var(--hairline);display:flex;'
            f'flex-direction:column;justify-content:center;align-items:flex-start;'
            f'gap:8px">'
            f'<div style="font-size:10px;letter-spacing:.14em;'
            f'text-transform:uppercase;color:var(--dim);font-weight:600">'
            f'Composite</div>'
            f'<div style="display:flex;align-items:baseline;gap:8px;'
            f'font-family:var(--font-mono)">'
            f'<span style="font-size:44px;font-weight:500;letter-spacing:-.02em;'
            f'color:var(--ink)">{composite:.0f}</span>'
            f'<span style="font-size:16px;color:var(--dim)">/ {max_score:.0f}</span>'
            f'</div>'
            f'<div style="width:100%;height:4px;background:var(--hairline);'
            f'border-radius:999px;overflow:hidden">'
            f'<div style="height:100%;width:{pct*100:.1f}%;background:{tone_color}"></div>'
            f'</div>'
            f'{_hero_pillars(pillars)}'
            f'</div>'
        )

    # Inline serif style so hero_verdict works standalone (independent of the
    # .verdict-posture-serif class from the typography slice).
    # NOTE: Python 3.11 f-strings do NOT allow backslashes in expression
    # parts, so the paragraph HTML is built outside the f-string. Don't
    # inline the escaped-quotes ternary or CI collection breaks.
    serif_stack = "'Instrument Serif','Iowan Old Style',Georgia,serif"
    why_html = ""
    if why:
        why_html = (
            '<p style="font-size:13.5px;color:var(--ink-mid);line-height:1.55;'
            f'margin:0;max-width:46ch">{why}</p>'
        )
    lead_html = (
        f'<div style="padding:22px 26px;background:linear-gradient(180deg,'
        f'color-mix(in srgb,{tone_color} 6%,var(--surface)),var(--surface) 80%);'
        f'border-right:1px solid var(--hairline)">'
        f'<div style="font-size:10px;letter-spacing:.16em;text-transform:uppercase;'
        f'color:{tone_color};font-weight:700;margin-bottom:10px">{kicker}</div>'
        f'<div style="font-family:{serif_stack};font-weight:400;font-size:32px;'
        f'line-height:1.1;letter-spacing:-.015em;color:var(--ink);'
        f'margin:4px 0 10px 0">'
        f'<em style="font-style:italic;color:{tone_color}">{posture}.</em>{q_html}'
        f'</div>'
        f'{why_html}'
        f'</div>'
    )

    grid_cols = "1.4fr 1fr" if score_html else "1fr"
    # F4 motion policy · slide-up on entry. The class hooks the shared
    # keyframes defined in design.py; prefers-reduced-motion collapses the
    # duration to 0 there, so this stays inert for users who opted out.
    return (
        f'<div class="motion-slide-up" style="display:grid;'
        f'grid-template-columns:{grid_cols};'
        f'border:1px solid var(--hairline);border-radius:var(--r-base);'
        f'overflow:hidden;margin:12px 0 20px 0">'
        f'{lead_html}{score_html}</div>'
    )


# ═══════════════════════════════════════════════════════════════════════════
# DT3 · degraded-mode banner (docs/UI_UX_BACKLOG.md)
# ═══════════════════════════════════════════════════════════════════════════
# Every page currently rolls its own "provider unavailable" / "source
# throttled" / "showing last known data" message -- some as st.warning,
# some as st.caption, some as raw markdown. That inconsistency reads as
# unpolished ("is this a real problem or just a hint?"). One shared
# banner shape means every degraded state carries the same visual weight
# and the same tone (amber warn by default, red bad for hard failure).

def degraded_banner(title: str,
                    detail: str = "",
                    fallback: str = "",
                    tone: str = "warn") -> str:
    """Provider-degraded / source-unreachable banner.

    title:    the short headline ("Data quality alert", "Source unreachable").
    detail:   one-line reason ("14 of 500 tickers were unavailable this scan").
    fallback: what the page is showing instead ("Picks are still valid but
              the universe is narrower than usual"). Rendered muted.
    tone:     'warn' (amber, default -- degraded but usable) or 'bad'
              (bear red -- hard failure / no data at all).

    Returns raw HTML -- callers stamp via st.markdown(unsafe_allow_html=True).
    """
    rail_color = "var(--bear)" if tone == "bad" else "var(--amber)"
    tint_bg    = "var(--tint-bear)" if tone == "bad" else "var(--tint-amber)"
    icon       = "⛔" if tone == "bad" else "⚠"

    detail_html = (
        f'<div style="font-size:12.5px;color:var(--ink-mid);line-height:1.5;'
        f'margin-top:3px">{detail}</div>'
        if detail else ""
    )
    fallback_html = (
        f'<div style="font-size:11.5px;color:var(--dim);line-height:1.5;'
        f'margin-top:6px;font-style:italic">{fallback}</div>'
        if fallback else ""
    )
    return (
        f'<div style="background:{tint_bg};border-left:4px solid {rail_color};'
        f'border-radius:var(--r-base);padding:10px 14px;margin:8px 0">'
        f'<div style="font-size:12px;font-weight:700;color:{rail_color};'
        f'letter-spacing:.05em;text-transform:uppercase">{icon} {title}</div>'
        f'{detail_html}{fallback_html}'
        f'</div>'
    )


# ═══════════════════════════════════════════════════════════════════════════
# F5 · empty-state kit (docs/UI_UX_BACKLOG.md)
# ═══════════════════════════════════════════════════════════════════════════
# Every page currently degrades differently when it has nothing to show --
# some use st.info (blue tint = "informational"), some st.warning (amber =
# "attention"), some a plain caption. An empty list isn't an error and isn't
# a warning; it's a state the user just hasn't populated yet. This helper
# gives every such state the same soft, muted panel so "empty by design"
# reads consistently across the app.

def empty_state(title: str, hint: str = "", icon: str = "") -> str:
    """Centered soft-neutral empty-state block.

    title: one short line ("Your watchlist is empty").
    hint:  one-line guidance for the next action.
    icon:  optional emoji or single glyph rendered above the title.

    Returns raw HTML -- callers stamp via st.markdown(unsafe_allow_html=True).
    Deliberately quieter than st.info/st.warning so a blank list doesn't
    read as an error or a warning; use those Streamlit primitives when the
    message IS informational or an actionable warning.
    """
    icon_html = (
        f'<div style="font-size:32px;line-height:1;margin-bottom:10px;'
        f'opacity:.7">{icon}</div>'
        if icon else ""
    )
    hint_html = (
        f'<div style="font-size:12.5px;color:var(--dim);line-height:1.55;'
        f'max-width:44ch;margin:6px auto 0">{hint}</div>'
        if hint else ""
    )
    return (
        f'<div style="display:flex;flex-direction:column;align-items:center;'
        f'justify-content:center;text-align:center;padding:34px 20px;'
        f'background:var(--sunken);border:1px dashed var(--hairline);'
        f'border-radius:var(--r-base);margin:12px 0">'
        f'{icon_html}'
        f'<div style="font-size:14.5px;font-weight:600;color:var(--ink-mid);'
        f'letter-spacing:.005em">{title}</div>'
        f'{hint_html}'
        f'</div>'
    )


# ═══════════════════════════════════════════════════════════════════════════
# IR1 · Structured Bull / Bear / Risk card (docs/UI_UX_BACKLOG.md)
# ═══════════════════════════════════════════════════════════════════════════
# The thesis engine in analysis/thesis/ already produces bull_factors +
# bear_factors + key_risks as typed Factor lists, each with source and
# evidence. Analyze Stock renders them inside the "Thesis" tab as three
# stacked chip lists -- a user has to click into the tab to see them.
#
# IR1 promotes the same payload to a proper hero-tier card that can sit
# above the fold alongside the Verdict Card. Three columns (bull / bear /
# risks) each with a semantic left-rail, count badge, and up to `limit`
# factors as inline chip-cards. The Thesis tab keeps the full list and its
# rules-provenance caption; this card is the at-a-glance summary.
#
# Deliberately no dependency on the ThesisResult dataclass -- takes plain
# lists of dict-shaped factors so the helper is unit-testable without the
# engine and future callers (screener rows, watchlist deep-hover) can pass
# a hand-built list.

_BBR_COL_STYLES = {
    # (rail, tint bg for the column header pill, icon)
    "bull": ("var(--bull)",  "var(--tint-bull)",  "🟢"),
    "bear": ("var(--bear)",  "var(--tint-bear)",  "🔴"),
    "risk": ("var(--amber)", "var(--tint-amber)", "⚠"),
}
_BBR_COL_TITLE = {"bull": "Bull case", "bear": "Bear case", "risk": "Key risks"}
_BBR_COL_EMPTY = {
    "bull": "No bull factors triggered.",
    "bear": "No bear factors triggered.",
    "risk": "No specific risks flagged.",
}


def _bbr_factor_chip(text: str, source: str, evidence: str, rail: str) -> str:
    """One factor row inside a bull/bear/risk column."""
    source_pill = (
        f'<span style="display:inline-block;background:var(--sunken);'
        f'border:1px solid var(--hairline);border-radius:4px;'
        f'padding:1px 6px;font-size:10px;color:var(--dim);'
        f'font-weight:600;letter-spacing:.04em">{source}</span>'
        if source else ""
    )
    evidence_html = (
        f'<span style="margin-left:6px;font-size:11px;color:var(--dim);'
        f'font-family:var(--font-mono)">{evidence}</span>'
        if evidence else ""
    )
    return (
        f'<div style="background:var(--sunken);border-left:3px solid {rail};'
        f'border-radius:6px;padding:7px 10px;margin:5px 0">'
        f'<div style="font-size:12.5px;line-height:1.4;color:var(--ink-mid)">'
        f'{text}</div>'
        f'<div style="margin-top:4px">{source_pill}{evidence_html}</div>'
        f'</div>'
    )


def _bbr_column(kind: str, factors: list, limit: int = 4) -> str:
    """One of the three columns (bull | bear | risk)."""
    rail, tint, icon = _BBR_COL_STYLES[kind]
    title = _BBR_COL_TITLE[kind]
    count = len(factors) if factors else 0
    count_badge = (
        f'<span style="display:inline-block;background:{tint};color:{rail};'
        f'border-radius:999px;padding:1px 8px;font-size:11px;font-weight:700;'
        f'font-family:var(--font-mono);margin-left:6px">{count}</span>'
    )
    header = (
        f'<div style="display:flex;align-items:center;margin-bottom:8px">'
        f'<span style="color:{rail};font-weight:700;letter-spacing:.12em;'
        f'text-transform:uppercase;font-size:11px">'
        f'{icon} {title}</span>{count_badge}</div>'
    )
    if not factors:
        body = (
            f'<div style="font-size:12px;color:var(--faint);font-style:italic;'
            f'padding:8px 4px">{_BBR_COL_EMPTY[kind]}</div>'
        )
    else:
        shown = factors[:limit]
        rows = "".join(
            _bbr_factor_chip(
                _get(_f, "text", ""), _get(_f, "source", ""),
                _get(_f, "evidence", ""), rail,
            )
            for _f in shown
        )
        overflow = ""
        if len(factors) > limit:
            overflow = (
                f'<div style="font-size:11px;color:var(--dim);text-align:right;'
                f'margin-top:4px">+{len(factors) - limit} more in Thesis tab</div>'
            )
        body = rows + overflow
    return (
        f'<div style="background:var(--card-lift);border:1px solid var(--hairline);'
        f'border-radius:var(--r-base);padding:12px 14px;height:100%">'
        f'{header}{body}</div>'
    )


def _get(obj, key: str, default=""):
    """Read `key` off either a Factor dataclass (attribute) or a plain dict."""
    if hasattr(obj, key):
        return getattr(obj, key, default) or default
    if isinstance(obj, dict):
        return obj.get(key, default) or default
    return default


def bull_bear_risk_card(bull_factors: list,
                        bear_factors: list,
                        key_risks: list,
                        limit_per_column: int = 4) -> str:
    """Three-column hero-tier Bull / Bear / Risk card (IR1).

    Each list holds either Factor dataclass instances (from analysis.thesis)
    or plain dicts with 'text' / 'source' / 'evidence' keys. Returns raw
    HTML -- caller stamps via st.markdown(unsafe_allow_html=True).

    limit_per_column caps the visible factors per column; overflow gets a
    "+N more in Thesis tab" hint so the card stays scannable when a stock
    has ~12 bull triggers.
    """
    return (
        f'<div style="display:grid;grid-template-columns:1fr 1fr 1fr;'
        f'gap:12px;margin:10px 0">'
        f'{_bbr_column("bull", bull_factors or [], limit_per_column)}'
        f'{_bbr_column("bear", bear_factors or [], limit_per_column)}'
        f'{_bbr_column("risk", key_risks     or [], limit_per_column)}'
        f'</div>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Mockup layout kit (2026-09-12 "proposed layout" artifact)
# String builders for the .sec-hd / .gate-strip / .sig-table / .rank-chip /
# .fu-card classes in design.py. Callers stamp the result with
# st.markdown(unsafe_allow_html=True). Cell / value arguments are raw HTML
# so chips can nest; escape user or feed text before passing it in.
# ─────────────────────────────────────────────────────────────────────────────

def section_header(title: str, aside: str = "") -> str:
    """Serif section title with an optional mono provenance note on the right.

    A div with role="heading", not an <h3>: Streamlit's markdown heading
    rules outrank the class (they forced Source Sans 28/600) and add a
    hover anchor link.
    """
    aside_html = f'<span class="sec-hd-a">{aside}</span>' if aside else ""
    return (f'<div class="sec-hd"><div class="sec-hd-t" role="heading" aria-level="3">'
            f"{title}</div>{aside_html}</div>")


def gate_strip(cells: list) -> str:
    """Summary ribbon of (label, value_html, sub) tuples.

    Capped at five cells: the §4.1 five-metric ceiling. A sixth cell is
    dropped rather than wrapped, so callers must choose what to show.
    """
    parts = []
    for label, value, sub in list(cells)[:5]:
        sub_html = f'<span class="gate-s">{sub}</span>' if sub else ""
        parts.append(
            f'<div class="gate-cell"><span class="gate-k">{label}</span>'
            f'<span class="gate-v">{value}</span>{sub_html}</div>'
        )
    return f'<div class="gate-strip">{"".join(parts)}</div>'


def rank_chip(rank: int) -> str:
    """Square rank badge. Ranks 1-3 carry the accent in falling strength."""
    tier = f" r{rank}" if 1 <= rank <= 3 else ""
    return f'<span class="rank-chip{tier}">{rank}</span>'


def signal_table(columns: list, rows: list) -> str:
    """Ranked data table.

    columns: list of (header, align) where align is "l" or "r".
             Right-aligned columns render in the mono face.
    rows:    list of lists of cell HTML, one entry per column.
    """
    aligns = [a for _, a in columns]
    head = "".join(
        f'<th class="{a}">{h}</th>' if a == "r" else f"<th>{h}</th>"
        for h, a in columns
    )
    body = "".join(
        "<tr>" + "".join(
            f'<td class="{aligns[i]}">{c}</td>' if aligns[i] == "r" else f"<td>{c}</td>"
            for i, c in enumerate(row)
        ) + "</tr>"
        for row in rows
    )
    return (f'<div class="sig-table-wrap"><table class="sig-table">'
            f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")


def follow_card(rank: int, symbol: str, sub: str, rows: list,
                foot_html: str = "", tone: str = "") -> str:
    """Compact follow-through card.

    rows: list of (key, value_html) pairs shown under a dotted rule.
    tone: "good" / "bad" tints the card; anything else stays neutral.
    """
    tone_cls = f" {tone}" if tone in ("good", "bad") else ""
    rows_html = "".join(
        f'<div class="fu-row"><span class="fu-k">{k}</span><span class="fu-v">{v}</span></div>'
        for k, v in rows
    )
    foot = f'<div class="fu-foot">{foot_html}</div>' if foot_html else ""
    return (
        f'<div class="fu-card{tone_cls}"><div class="fu-rank">RANK {rank:02d}</div>'
        f'<div class="fu-sym">{symbol}</div><div class="fu-sub">{sub}</div>'
        f"{rows_html}{foot}</div>"
    )


def share_bars(rows: list, fill: str = "var(--accent)", absolute: bool = False) -> str:
    """Horizontal share bars: rows of (label, pct 0-100, value_text[, fill]).

    Bar length is the share of the largest row, so the biggest item always
    spans the track; the printed value stays the true percentage. With
    ``absolute=True`` the track is 0-100 instead (breadth percentages).
    """
    rows = list(rows)
    peak = 100.0 if absolute else (max((float(r[1]) for r in rows), default=0.0) or 1.0)
    out = []
    for r in rows:
        label, pct, text = r[0], float(r[1]), r[2]
        colour = r[3] if len(r) > 3 else fill
        out.append(
            f'<div class="alloc-row"><span class="alloc-name">{label}</span>'
            f'<div class="alloc-bar"><div class="alloc-fill" '
            f'style="width:{max(pct / peak * 100, 1.5):.1f}%;background:{colour}"></div></div>'
            f'<span class="alloc-val">{text}</span></div>'
        )
    return f'<div class="alloc">{"".join(out)}</div>'


def quiet_output(label: str, lead: str, unit: str = "", sub: str = "",
                 rows: Optional[list] = None, note: str = "") -> str:
    """Sunken output panel for a one-calculation tool (Position Sizer).

    One big serif number, a mono sub-line, then a dotted key/value list of
    (label, value) rows and an optional muted footnote.
    """
    unit_html = f'<span class="quiet-lead-u">{unit}</span>' if unit else ""
    sub_html = f'<div class="quiet-lead-sub">{sub}</div>' if sub else ""
    kv = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in (rows or []))
    kv_html = f'<dl class="quiet-kv">{kv}</dl>' if kv else ""
    note_html = f'<div class="quiet-note">{note}</div>' if note else ""
    return (f'<div class="quiet-output"><div class="quiet-out-lbl">{label}</div>'
            f'<div class="quiet-lead">{lead}{unit_html}</div>{sub_html}{kv_html}{note_html}</div>')


def breadth_gauge(share: float, left: str = "weak", right: str = "strong") -> str:
    """Half-circle gauge (bear / amber / bull thirds) with a needle at
    ``share`` (0-1). Returns inline SVG sized by the .breadth-gauge class."""
    try:
        share = min(max(float(share), 0.0), 1.0)
    except (TypeError, ValueError):
        share = 0.5
    arc = "M 20 96 A 80 80 0 0 1 180 96"
    seg = 83.78  # one third of the 251.3 arc length
    parts = [f'<path d="{arc}" fill="none" stroke="var(--sunken)" stroke-width="10"/>']
    for i, tone in enumerate(("bear", "amber", "bull")):
        parts.append(f'<path d="{arc}" fill="none" stroke="var(--{tone})" stroke-width="10" '
                     f'stroke-dasharray="{seg:.2f} 251.3" stroke-dashoffset="{-seg * i:.2f}"/>')
    angle = -90 + 180 * share
    parts.append(f'<g transform="translate(100,96) rotate({angle:.1f})">'
                 '<line x1="0" y1="0" x2="0" y2="-70" stroke="var(--ink)" stroke-width="2" '
                 'stroke-linecap="round"/><circle r="5" fill="var(--ink)"/></g>')
    parts.append(f'<text x="12" y="112" class="breadth-gauge-t">{left}</text>'
                 f'<text x="188" y="112" text-anchor="end" class="breadth-gauge-t">{right}</text>')
    return ('<svg class="breadth-gauge" viewBox="0 0 200 116" role="img" '
            f'aria-label="{share * 100:.0f}% advancing">{"".join(parts)}</svg>')


def flow_bars(sessions: list) -> str:
    """Paired FII / DII net-flow bars per session.

    sessions: list of (axis_label, fii_net, dii_net) oldest first, in any
    unit. Bar height is relative to the largest absolute value; green is a
    net-buy day, red net-sell. Missing values render as an empty slot.
    """
    vals = [abs(v) for _, f, d in sessions for v in (f, d) if v is not None and v == v]
    peak = max(vals, default=0.0) or 1.0

    def _bar(v, who):
        if v is None or v != v:
            return f'<div class="flow-b flow-{who}" style="height:0"></div>'
        tone = "bull" if v >= 0 else "bear"
        return (f'<div class="flow-b flow-{who}" title="{who.upper()} {v:+,.0f}" '
                f'style="height:{max(abs(v) / peak * 100, 2):.0f}%;background:var(--{tone})"></div>')

    cols = "".join(f'<div class="flow-col">{_bar(f, "fii")}{_bar(d, "dii")}</div>'
                   for _, f, d in sessions)
    axis = "".join(f"<span>{lbl}</span>" for lbl, _, _ in sessions)
    return f'<div class="flow-bars">{cols}</div><div class="flow-axis">{axis}</div>'


def level_strip(cells: list, highlight: int = -1) -> str:
    """Six-cell key-levels ribbon: (label, value, sub) tuples.

    Unlike gate_strip this allows six cells (the intraday artboard shows
    open, prev close, VWAP, ORB high/low, day range); extra cells drop.
    """
    out = []
    cells = list(cells)[:6]
    for i, (label, value, sub) in enumerate(cells):
        cls = "lc hi" if i == highlight else "lc"
        sub_html = f'<div class="lc-s">{sub}</div>' if sub else ""
        out.append(f'<div class="{cls}"><div class="lc-k">{label}</div>'
                   f'<div class="lc-v">{value}</div>{sub_html}</div>')
    # Fewer than six cells share the full width instead of leaving a gap.
    cols = "" if len(cells) == 6 else (
        f' style="grid-template-columns:repeat({max(len(cells), 1)}, minmax(0, 1fr))"')
    return f'<div class="level-strip"{cols}>{"".join(out)}</div>'


def depth_ladder(depth: dict) -> str:
    """Five-level bid/ask ladder from angel_fetcher.get_market_depth().

    Sells print highest price first above the spread row, buys below.
    Bar length is each level's qty relative to the largest level shown.
    """
    buys = [lv for lv in (depth.get("buys") or []) if lv.get("price")]
    sells = [lv for lv in (depth.get("sells") or []) if lv.get("price")]
    peak = max((lv.get("qty", 0) for lv in buys + sells), default=0) or 1

    def _row(lv, side):
        w = lv.get("qty", 0) / peak * 100
        return (f'<div class="depth-row"><span class="d-bar {side}" style="width:{w / 2:.0f}%"></span>'
                f'<span class="d-qty">{lv.get("qty", 0):,}</span>'
                f'<span class="d-price {side}">{lv["price"]:,.2f}</span>'
                f'<span class="d-ord">{lv.get("orders", 0)}</span></div>')

    sells_html = "".join(_row(lv, "sell") for lv in sorted(sells, key=lambda x: -x["price"]))
    buys_html = "".join(_row(lv, "buy") for lv in sorted(buys, key=lambda x: -x["price"]))
    bid = max((lv["price"] for lv in buys), default=None)
    ask = min((lv["price"] for lv in sells), default=None)
    if bid is not None and ask is not None:
        spread = (f'<div class="depth-spread">bid <b>{bid:,.2f}</b> · ask <b>{ask:,.2f}</b>'
                  f' · spread <b>₹{ask - bid:,.2f}</b></div>')
    else:
        spread = '<div class="depth-spread">one side of the book is empty</div>'
    return ('<div class="depth-ladder"><div class="depth-head"><span>Qty</span><span>Price</span>'
            f'<span>Orders</span></div>{sells_html}{spread}{buys_html}</div>')


# ── Analyze Stock (artboard 02) ─────────────────────────────────────────────

def stock_head(symbol: str, company: str = "", tags: Optional[list] = None,
               price: Optional[float] = None, chg: Optional[float] = None,
               chg_pct: Optional[float] = None, note: str = "") -> str:
    """Ticker identity (serif symbol, company line, tag chips) with the price
    block on the right. The action buttons are Streamlit widgets, so the
    page renders them in a column beside this string."""
    tags_html = "".join(chip_tag(t) for t in (tags or []) if t)
    tags_html = f'<div class="sh-tags">{tags_html}</div>' if tags_html else ""
    co_html = f'<span class="sh-co">{company}</span>' if company else ""
    price_html = ""
    if price:
        whole, _, paise = f"{price:,.2f}".partition(".")
        bits = []
        if chg is not None:
            bits.append(chip_delta(chg, unit=""))
        if chg_pct is not None:
            bits.append(chip_delta(chg_pct))
        row = f'<div class="sh-row">{"".join(bits)}</div>' if bits else ""
        note_html = f'<div class="sh-note">{note}</div>' if note else ""
        price_html = (
            f'<div class="sh-price"><div class="sh-now"><span class="sh-ccy">₹</span>{whole}'
            f'<span class="sh-paise">.{paise}</span></div>{row}{note_html}</div>'
        )
    return (f'<div class="stock-hd"><div class="sh-id"><span class="sh-sym">{symbol}</span>'
            f'{co_html}{tags_html}</div>{price_html}</div>')


def kv_list(rows: list, note: str = "") -> str:
    """Dotted key/value list: (label, value_html[, highlight]) rows.

    Highlighted rows carry an accent tint (the sector-aware metrics on the
    fundamentals card). ``note`` renders as an accent-railed footnote.
    """
    parts = []
    for r in rows:
        hi = ' class="hi"' if len(r) > 2 and r[2] else ""
        parts.append(f"<dt{hi}>{r[0]}</dt><dd{hi}>{r[1]}</dd>")
    note_html = f'<div class="kv-note">{note}</div>' if note else ""
    return f'<dl class="kv">{"".join(parts)}</dl>{note_html}'


def kit_card(title: str, body_html: str, sub: str = "") -> str:
    """Panel with a title bar and a padded body (``.kit-panel`` shell)."""
    sub_html = f'<span class="kit-panel-s">{sub}</span>' if sub else ""
    return (f'<div class="kit-panel"><div class="kit-panel-hd"><span class="kit-panel-t">{title}</span>'
            f'{sub_html}</div><div class="kit-card-b">{body_html}</div></div>')


def thesis_grid(bull: list, bear: list, risk: list,
                verdict_label: str = "", verdict_text: str = "", limit: int = 3) -> str:
    """Bull / Bear / Risk blocks with a full-width descriptive verdict block.

    Each list holds Factor dataclasses or dicts with a 'text' key; lists are
    capped at ``limit`` with a "+N more in the Thesis tab" line.
    """
    def _block(kind: str, label: str, factors: list, empty: str) -> str:
        items = [str(_get(f, "text", "")) for f in (factors or [])]
        items = [t for t in items if t]
        if items:
            body = "<ul>" + "".join(f"<li>{t}</li>" for t in items[:limit]) + "</ul>"
            if len(items) > limit:
                body += f'<div class="th-more">+{len(items) - limit} more in the Thesis tab</div>'
        else:
            body = f'<div class="th-empty">{empty}</div>'
        return f'<div class="th-block {kind}"><div class="th-lbl">{label}</div>{body}</div>'

    verdict = ""
    if verdict_label or verdict_text:
        verdict = (f'<div class="th-block verd"><div class="th-lbl">Verdict · descriptive'
                   f'{" · " + verdict_label if verdict_label else ""}</div>'
                   f'<p class="th-body">{verdict_text}</p></div>')
    return ('<div class="thesis-grid">'
            + _block("bull", "✓ Bull", bull, "No bull factors triggered.")
            + _block("bear", "✕ Bear", bear, "No bear factors triggered.")
            + _block("risk", "△ Risk", risk, "No specific risks flagged.")
            + verdict + "</div>")


# ── Live Ticker (artboard 06) ───────────────────────────────────────────────

def fmt_volume(v) -> str:
    """Traded quantity in Indian units: 2.42Cr, 81L, 4.8K. "—" when unknown."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if not v or v != v:
        return "—"
    if v >= 1e7:
        return f"{v / 1e7:.2f}Cr"
    if v >= 1e5:
        return f"{v / 1e5:.1f}L".replace(".0L", "L")
    if v >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:,.0f}"


def sparkline(values: list, tone: str = "") -> str:
    """Inline SVG line over ``values`` (oldest first) with an end dot.

    tone: "up" / "down" colours it bull / bear; otherwise it follows the
    series' own direction (last vs first).
    """
    pts = [float(v) for v in (values or []) if v is not None and v == v]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    w, h, pad = 260.0, 36.0, 3.0
    xy = [(i / (len(pts) - 1) * w, pad + (hi - p) / span * (h - 2 * pad)) for i, p in enumerate(pts)]
    d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    up = (tone == "up") if tone in ("up", "down") else pts[-1] >= pts[0]
    colour = "var(--bull)" if up else "var(--bear)"
    ex, ey = xy[-1]
    return (f'<svg class="mini-spark" viewBox="0 0 {w:.0f} {h:.0f}" preserveAspectRatio="none" '
            f'aria-hidden="true"><path d="{d}" fill="none" stroke="{colour}" stroke-width="1.5" '
            f'vector-effect="non-scaling-stroke"/><circle cx="{ex:.1f}" cy="{ey:.1f}" r="2.4" '
            f'fill="{colour}"/></svg>')


def mini_index(name: str, value: float, chg: float, chg_pct: float,
               spark: Optional[list] = None, invert: bool = False) -> str:
    """Right-rail index card: name, mono level, change line, sparkline.

    invert=True for fear gauges (India VIX), where a rise reads as risk-off,
    so the change and sparkline colour flip.
    """
    rising = chg >= 0
    good = (not rising) if invert else rising
    colour = "var(--bull)" if good else "var(--bear)"
    arrow = "▲" if rising else "▼"
    spark_html = sparkline(spark or [], tone="up" if good else "down")
    return (f'<div class="mini-index"><div class="mi-name">{name}</div>'
            f'<div class="mi-val">{value:,.2f}</div>'
            f'<div class="mi-sub" style="color:{colour}">{arrow} {chg:+,.2f} · {chg_pct:+.2f}%</div>'
            f"{spark_html}</div>")


def ticker_table(rows: list) -> str:
    """Dense live-price table: ticker (+ sector), LTP, Δ, Δ%, volume, day
    high / low and VWAP, in a fixed-height scroll panel with a sticky header.

    rows: dicts with sym, sub, price, chg, chg_pct and optional volume,
    high, low, vwap, flash ("up" / "down" flashes the row once).
    """
    def _px(v):
        return f"{v:,.2f}" if isinstance(v, (int, float)) and v and v == v else "—"

    body = []
    for r in rows:
        pct = float(r.get("chg_pct") or 0.0)
        cls = "tk-up" if pct > 0.05 else "tk-dn" if pct < -0.05 else "tk-flat"
        flash = {"up": ' class="tk-flash-up"', "down": ' class="tk-flash-dn"'}.get(r.get("flash"), "")
        vwap = r.get("vwap")
        vwap_html = _px(vwap)
        if vwap and r.get("price"):
            side = "tk-up" if r["price"] >= vwap else "tk-dn"
            vwap_html = f'<span class="{side}">{vwap_html}</span>'
        body.append(
            f"<tr{flash}><td><span class=\"sym\">{r['sym']}</span>"
            f"<span class=\"co\">{r.get('sub') or ''}</span></td>"
            f"<td>{_px(r.get('price'))}</td>"
            f'<td class="{cls}">{float(r.get("chg") or 0.0):+,.2f}</td>'
            f'<td class="{cls}">{pct:+.2f}%</td>'
            f"<td>{fmt_volume(r.get('volume'))}</td>"
            f"<td>{_px(r.get('high'))}</td><td>{_px(r.get('low'))}</td>"
            f"<td>{vwap_html}</td></tr>"
        )
    head = ("<tr><th>Ticker</th><th>LTP</th><th>Δ</th><th>Δ %</th><th>Volume</th>"
            "<th>Day high</th><th>Day low</th><th>VWAP</th></tr>")
    return (f'<div class="ticker-panel"><table class="ticker-table"><thead>{head}</thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


# ── Angel One (artboard 11) ─────────────────────────────────────────────────

def status_panel(label: str, tone: str, headline: str, hint: str = "",
                 stats: Optional[list] = None) -> str:
    """Quiet connection-status panel: dot + label, serif headline, muted
    hint, then a grid of (label, value_html, sub) stat tiles.

    tone: good / warn / bad / neutral colours the dot and label.
    """
    colour = {"good": "var(--bull)", "warn": "var(--amber)",
              "bad": "var(--bear)"}.get(tone, "var(--dim)")
    tiles = "".join(
        f'<div class="ao-stat"><div class="ao-stat-k">{k}</div><div class="ao-stat-v">{v}'
        + (f'<span class="ao-stat-s">{s}</span>' if s else "") + "</div></div>"
        for k, v, s in (stats or [])
    )
    hint_html = f'<p class="ao-hint">{hint}</p>' if hint else ""
    grid = f'<div class="ao-stats">{tiles}</div>' if tiles else ""
    return (f'<div class="ao-status"><div class="ao-line"><span class="ao-dot" '
            f'style="background:{colour}"></span><span class="ao-label" style="color:{colour}">'
            f'{label}</span></div><div class="ao-h" role="heading" aria-level="3">{headline}</div>'
            f"{hint_html}{grid}</div>")


def activity_log(events: list, empty: str = "No activity recorded yet.") -> str:
    """Event list: dicts with ts (epoch s), kind, detail, tone. Newest first."""
    import datetime as _dt
    if not events:
        return f'<div class="ao-log-empty">{empty}</div>'
    label = {"good": "OK", "bad": "Failed", "warn": "Warn"}
    rows = []
    for e in events:
        when = _dt.datetime.fromtimestamp(float(e.get("ts") or 0)).strftime("%H:%M")
        tone = e.get("tone", "neutral")
        rows.append(
            f'<li><span class="ao-lt">{when}</span><div><span class="ao-lk">{e.get("kind", "")}</span>'
            f'<div class="ao-ls">{e.get("detail", "")}</div></div>'
            f'{chip_pill(label.get(tone, "info"), tone if tone in ("good", "bad", "warn") else "neutral")}</li>'
        )
    return f'<ul class="ao-log">{"".join(rows)}</ul>'

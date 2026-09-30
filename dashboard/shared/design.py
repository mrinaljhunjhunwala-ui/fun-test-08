"""dashboard/shared/design.py - NSE Pro CSS theme, Plotly template, UI helpers.

── DESIGN TOKENS (NSE Pro v2 — "Dealing Room") ───────────────────────────────
Reference point: the black-and-phosphor heritage of Bloomberg/Reuters terminals
rather than a generic dark-mode SaaS dashboard. One accent (warm rust, #c88a3f since the 2026-09 palette) carries
all interactive/brand chrome; green and red are reserved strictly for
buy/sell semantics so they never compete with the UI for attention.

  Surface   ink #0e1116 · surface #171b22 · sunken #12151b · hairline rgba(255,255,255,.08)
  Text      primary #e8e6e0 · dim #8b8e97 · faint #5f636c
  Signal    bull #6faa84 · bear #c77070 · caution #d6a64c · accent #c88a3f
  Type      display/UI: Inter · data/numeric: JetBrains Mono
  Radius    sharp 6px (tables/inputs/tape) · base 10px (cards/metrics) · soft 18px (hero panels)

Public API (function names + CSS class names) is unchanged from v1 — 13 page
files reference these directly. Only the tokens/values inside change.
────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations
import os
import sys
import warnings
import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

from dashboard.shared.tokens import COLORS as _C, css_vars as _css_vars
# FIX WARN1 — narrowed from a blanket `filterwarnings("ignore")` so numpy's
# RuntimeWarnings (invalid value / divide by zero / all-NaN slice) stay visible.
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# Film-grain noise tile for the page background. Kept out of the CSS literal
# because a data-URI can't be line-wrapped inside its quotes.
_NOISE_SVG_URI = (
    "url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'>"
    "<filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/>"
    "<feColorMatrix values='0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 0.03 0'/></filter>"
    "<rect width='100%25' height='100%25' filter='url(%23n)'/></svg>\")"
)


def apply_design():
    """Register Plotly template + inject CSS. Idempotent across pages."""
    # FIX LAYOUT-WIDE (2026-09-26): with pages/ routing, app.py's
    # set_page_config(layout="wide") is lost after st.switch_page, because
    # each page runs as its own script. Every page rendered in Streamlit's
    # narrow 736px column. Re-assert only the layout here: set_page_config
    # is additive and callable more than once per run since Streamlit 1.44
    # (floor is 1.52), so title and icon from app.py are left alone.
    st.set_page_config(layout="wide")
    if "nse_pro" not in pio.templates:
        pio.templates["nse_pro"] = go.layout.Template(
            layout=dict(
                paper_bgcolor="#0e1116",
                plot_bgcolor="#12151b",
                font=dict(family="Inter, -apple-system, sans-serif", color=_C["dim"], size=12),
                title=dict(font=dict(size=15, color=_C["ink"])),
                xaxis=dict(gridcolor="rgba(255,255,255,0.05)", linecolor="rgba(255,255,255,0.07)",
                           tickfont=dict(color=_C["faint"], size=11), zeroline=False),
                yaxis=dict(gridcolor="rgba(255,255,255,0.05)", linecolor="rgba(255,255,255,0.07)",
                           tickfont=dict(color=_C["faint"], size=11), zeroline=False),
                legend=dict(bgcolor="rgba(18,21,27,0.9)", bordercolor="rgba(255,255,255,0.07)",
                            borderwidth=1, font=dict(color="#8b8e97", size=11)),
                hoverlabel=dict(bgcolor="#171b22", bordercolor="rgba(255,255,255,0.14)",
                                font=dict(color="#e8e6e0", family="JetBrains Mono", size=12)),
                colorway=[_C["accent"], _C["bull"], _C["amber"], _C["bear"], _C["dim"], _C["azure"],
                          _C["violet"], _C["ink"]],
            )
        )
        pio.templates.default = "nse_pro"

    # ── NSE Pro Design System v2 — "Dealing Room" ──────────────────────────────
    st.markdown(
        """<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&family=Instrument+Serif:ital@0;1&display=swap');

    /* ── SPRINT 1.1: Design tokens as CSS custom properties ─────────────────
       Single source of truth for palette, radius, and shadow. Every rule
       below and every page-level inline style SHOULD source colour from
       these vars — audit found ~8 parallel greens (#6faa84, #6faa84,
       #00d4aa, #3ddc84 …) drifting page-to-page. New rule: no raw hex in
       page files. See docs/UI_AUDIT_2026-09.md → Cluster B. ─────────────── */
    :root {
/*__COLOR_TOKENS__*/
      /* Surfaces — §9.2 texture layers (docs/UI_UX_DESIGN_2026-09.md).
         Pure #0e1116 ground lets the rust accent / bull / bear pop with real
         chromatic weight; cards float above it as a 2% white overlay
         (borrowed from NSVisualEffectView) instead of a heavier tinted
         rectangle. Cyan was in an earlier draft; #103 explicitly rejected
         it for saffron as the single interactive hue. */
      --card-lift: rgba(255,255,255,.02);      /* §9.2.2 — floated card fill on ground */

      /* Hairline system — §9.2.3 three widths.
         Semantic mapping: -soft for section dividers (barely there),
         -base for interactive borders (buttons, chips), -strong for
         active-focus (inputs, active nav). Old --hairline stays as an
         alias on -base for callers not yet migrated. */
      --hairline-soft:   rgba(255,255,255,.04);
      --hairline:        rgba(255,255,255,.08);
      --hairline-strong: rgba(255,255,255,.16);

      /* Ink — four densities, §9.3 colour table. */

      /* Signal — reserved strictly for buy/sell/warn semantics */

      /* Accent — one hue for all interactive/brand chrome */

      /* Purple/blue kept for categorical fills only (charts, sector tags) */

      /* Semantic aliases (map onto signal, so pages read intent, not hue) */
      --pos:       var(--bull);
      --neg:       var(--bear);
      --warn:      var(--amber);
      --info:      var(--accent);

      /* Regime colours — same three severities, aliased for narrative */
      --regime-good:  var(--bull);
      --regime-mixed: var(--amber);
      --regime-bad:   var(--bear);
      --regime-none:  var(--faint);

      /* Tinted backgrounds (12% overlays, used on cards + pills) */
      --tint-bull:   rgba(111,170,132,.12);
      --tint-bear:   rgba(199,112,112,.12);
      --tint-amber:  rgba(214,166,76,.12);
      --tint-accent: rgba(200,138,63,.12);
      --tint-violet: rgba(169,139,201,.12);

      /* Radius */
      --r-sharp:  6px;   /* tables, inputs, ticker tape */
      --r-base:   10px;  /* cards, metrics */
      --r-soft:   18px;  /* hero / glass panels */

      /* Type */
      --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      --font-mono: 'JetBrains Mono', 'Courier New', monospace;
      --font-serif: 'Instrument Serif', 'Iowan Old Style', Georgia, serif;

      /* F4 · Motion policy (docs/UI_UX_BACKLOG.md)
         One motion vocabulary for the whole app:
           - ≤180 ms so nothing feels "waited on"
           - ease-out for enter states (fast start, gentle land)
           - prefers-reduced-motion honoured: durations collapse to 0
             via the media query below, so no caller has to guard. */
      --motion-fast:  120ms;   /* hover, chip flip, chevron rotate */
      --motion-base:  180ms;   /* card fade-in, sheet enter */
      --motion-slow:  260ms;   /* hero verdict entry, large panels */
      --ease-out:     cubic-bezier(0.16, 1, 0.3, 1);   /* enter */
      --ease-in-out:  cubic-bezier(0.65, 0, 0.35, 1);  /* toggles */
    }

    /* F4 · reduced-motion guard -- flattens every duration to 0. Applies
       to app CSS, inline styles that use var(--motion-*), and the
       .motion-fade-in / .motion-slide-up utility classes below. Users
       who prefer reduced motion get instant states, no animation. */
    @media (prefers-reduced-motion: reduce) {
      :root {
        --motion-fast: 0ms;
        --motion-base: 0ms;
        --motion-slow: 0ms;
      }
      * {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
      }
    }

    /* F4 · utility classes. Deliberately small surface -- most UI should
       not animate at all. Reserve these for entry moments where a soft
       reveal reads more polished than a hard cut. */
    @keyframes motion-fade-in-kf {
      from { opacity: 0; }
      to   { opacity: 1; }
    }
    @keyframes motion-slide-up-kf {
      from { opacity: 0; transform: translateY(6px); }
      to   { opacity: 1; transform: translateY(0); }
    }
    .motion-fade-in {
      animation: motion-fade-in-kf var(--motion-base) var(--ease-out) both;
    }
    .motion-slide-up {
      animation: motion-slide-up-kf var(--motion-slow) var(--ease-out) both;
    }

    /* ── §9.4 · Typography scale (docs/UI_UX_DESIGN_2026-09.md) ─────────────
       One axis, formalised. Every card / heading / label picks from here so
       Command Centre's 11 px caption stack, Investor Guide's flat 14 px,
       and Analyze Stock's 20 px hero can't drift apart again. */
    .t-display  { font-family: var(--font-sans); font-size: 32px; font-weight: 700;
                  letter-spacing: -0.02em; line-height: 1.05; color: var(--ink); }
    .t-h1       { font-family: var(--font-sans); font-size: 22px; font-weight: 700;
                  letter-spacing: -0.01em; line-height: 1.15; color: var(--ink); }
    .t-h2       { font-family: var(--font-sans); font-size: 16px; font-weight: 600;
                  line-height: 1.3;  color: var(--ink); }
    .t-body     { font-family: var(--font-sans); font-size: 14px; font-weight: 400;
                  line-height: 1.5;  color: var(--ink-mid); }
    .t-label    { font-family: var(--font-sans); font-size: 11px; font-weight: 600;
                  letter-spacing: 0.12em; text-transform: uppercase; color: var(--dim); }
    .t-value    { font-family: var(--font-mono); font-size: 20px; font-weight: 700;
                  letter-spacing: -0.01em; color: var(--ink); font-variant-numeric: tabular-nums; }
    .t-value-sm { font-family: var(--font-mono); font-size: 14px; font-weight: 600;
                  color: var(--ink); font-variant-numeric: tabular-nums; }
    .t-caption  { font-family: var(--font-sans); font-size: 12px; font-weight: 400;
                  line-height: 1.3;  color: var(--dim); }

    /* ── §9.2.4 · Grain overlay (SVG noise at 3% opacity on the body).
       Imperceptible in isolation; stops the "empty dark rectangle" flatness
       that reads as amateur on pure black. Bloomberg terminals had physical
       CRT grain; this is the digital equivalent. Zero runtime cost —
       inline SVG data-URI, single background-image. */

    /* ── Editorial serif · used SPARINGLY on hero moments only ─────────────
       - Page-title-serif on the H1 of anchor pages (Command Centre,
         Analyze Stock, Portfolio, Investor Guide).
       - Verdict-posture-serif on the descriptive posture line inside a
         hero card ("Constructive, with reservations.").
       - Never on data, buttons, or body copy — that's what Plex Sans is
         for. See docs/UI_UX_DESIGN_2026-09.md §4 (cross-cutting rules). */
    .page-title-serif {
        font-family: var(--font-serif) !important;
        font-weight: 400 !important;
        font-size: clamp(32px, 4vw, 44px) !important;
        line-height: 1.05 !important;
        letter-spacing: -0.02em !important;
        color: var(--ink) !important;
        margin: 0 0 6px 0 !important;
        text-wrap: balance;
    }
    .page-title-serif em {
        font-style: italic;
        color: var(--accent);
    }
    .page-subtitle {
        font-size: 13px !important;
        color: var(--dim) !important;
        margin: 0 0 20px 0 !important;
        max-width: 62ch;
        line-height: 1.55;
    }
    .verdict-posture-serif {
        font-family: var(--font-serif) !important;
        font-weight: 400 !important;
        font-size: 32px !important;
        line-height: 1.1 !important;
        letter-spacing: -0.015em !important;
        color: var(--ink) !important;
        margin: 4px 0 8px 0 !important;
    }
    .verdict-posture-serif em {
        font-style: italic;
        color: var(--accent);
    }

    /* ── Hide Streamlit's auto-generated pages/ nav (custom nav lives in
          render_sidebar). Belt-and-suspenders with showSidebarNavigation=false. ── */
    [data-testid="stSidebarNav"] { display: none !important; }

    /* ── Global ──────────────────────────────────────────────────────────────── */
    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
        -webkit-font-smoothing: antialiased;
    }
    .stApp {
        background: var(--ground);
        /* §9.2.1 pure-black ground + §9.2.4 SVG noise grain at 3% opacity +
           the existing saffron aurora at the top-centre. Stacked in a
           single background: shorthand so it stays as one paint pass. */
        background-image:
          radial-gradient(ellipse 90% 40% at 50% -10%, rgba(200,138,63,0.05) 0%, transparent 60%),
          /*__NOISE_SVG__*/;
        background-attachment: fixed;
        background-size: auto, 160px 160px;
    }
    .mono { font-family:'JetBrains Mono','Courier New',monospace !important; font-variant-numeric: tabular-nums; }

    /* ── Cards ───────────────────────────────────────────────────────────────
       Flat tinted surface + solid left rail, not a diagonal two-stop gradient —
       calmer and denser at a glance, closer to a real order-flow ticket. ─────── */
    .card-green, .card-yellow, .card-red, .card-blue, .card-purple, .card-orange {
        border-radius:10px; padding:14px 18px; margin:6px 0;
        transition: border-color .15s ease, background .15s ease;
    }
    .card-green:hover, .card-yellow:hover, .card-red:hover,
    .card-blue:hover, .card-purple:hover, .card-orange:hover {
        background-position: right center;
    }
    .card-green  { background:rgba(111,170,132,.07);  border-left:3px solid #6faa84; }
    .card-yellow { background:rgba(214,166,76,.07);  border-left:3px solid #d6a64c; }
    .card-red    { background:rgba(199,112,112,.07);   border-left:3px solid #c77070; }
    .card-blue   { background:rgba(200,138,63,.06);  border-left:3px solid #c88a3f; }
    .card-purple { background:rgba(169,139,201,.06); border-left:3px solid #a98bc9; }
    .card-orange { background:rgba(214,166,76,.07);  border-left:3px solid #d6a64c; }

    /* ── Score & typography ───────────────────────────────────────────────────── */
    .score-big {
        font-family:'JetBrains Mono',monospace; font-size:54px; font-weight:700; letter-spacing:-1px;
        font-variant-numeric: tabular-nums;
    }
    .signal-big   { font-size:21px; font-weight:700; letter-spacing:.6px; text-transform:uppercase; }
    .narrative    { font-size:14px; line-height:1.75; color:#8b8e97; }
    .ticker-label { font-size:23px; font-weight:700; color:#e8e6e0; letter-spacing:-.2px; }

    /* ── Pills ───────────────────────────────────────────────────────────────── */
    .pill-green {
        display:inline-block; background:rgba(111,170,132,.12); color:#6faa84; border:1px solid rgba(111,170,132,.4);
        border-radius:20px; padding:3px 14px; font-size:12px; font-weight:600;
    }
    .pill-red {
        display:inline-block; background:rgba(199,112,112,.12); color:#c77070; border:1px solid rgba(199,112,112,.4);
        border-radius:20px; padding:3px 14px; font-size:12px; font-weight:600;
    }
    .pill-yellow {
        display:inline-block; background:rgba(214,166,76,.12); color:#d6a64c; border:1px solid rgba(214,166,76,.4);
        border-radius:20px; padding:3px 14px; font-size:12px; font-weight:600;
    }
    .pill-gray {
        display:inline-block; background:rgba(255,255,255,.06); color:#8b8e97; border:1px solid rgba(255,255,255,.14);
        border-radius:20px; padding:3px 14px; font-size:12px;
    }
    .pill-blue {
        display:inline-block; background:rgba(200,138,63,.12); color:#c88a3f; border:1px solid rgba(200,138,63,.4);
        border-radius:20px; padding:3px 14px; font-size:12px; font-weight:600;
    }

    /* ── Signal badges ───────────────────────────────────────────────────────── */
    .badge-buy {
        background:rgba(111,170,132,.14); color:#6faa84; border:1px solid #6faa84; border-radius:6px; padding:4px 14px;
        font-size:13px; font-weight:700; letter-spacing:.5px; display:inline-block;
    }
    .badge-sell {
        background:rgba(199,112,112,.14); color:#c77070; border:1px solid #c77070; border-radius:6px; padding:4px 14px;
        font-size:13px; font-weight:700; letter-spacing:.5px; display:inline-block;
    }
    .badge-hold {
        background:rgba(214,166,76,.14); color:#d6a64c; border:1px solid #d6a64c; border-radius:6px; padding:4px 14px;
        font-size:13px; font-weight:700; letter-spacing:.5px; display:inline-block;
    }
    .badge-watch {
        background:rgba(200,138,63,.14); color:#c88a3f; border:1px solid #c88a3f; border-radius:6px; padding:4px 14px;
        font-size:13px; font-weight:700; letter-spacing:.5px; display:inline-block;
    }

    /* ── Angel One badges ────────────────────────────────────────────────────── */
    .ao-badge-on {
        background:rgba(111,170,132,.08); border:1px solid rgba(111,170,132,.4); border-radius:8px; padding:10px 14px;
        font-size:12px; color:#6faa84; margin:4px 0; display:flex; align-items:center; gap:8px;
    }
    .ao-badge-off {
        background:rgba(255,255,255,.02); border:1px solid rgba(255,255,255,.08); border-radius:8px;
        padding:10px 14px; font-size:12px; color:#5f636c; margin:4px 0; display:block;
    }

    /* ── Streamlit metric override ─────────────────────────────────────────────
       §9.2 · sits on the pure-black ground as a 2% white lift instead of
       a heavier tinted rectangle. Reads as a floated card without the
       chrome. */
    [data-testid="stMetric"] {
        background: var(--card-lift);
        border: 1px solid var(--hairline);
        border-radius: var(--r-base); padding: 14px 18px;
    }
    [data-testid="stMetricValue"] {
        font-family:'JetBrains Mono',monospace; font-weight:700; letter-spacing:-.3px; font-size:20px;
    }
    [data-testid="stMetricLabel"] {
        font-size:11px; color:#5f636c; text-transform:uppercase; letter-spacing:1px; font-weight:600;
    }
    /* Never clip/ellipsis metric text — always show the full value, label and delta */
    [data-testid="stMetric"] { overflow: visible !important; }
    [data-testid="stMetricValue"], [data-testid="stMetricValue"] *,
    [data-testid="stMetricLabel"], [data-testid="stMetricLabel"] *,
    [data-testid="stMetricDelta"], [data-testid="stMetricDelta"] * {
        white-space: normal !important; overflow: visible !important;
        text-overflow: clip !important; max-width: none !important;
    }

    /* ── Buttons ─────────────────────────────────────────────────────────────── */
    .stButton > button {
        border-radius:6px; font-weight:600; letter-spacing:.2px;
        border: 1px solid rgba(255,255,255,.1); transition: all .15s ease;
        background: rgba(255,255,255,.04);
    }
    .stButton > button:hover { border-color: rgba(200,138,63,.5); color:#c88a3f; }
    /* F7 · keyboard focus ring — matches the saffron halo used on inputs so
       "you can act here" reads the same on click, tab-through, and hover.
       :focus-visible fires only on keyboard nav (never on mouse click), so
       pointer users don't see the ring unless they Tab to a button. */
    .stButton > button:focus-visible {
        outline: none !important;
        border-color: var(--accent) !important;
        box-shadow: 0 0 0 3px rgba(200,138,63,.20) !important;
    }
    .stButton > button[kind="primary"] {
        background: #c88a3f; border:none; color:#0e1116; font-weight:700;
    }
    .stButton > button[kind="primary"]:hover { background:#dda35c; color:#0e1116; }
    .stButton > button[kind="primary"]:focus-visible {
        outline: none !important;
        box-shadow: 0 0 0 3px rgba(200,138,63,.30) !important;
    }

    /* ── Tabs ────────────────────────────────────────────────────────────────── */
    .stTabs [data-baseweb="tab-list"] {
        gap: 4px; background: rgba(255,255,255,.02);
        border-radius: 8px; padding: 4px;
        border: 1px solid rgba(255,255,255,.05);
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 6px; padding: 8px 18px; font-weight: 500;
        color: #5f636c; transition: all .15s;
    }
    .stTabs [aria-selected="true"] {
        background: #171b22; font-weight: 700; color: #c88a3f;
    }
    /* F7 · focus-visible ring on tab buttons — keyboard nav only. */
    .stTabs [data-baseweb="tab"]:focus-visible {
        outline: none;
        box-shadow: 0 0 0 2px rgba(200,138,63,.35) inset;
    }

    /* ── Sidebar ─────────────────────────────────────────────────────────────── */
    [data-testid="stSidebar"] {
        background: #0b0e12;
        border-right: 1px solid rgba(255,255,255,.06);
    }
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
        font-size: 13px; color: #8b8e97;
    }

    /* ── SLICE 1 + POLISH · UI/UX 2026-09 · sidebar group headers + nav ──
       Structural polish for the 6-group nav. Group headers flatten to
       hairline dividers with an uppercase eyebrow label. Nav buttons
       drop their default borders + backgrounds to read as list items.
       The active page gets a leading-edge accent pill so it reads at a
       glance. Sourced from --accent (saffron #c88a3f). See
       docs/UI_UX_DESIGN_2026-09.md §3. */
    [data-testid="stSidebar"] [data-testid="stExpander"] {
        background: transparent !important;
        border: 0 !important;
        border-bottom: 1px solid rgba(255,255,255,.06) !important;
        border-radius: 0 !important;
        margin: 0 !important;
    }
    /* Kill Streamlit's expanded-state purple tint on the summary/details */
    [data-testid="stSidebar"] [data-testid="stExpander"] details,
    [data-testid="stSidebar"] [data-testid="stExpander"] details[open],
    [data-testid="stSidebar"] [data-testid="stExpander"] summary,
    [data-testid="stSidebar"] [data-testid="stExpander"] summary:hover,
    [data-testid="stSidebar"] [data-testid="stExpander"] summary:focus {
        background: transparent !important;
        box-shadow: none !important;
    }
    [data-testid="stSidebar"] [data-testid="stExpander"] summary {
        padding: 10px 12px 8px !important;
    }
    [data-testid="stSidebar"] [data-testid="stExpander"] summary p,
    [data-testid="stSidebar"] [data-testid="stExpander"] summary span {
        font-size: 10px !important;
        text-transform: uppercase;
        letter-spacing: .14em;
        color: #8b8e97 !important;
        font-weight: 600 !important;
    }
    /* Nav buttons — flatten to list-item feel, keep only left padding */
    [data-testid="stSidebar"] .stButton > button {
        background: transparent !important;
        border: 0 !important;
        border-left: 3px solid transparent !important;
        border-radius: 0 4px 4px 0 !important;
        padding-left: 13px !important;
        text-align: left !important;
        color: #c3c1ba !important;
        font-weight: 500 !important;
        transition: background .12s ease, color .12s ease, border-color .12s ease !important;
    }
    [data-testid="stSidebar"] .stButton > button:hover {
        background: rgba(200,138,63,.06) !important;
        color: #e8e6e0 !important;
        border-left-color: rgba(200,138,63,.35) !important;
    }
    /* Active page: leading-edge saffron pill on the disabled (current) button.
       §9.2 optional glow layer — 12 px soft saffron halo at 22% opacity so
       the active-page pill reads as lit rather than just tinted. Never on
       hover (would fire on every mouse move); only on the current page. */
    [data-testid="stSidebar"] .stButton > button:disabled {
        background: linear-gradient(90deg, rgba(200,138,63,.14), transparent 60%) !important;
        color: #e8e6e0 !important;
        border: 0 !important;
        border-left: 3px solid #c88a3f !important;
        border-radius: 0 4px 4px 0 !important;
        padding-left: 13px !important;
        text-align: left !important;
        opacity: 1 !important;
        cursor: default !important;
        box-shadow: 0 0 12px 0 rgba(200,138,63,.22) !important;
    }
    [data-testid="stSidebar"] .stButton > button:disabled:hover {
        color: #e8e6e0 !important;
        border-color: #c88a3f !important;
        background: linear-gradient(90deg, rgba(200,138,63,.14), transparent 60%) !important;
    }

    /* ── Selectbox / inputs ──────────────────────────────────────────────────── */
    [data-baseweb="select"] > div:first-child {
        background: #12151b; border-color: rgba(255,255,255,.1) !important;
        border-radius: 6px;
    }
    .stTextInput > div > div > input {
        background: #12151b; border-color: rgba(255,255,255,.1);
        border-radius: 6px; color: #e8e6e0; font-family:'JetBrains Mono',monospace;
    }
    .stNumberInput > div > div > input {
        background: #12151b; border-color: rgba(255,255,255,.1);
        border-radius: 6px; color: #e8e6e0; font-family:'JetBrains Mono',monospace;
    }
    /* §9.2 · focus-visible glow — only on the input actually being keyed,
       never on hover. Matches the active-nav pill's saffron halo so the
       whole app teaches the same "you can act here" affordance. */
    .stTextInput > div > div > input:focus-visible,
    .stNumberInput > div > div > input:focus-visible,
    [data-baseweb="select"] > div:first-child:focus-within {
        border-color: var(--accent) !important;
        box-shadow: 0 0 0 3px rgba(200,138,63,.15) !important;
        outline: none !important;
    }

    /* ── F3 · widget parity — Paper Trades / Intraday / Angel One ──────────────
       Streamlit ships default (light) chrome for the widgets below. Before F3
       those defaults bled through the dark ground on the three pages that use
       them heavily. Same vocabulary as stTextInput/stNumberInput above:
       card-lift fill, hairline border, saffron accent on focus/checked. */

    /* text_area — Intraday ORB ticker list. */
    .stTextArea textarea {
        background: #12151b !important;
        border-color: rgba(255,255,255,.1) !important;
        border-radius: 6px;
        color: var(--ink) !important;
        font-family: var(--font-mono);
    }
    .stTextArea textarea:focus-visible {
        border-color: var(--accent) !important;
        box-shadow: 0 0 0 3px rgba(200,138,63,.15) !important;
        outline: none !important;
    }

    /* slider — Intraday scan range. */
    .stSlider [data-baseweb="slider"] [role="slider"] {
        background: var(--accent) !important;
        border-color: var(--accent) !important;
        box-shadow: 0 0 0 3px rgba(200,138,63,.12);
    }
    .stSlider [data-baseweb="slider"] > div > div:first-child {
        background: var(--accent) !important;
    }
    .stSlider [data-baseweb="slider"] > div > div:last-child {
        background: rgba(255,255,255,.12) !important;
    }

    /* radio + checkbox + toggle — Paper Trades / Angel One controls. */
    .stRadio label, .stCheckbox label {
        color: var(--ink-mid) !important;
    }
    .stRadio [data-baseweb="radio"] [role="radio"][aria-checked="true"] > div,
    .stRadio [data-baseweb="radio"] [aria-checked="true"] {
        border-color: var(--accent) !important;
        background: var(--accent) !important;
    }
    .stCheckbox [data-baseweb="checkbox"] [aria-checked="true"] {
        background: var(--accent) !important;
        border-color: var(--accent) !important;
    }
    .stRadio [role="radio"]:focus-visible,
    .stCheckbox [role="checkbox"]:focus-visible {
        outline: 3px solid rgba(200,138,63,.25) !important;
        outline-offset: 2px;
    }
    /* toggle — Streamlit renders this as a BaseWeb switch. */
    [data-baseweb="switch"][aria-checked="true"] > div:first-child {
        background: var(--accent) !important;
    }
    [data-baseweb="switch"] > div:first-child > div {
        background: var(--ink) !important;
    }

    /* download_button — matches stButton chrome (Streamlit renders it as a
       separate testid otherwise the primary/secondary rules don't reach it). */
    [data-testid="stDownloadButton"] button {
        border-radius: 10px; font-weight: 600; letter-spacing: .2px;
        border: 1px solid rgba(255,255,255,.08);
        background: rgba(255,255,255,.04);
        color: var(--ink);
        transition: all .15s ease;
    }
    [data-testid="stDownloadButton"] button:hover {
        border-color: rgba(200,138,63,.5); color: var(--accent);
    }
    [data-testid="stDownloadButton"] button:focus-visible {
        outline: 3px solid rgba(200,138,63,.25) !important;
        outline-offset: 2px;
    }

    /* ── Expanders ─────────────────────────────────────────────────────────────
       §9.2 · same card-lift + hairline pairing as .glass-panel and stMetric,
       so a collapsed "Data health" tile reads as part of the same surface
       vocabulary rather than a Streamlit default. */
    [data-testid="stExpander"] {
        background: var(--card-lift);
        border: 1px solid var(--hairline) !important;
        border-radius: var(--r-base);
    }

    /* ── DataFrames ──────────────────────────────────────────────────────────── */
    [data-testid="stDataFrame"] { border-radius: 6px; overflow: hidden; }
    [data-testid="stDataFrame"] thead th {
        background: #12151b !important; color: #5f636c !important;
        font-size: 11px; text-transform: uppercase; letter-spacing: .8px;
        font-weight: 600; border-bottom: 1px solid rgba(255,255,255,.07) !important;
    }
    [data-testid="stDataFrame"] tbody td {
        color: #c3c1ba !important; font-family:'JetBrains Mono',monospace; font-size:13px;
    }
    [data-testid="stDataFrame"] tbody tr:hover td { background: rgba(200,138,63,.05) !important; }

    /* ── Order form ──────────────────────────────────────────────────────────── */
    .order-buy {
        background:rgba(111,170,132,.06); border:1px solid rgba(111,170,132,.3); border-radius:10px; padding:18px;
    }
    .order-sell {
        background:rgba(199,112,112,.06); border:1px solid rgba(199,112,112,.3); border-radius:10px; padding:18px;
    }

    /* ── Custom metric box ─────────────────────────────────────────────────────
       §9.2 · card-lift + hairline pairing. Label size / letter-spacing
       aligned with .t-label so the two vocabularies read as one. */
    .metric-box {
        background: var(--card-lift); border-radius: var(--r-base); padding:16px; text-align:center;
        border:1px solid var(--hairline);
    }
    .metric-val {
        font-family: var(--font-mono); font-size:27px; font-weight:700; margin:4px 0; letter-spacing:-.3px;
        color: var(--ink);
    }
    .metric-lbl {
        font-size:11px; color: var(--dim); text-transform:uppercase; letter-spacing:.12em; font-weight:600;
    }
    .metric-delta-pos { color: var(--bull); font-size:13px; font-weight:600; font-family: var(--font-mono); }
    .metric-delta-neg { color: var(--bear); font-size:13px; font-weight:600; font-family: var(--font-mono); }

    /* ── Section divider ─────────────────────────────────────────────────────── */
    .sec-div { display:flex; align-items:center; gap:12px; margin:28px 0 18px; }
    .sec-div-label {
        font-size:11px; font-weight:700; color:#5f636c; text-transform:uppercase; letter-spacing:1.5px;
        white-space:nowrap;
    }
    .sec-div-line  { flex:1; height:1px; background:linear-gradient(90deg,rgba(255,255,255,.09),transparent); }

    /* ── Glass panel — reserved for hero/summary panels only (soft radius tier).
       §9.2.2 — the "NSVisualEffectView" trick: a barely-visible 2 % white
       overlay on the pure-black ground gives depth without a heavier card
       fill, and the amber aurora at the top of the page shows through
       cleanly. Every subsequent hero, morning-brief, and posture card
       inherits this. */
    .glass-panel {
        background: var(--card-lift);
        border: 1px solid var(--hairline);
        border-radius: var(--r-soft); padding: 20px;
        backdrop-filter: blur(4px);
        -webkit-backdrop-filter: blur(4px);
    }

    /* ── Scrollbars ──────────────────────────────────────────────────────────── */
    ::-webkit-scrollbar { width:6px; height:6px; }
    ::-webkit-scrollbar-thumb { background:#232833; border-radius:3px; }
    ::-webkit-scrollbar-thumb:hover { background:#313846; }
    ::-webkit-scrollbar-track { background:transparent; }

    /* ── Alerts & info boxes ─────────────────────────────────────────────────── */
    [data-testid="stAlert"] { border-radius: 8px; }

    /* ── F7b · colour-blind pattern layer ──────────────────────────────────────
       --bull / --bear are the only signal on many delta surfaces; ~5% of men
       have red-green colour vision deficiency. These utility classes pair
       the colour with a shape signal (up/down triangle) via ::before so any
       new caller can get shape+colour just by adding the class — no need to
       remember to prefix the string with ▲/▼. The existing `.metric-delta-
       pos/neg` classes already had callers prefixing the arrow manually;
       these are the migration target for anything new. */
    .delta-pos, .delta-neg { font-variant-numeric: tabular-nums; }
    .delta-pos { color: var(--bull); }
    .delta-neg { color: var(--bear); }
    .delta-pos::before { content: "\25B2  "; font-size: 0.85em; }  /* ▲ */
    .delta-neg::before { content: "\25BC  "; font-size: 0.85em; }  /* ▼ */

    /* ── Animations — functional only (live-signal pulse), not decorative ──────── */
    @keyframes pulse-green {
        0%,100% { box-shadow:0 0 0 0 rgba(111,170,132,.35) }
        50% { box-shadow:0 0 0 8px rgba(111,170,132,0) }
    }
    @keyframes pulse-red {
        0%,100% { box-shadow:0 0 0 0 rgba(199,112,112,.35) }
        50% { box-shadow:0 0 0 8px rgba(199,112,112,0) }
    }
    .pulse-green { animation:pulse-green 2s infinite; }
    .pulse-red   { animation:pulse-red 2s infinite; }

    /* UX3 · live-tick one-shot pulse — fires once when a tracked value
       ticks up or down between reruns. Distinct from .pulse-green/red
       (which loop for card-state emphasis on Paper Trades). The tick
       variant is a single 1.4s pass so the eye catches the change
       without a permanent aura. Callers set the class conditionally
       based on session-state diff — the animation runs on the next
       paint and stops on its own. */
    @keyframes tick-pulse-up-kf {
        0%   { box-shadow: 0 0 0 0 rgba(111,170,132,.55); }
        60%  { box-shadow: 0 0 0 10px rgba(111,170,132,0); }
        100% { box-shadow: 0 0 0 0 rgba(111,170,132,0); }
    }
    @keyframes tick-pulse-down-kf {
        0%   { box-shadow: 0 0 0 0 rgba(199,112,112,.55); }
        60%  { box-shadow: 0 0 0 10px rgba(199,112,112,0); }
        100% { box-shadow: 0 0 0 0 rgba(199,112,112,0); }
    }
    .tick-pulse-up   { animation: tick-pulse-up-kf   1.4s ease-out 1; }
    .tick-pulse-down { animation: tick-pulse-down-kf 1.4s ease-out 1; }

    /* ── Ticker tape — the one signature element: a lit "dealing room" strip ──── */
    @keyframes ticker-scroll { 0%{transform:translateX(0)} 100%{transform:translateX(-50%)} }
    .ticker-wrap {
        overflow:hidden; padding:9px 0; margin:8px 0;
        border-top:1px solid rgba(200,138,63,.3);
        border-bottom:1px solid rgba(255,255,255,.05);
        background:linear-gradient(180deg, rgba(200,138,63,.04), transparent);
    }
    .ticker-content {
        display:inline-block; white-space:nowrap; animation:ticker-scroll 80s linear infinite;
        font-size:13px; font-family:'JetBrains Mono','Courier New',monospace; letter-spacing:.2px;
    }
    .ticker-wrap:hover .ticker-content { animation-play-state:paused; }

    /* ── F6 · Loading skeletons — shimmer placeholders for slow surfaces ───── */
    @keyframes cc-skel-shimmer {
        0%   { background-position: -420px 0 }
        100% { background-position:  420px 0 }
    }
    .cc-skel {
        background: linear-gradient(90deg,
            rgba(255,255,255,0.02) 0%,
            rgba(255,255,255,0.06) 45%,
            rgba(255,255,255,0.12) 50%,
            rgba(255,255,255,0.06) 55%,
            rgba(255,255,255,0.02) 100%);
        background-size: 840px 100%;
        animation: cc-skel-shimmer 1.6s linear infinite;
        border-radius: 6px;
        display: block;
    }
    .cc-skel-card {
        background: var(--card-lift, rgba(255,255,255,0.02));
        border: 1px solid var(--hairline-soft, rgba(255,255,255,0.08));
        border-radius: 10px;
        padding: 14px 16px;
        margin: 8px 0;
    }
    @media (prefers-reduced-motion: reduce) {
        .cc-skel { animation: none; }
    }

    /* ── UX2 · Ticker hover preview card ─────────────────────────────────────
       Pure CSS `:hover` tooltip anchored to a ticker span. No JS, no custom
       component — the entire preview (sparkline SVG, price, delta, score
       chip) is pre-rendered inline into the span at page render time and
       hidden/shown by a single CSS rule. Fine for the desktop hover UX;
       mobile users tap-through the ticker without the preview (no `:hover`
       state on touch).

       Layout: the outer .ticker-hover span is inline + position:relative
       so the .ticker-hover-card absolute-positions off of it. The card
       floats above the anchor with a small top offset and a subtle drop
       shadow; the tail arrow is a rotated square at the top edge.

       Wired first on Command Centre top-picks cards (see picks_ui /
       shared/ui_components ticker_hover_wrap). Other surfaces can adopt
       the same helper. */
    .ticker-hover {
        position: relative;
        display: inline-block;
        cursor: default;
    }
    .ticker-hover .ticker-hover-card {
        position: absolute;
        bottom: 100%;
        left: 0;
        margin-bottom: 8px;
        min-width: 200px;
        max-width: 260px;
        background: var(--card-lift, #101828);
        border: 1px solid var(--hairline, rgba(255,255,255,.08));
        border-radius: 10px;
        padding: 10px 12px;
        box-shadow: 0 8px 24px rgba(0,0,0,.55);
        opacity: 0;
        visibility: hidden;
        transform: translateY(4px);
        transition: opacity .15s ease-out, transform .15s ease-out, visibility 0s .15s;
        z-index: 100;
        pointer-events: none;
        text-align: left;
        font-family: var(--font-sans);
    }
    .ticker-hover:hover .ticker-hover-card,
    .ticker-hover:focus-visible .ticker-hover-card,
    .ticker-hover:focus-within .ticker-hover-card {
        opacity: 1;
        visibility: visible;
        transform: translateY(0);
        transition: opacity .15s ease-out, transform .15s ease-out, visibility 0s;
    }
    /* Tail arrow — small rotated square anchored to the card's bottom edge. */
    .ticker-hover .ticker-hover-card::after {
        content: "";
        position: absolute;
        bottom: -5px;
        left: 16px;
        width: 10px;
        height: 10px;
        background: var(--card-lift, #101828);
        border-right: 1px solid var(--hairline, rgba(255,255,255,.08));
        border-bottom: 1px solid var(--hairline, rgba(255,255,255,.08));
        transform: rotate(45deg);
    }
    .ticker-hover-card .thc-row {
        display: flex; justify-content: space-between; align-items: center;
        margin-bottom: 4px; gap: 8px;
    }
    .ticker-hover-card .thc-sym {
        font-size: 13px; font-weight: 700; color: var(--ink, #f0f4ff);
        letter-spacing: .2px;
    }
    .ticker-hover-card .thc-score {
        font-family: var(--font-mono); font-size: 11px; font-weight: 700;
        padding: 1px 6px; border-radius: 4px;
    }
    .ticker-hover-card .thc-price {
        font-family: var(--font-mono); font-size: 14px; font-weight: 700;
        color: var(--ink, #f0f4ff);
    }
    .ticker-hover-card .thc-delta {
        font-family: var(--font-mono); font-size: 11px; font-weight: 600;
    }
    .ticker-hover-card .thc-sector {
        font-size: 10px; color: var(--dim, #8b8e97);
        text-transform: uppercase; letter-spacing: .6px; margin-top: 4px;
    }
    .ticker-hover-card svg { margin-top: 4px; }
    /* Below 768 px the card would overflow a phone viewport, so hide it and
       let the ticker just be a ticker — F2 mobile pass covers the layout. */
    @media (max-width: 768px) {
        .ticker-hover .ticker-hover-card { display: none !important; }
    }

    /* ── F2 · Mobile responsive pass ────────────────────────────────────────
       The app is desktop-first (portfolio work, chart reading). Below 768 px
       the two-column card grids stack, the top-bar chips shrink, hero text
       scales down, and container padding tightens so content fits without
       a horizontal scrollbar. 481–768 is "tablet"; ≤480 is phone.

       Streamlit's own st.columns does NOT auto-collapse on narrow viewports
       even with layout="wide" — flipping [data-testid="stHorizontalBlock"]
       to flex-direction:column is what stacks the 2-column card grids
       (holdings, top picks, movers). Chip/text inline-styles from Python
       need !important to override.

       Not touched: st.tabs (already horizontal-scrollable), sidebar (users
       expect it collapsed on phones — Streamlit's own toggle handles that),
       the ticker tape animation itself (scrolls fine at any width). */
    @media (max-width: 768px) {
        /* Collapse every st.columns() row to a single-column stack. */
        [data-testid="stHorizontalBlock"] {
            flex-direction: column !important;
            gap: 8px !important;
        }
        [data-testid="stColumn"] {
            width: 100% !important;
            flex: 1 1 100% !important;
            min-width: 0 !important;
        }

        /* Opt-in utility for raw <div style="display:flex"> card grids that
           can't reach data-testid. Pages that hand-wrote a 2- or 3-card row
           with inline flex (Command Centre Market Pulse, My Portfolio stat
           tiles) tag their outer div with class="mobile-stack" to opt in. */
        .mobile-stack {
            flex-direction: column !important;
        }

        /* Top-bar chips (F7b .topbar-chip hook + rendered by
           chart_helpers._live_top_bar). The Python side sets an inline
           min-width:118px which overflows a 375 px viewport with 5+ chips;
           let them shrink to their content width instead. */
        .topbar-chip {
            min-width: 0 !important;
            padding: 5px 9px !important;
        }

        /* Typography scale — the biggest text ("Live Ticker" hero, score
           dial) is what triggers horizontal scroll on phones. Pull it in. */
        .t-display        { font-size: 24px !important; }
        .t-h1             { font-size: 18px !important; }
        .page-title-serif { font-size: 28px !important; line-height: 1.05 !important; }
        .score-big        { font-size: 40px !important; }

        /* Ticker tape — narrower gap between symbols so more fit per screen
           without changing the overall scroll cadence. */
        .ticker-content { font-size: 12px !important; }

        /* Streamlit's default block-container padding is calibrated for a
           1200 px+ viewport. On phones the ~5rem side padding wastes half
           the screen; tighten to a 12 px gutter so cards go edge-to-edge. */
        .stApp .main .block-container,
        [data-testid="stAppViewContainer"] .main .block-container {
            padding-left: 12px !important;
            padding-right: 12px !important;
            padding-top: 12px !important;
        }
    }

    @media (max-width: 480px) {
        /* Phone-only tightening on top of the tablet rules above. */
        .page-title-serif { font-size: 22px !important; }
        .t-display        { font-size: 20px !important; }
        .score-big        { font-size: 32px !important; }
        .ticker-content   { font-size: 11px !important; }
    }

    /* ── P2 · Investor Guide long-form typography (15_investor_guide.py) ───
       Self-contained block. Scoped via the .guide-doc marker on the page H1
       so no other page's markdown picks up the 15/1.6 body scale.
       H2 22/700 · body 15/1.6. The H1 deliberately keeps the shared
       .page-title-serif scale (serif, !important) so the guide title matches
       every other page — a 32/700 override here could never win anyway. */
    .t-h2.guide-h2 { font-size: 22px; font-weight: 700; line-height: 1.25;
                     margin-top: 22px !important; }
    [data-testid="stMain"]:has(.guide-doc) [data-testid="stMarkdownContainer"] p,
    [data-testid="stMain"]:has(.guide-doc) [data-testid="stMarkdownContainer"] li {
        font-size: 15px; line-height: 1.6;
    }
    @media (max-width: 480px) {
        .t-h2.guide-h2 { font-size: 18px; }
    }

    /* Investor Guide as a document (artboard 09): one centred reading column
       per chapter tab, serif chapter title, table of contents, drop-cap lede,
       numbered section heads, pull quotes and callouts. Scoped to the keyed
       containers the page emits (.st-key-guide_ch_<n>). */
    [class*="st-key-guide_ch_"] { max-width: 760px; margin: 0 auto; }
    .gd-eyebrow {
        margin: 18px 0 12px 0; font-size: 11px; font-weight: 600; color: var(--dim);
        letter-spacing: 0.18em; text-transform: uppercase;
    }
    .gd-eyebrow span { color: var(--accent); }
    .gd-title {
        margin: 0 0 14px 0; font-family: var(--font-serif); font-weight: 400;
        font-size: 42px; line-height: 1.08; letter-spacing: -0.02em; color: var(--ink);
        text-wrap: balance;
    }
    .gd-title em { color: var(--accent); }
    .gd-meta {
        display: flex; flex-wrap: wrap; gap: 6px 14px; padding-bottom: 20px; margin-bottom: 26px;
        border-bottom: 1px solid var(--hairline); font-family: var(--font-mono);
        font-size: 12px; color: var(--faint);
    }
    .gd-toc {
        display: grid; gap: 2px; padding: 14px 18px; margin: 0 0 28px 0;
        background: var(--sunken); border-radius: 4px;
    }
    .gd-toc-t {
        margin-bottom: 6px; font-size: 10px; font-weight: 600; color: var(--dim);
        letter-spacing: 0.14em; text-transform: uppercase;
    }
    .gd-toc a {
        display: flex; justify-content: space-between; gap: 12px; padding: 4px 0;
        border-bottom: 1px dotted var(--hairline); font-size: 13.5px;
        color: var(--ink) !important; text-decoration: none !important;
    }
    .gd-toc a:last-of-type { border-bottom: none; }
    .gd-toc a:hover { color: var(--accent) !important; }
    .gd-toc .n { font-family: var(--font-mono); font-size: 11px; color: var(--faint); }
    .gd-lede {
        margin: 0 0 28px 0; font-family: var(--font-serif); font-size: 20px;
        line-height: 1.5; color: var(--ink);
    }
    .gd-lede::first-letter {
        float: left; padding: 6px 10px 0 0; font-size: 3.6em; line-height: 0.85; color: var(--accent);
    }
    .gd-h3 {
        display: flex; align-items: baseline; gap: 12px; margin: 34px 0 12px 0;
        font-family: var(--font-serif); font-size: 25px; letter-spacing: -0.01em;
        color: var(--ink); scroll-margin-top: 80px;
    }
    .gd-h3 .n { font-family: var(--font-mono); font-size: 12px; font-weight: 600; color: var(--accent); }
    .gd-quote {
        margin: 26px 0; padding: 4px 0 4px 20px; border-left: 2px solid var(--accent);
        font-family: var(--font-serif); font-style: italic; font-size: 20px;
        line-height: 1.4; color: var(--ink);
    }
    .gd-callout {
        margin: 18px 0; padding: 14px 18px; background: var(--sunken); border-radius: 4px;
        font-size: 13.5px; line-height: 1.6; color: var(--ink-mid);
    }
    .gd-callout b { color: var(--ink); }
    .gd-callout.warn { border-left: 2px solid var(--amber); }
    .gd-next {
        margin-top: 34px; padding-top: 18px; border-top: 1px solid var(--hairline);
        font-size: 13px; color: var(--dim);
    }
    [class*="st-key-guide_ch_"] .sig-table-wrap { margin: 10px 0 18px 0; }
    [class*="st-key-guide_ch_"] table.sig-table td { white-space: normal; vertical-align: top; }
    @media (max-width: 640px) {
        .gd-title { font-size: 32px; }
        .gd-lede { font-size: 17px; }
    }

    /* ── P2 · Command Centre regime strip / pick-more ──
       Self-contained block (02_command_centre.py). */
    .regime-strip {
        display: flex; flex-wrap: wrap; gap: 6px 18px; align-items: center;
        padding: 6px 12px; margin: -4px 0 10px 0;
        background: var(--sunken); border: 1px solid var(--hairline-soft); border-radius: 8px;
    }
    .regime-strip-cell { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
    .regime-strip-dot { width: 6px; height: 6px; border-radius: 50%; }
    .regime-strip-k {
        color: var(--dim); font-size: 10px; font-weight: 700;
        letter-spacing: 0.5px; text-transform: uppercase;
    }
    .regime-strip-v { font-weight: 600; font-family: var(--font-mono); }

    .pick-card details.pick-more { margin-top: 4px; }
    .pick-card details.pick-more > summary {
        cursor: pointer; font-size: 10px; color: var(--faint);
        letter-spacing: 0.4px; text-transform: uppercase; list-style: none;
    }
    .pick-card details.pick-more > summary::before { content: "▸ "; }
    .pick-card details.pick-more[open] > summary::before { content: "▾ "; }

    /* ── Mockup layout kit (2026-09-12 "proposed layout" artifact) ─────────
       Shared by the page-by-page mockup rollout. Python builders live in
       ui_components.py: section_header / gate_strip / rank_chip /
       signal_table / follow_card. Dark-only; every colour is a token. */
    .sec-hd {
        display: flex; flex-wrap: wrap; align-items: baseline;
        justify-content: space-between; gap: 4px 12px; margin: 20px 0 10px 0;
    }
    .sec-hd-t {
        font-family: var(--font-serif); font-weight: 400; font-size: 20px;
        letter-spacing: -0.01em; color: var(--ink); margin: 0;
    }
    .sec-hd-a {
        font-family: var(--font-mono); font-size: 10px; color: var(--faint);
        letter-spacing: 0.08em; text-transform: uppercase;
    }

    /* Five-cell summary ribbon. Five is the ceiling (UI_UX_DESIGN §4.1). */
    .gate-strip {
        display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));
        background: var(--sunken); border: 1px solid var(--hairline);
        border-radius: 6px; overflow: hidden; margin: 6px 0 14px 0;
    }
    .gate-cell { padding: 10px 14px; border-right: 1px solid var(--hairline-soft); min-width: 0; }
    .gate-cell:last-child { border-right: none; }
    .gate-k {
        display: block; font-size: 10px; font-weight: 600; color: var(--dim);
        letter-spacing: 0.1em; text-transform: uppercase;
    }
    .gate-v {
        display: block; margin-top: 3px; font-family: var(--font-mono);
        font-size: 16px; font-weight: 600; color: var(--ink);
        font-variant-numeric: tabular-nums;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .gate-s { display: block; margin-top: 2px; font-size: 11px; color: var(--faint); }

    /* Ranked data table: mono right-aligned numbers, hairline rows. */
    .sig-table-wrap {
        background: var(--card-lift); border: 1px solid var(--hairline);
        border-radius: 6px; overflow-x: auto; margin-bottom: 10px;
    }
    table.sig-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    table.sig-table th {
        padding: 9px 12px; text-align: left; white-space: nowrap;
        background: var(--sunken); border-bottom: 1px solid var(--hairline);
        font-size: 10px; font-weight: 600; color: var(--dim);
        letter-spacing: 0.08em; text-transform: uppercase;
    }
    table.sig-table td {
        padding: 8px 12px; color: var(--ink); vertical-align: middle;
        border-bottom: 1px solid var(--hairline-soft);
    }
    table.sig-table tr:last-child td { border-bottom: none; }
    table.sig-table tbody tr:hover td { background: color-mix(in srgb, var(--accent) 4%, transparent); }
    table.sig-table th.r, table.sig-table td.r {
        text-align: right; white-space: nowrap;
        font-family: var(--font-mono); font-variant-numeric: tabular-nums;
    }
    table.sig-table .sym { font-weight: 600; }
    table.sig-table .co { display: block; margin-top: 1px; font-size: 11px; color: var(--dim); }

    /* Rank chip: top three carry the accent in falling strength. */
    .rank-chip {
        display: inline-flex; align-items: center; justify-content: center;
        width: 22px; height: 22px; border-radius: 3px;
        font-family: var(--font-mono); font-size: 11px; font-weight: 600;
        background: var(--sunken); color: var(--ink-mid); border: 1px solid var(--hairline);
    }
    .rank-chip.r1 { background: var(--accent); color: var(--ground); border-color: var(--accent); }
    .rank-chip.r2 {
        background: color-mix(in srgb, var(--accent) 45%, var(--sunken)); color: var(--ink);
        border-color: color-mix(in srgb, var(--accent) 45%, var(--hairline));
    }
    .rank-chip.r3 {
        background: color-mix(in srgb, var(--accent) 22%, var(--sunken)); color: var(--ink);
        border-color: color-mix(in srgb, var(--accent) 30%, var(--hairline));
    }

    /* Follow-through cards (five across). */
    .fu-grid {
        display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));
        gap: 10px; margin-bottom: 18px;
    }
    .fu-card {
        position: relative; overflow: hidden; min-width: 0; padding: 12px 14px;
        background: var(--surface); border: 1px solid var(--hairline); border-radius: 6px;
    }
    .fu-card.good {
        background: linear-gradient(180deg, color-mix(in srgb, var(--bull) 7%, var(--surface)), var(--surface) 80%);
        border-color: color-mix(in srgb, var(--bull) 30%, var(--hairline));
    }
    .fu-card.bad {
        background: linear-gradient(180deg, color-mix(in srgb, var(--bear) 7%, var(--surface)), var(--surface) 80%);
        border-color: color-mix(in srgb, var(--bear) 28%, var(--hairline));
    }
    .fu-rank {
        position: absolute; top: 9px; right: 12px; font-family: var(--font-mono);
        font-size: 10px; color: var(--faint); letter-spacing: 0.08em;
    }
    .fu-sym { font-weight: 600; font-size: 14px; color: var(--ink); }
    .fu-sub { margin-top: 1px; font-size: 11px; color: var(--dim); }
    .fu-row {
        display: flex; justify-content: space-between; align-items: baseline; gap: 6px;
        margin-top: 8px; padding-top: 7px; border-top: 1px dotted var(--hairline);
    }
    .fu-k { font-size: 10px; color: var(--dim); letter-spacing: 0.08em; text-transform: uppercase; }
    .fu-v { font-family: var(--font-mono); font-size: 12.5px; color: var(--ink); white-space: nowrap; }
    .fu-foot { margin-top: 9px; }

    /* Sector heat grid: tint strength is set inline per cell (color-mix
       of --bull / --bear into --surface), so only structure lives here. */
    .heat-grid {
        display: grid; grid-template-columns: repeat(auto-fit, minmax(112px, 1fr));
        gap: 1px; background: var(--hairline); border: 1px solid var(--hairline);
        border-radius: 6px; overflow: hidden; margin-bottom: 16px;
    }
    .heat-cell {
        min-height: 76px; padding: 11px 12px 12px; background: var(--surface);
        display: flex; flex-direction: column; justify-content: space-between;
    }
    .heat-name {
        font-size: 11px; font-weight: 600; color: var(--ink-mid);
        letter-spacing: 0.06em; text-transform: uppercase;
    }
    .heat-val { font-family: var(--font-mono); font-size: 15px; font-weight: 600; }
    .heat-sub { font-family: var(--font-mono); font-size: 10px; color: var(--dim); }

    /* Panel shell with a title bar (two-column rows). */
    .kit-panel {
        background: var(--card-lift); border: 1px solid var(--hairline);
        border-radius: 6px; overflow: hidden; margin-bottom: 12px;
    }
    .kit-panel-hd {
        display: flex; justify-content: space-between; align-items: baseline; gap: 8px;
        padding: 11px 14px; border-bottom: 1px solid var(--hairline);
    }
    .kit-panel-t { font-weight: 600; font-size: 13px; color: var(--ink); }
    .kit-panel-s { font-size: 11px; color: var(--dim); }
    .kit-panel .sig-table-wrap { border: none; border-radius: 0; margin: 0; background: none; }

    /* Change feed (verdict ledger). */
    .ledger-list { list-style: none; margin: 0; padding: 0; }
    .ledger-list li {
        display: grid; grid-template-columns: 52px 1fr; gap: 12px;
        padding: 10px 14px; border-bottom: 1px solid var(--hairline-soft); font-size: 13px;
    }
    .ledger-list li:last-child { border-bottom: none; }
    .ledger-when { font-family: var(--font-mono); font-size: 11px; color: var(--dim); padding-top: 3px; }
    .ledger-sym { font-weight: 600; color: var(--ink); margin-right: 6px; }
    .ledger-note { margin-top: 3px; font-size: 11.5px; color: var(--dim); }

    /* Share bars (sector allocation, posture spread). */
    .alloc { display: grid; gap: 8px; padding: 12px 14px; }
    .alloc-row {
        display: grid; grid-template-columns: minmax(90px, 130px) 1fr 58px;
        gap: 10px; align-items: center; font-size: 12px;
    }
    .alloc-name { color: var(--ink-mid); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .alloc-bar { height: 8px; background: var(--sunken); border-radius: 2px; overflow: hidden; }
    .alloc-fill { height: 100%; background: var(--accent); }
    .alloc-val { font-family: var(--font-mono); font-size: 11.5px; color: var(--ink); text-align: right; }
    .kit-panel-note {
        padding: 10px 14px 12px; border-top: 1px solid var(--hairline-soft);
        font-size: 12px; line-height: 1.5; color: var(--dim);
    }

    /* One-line provider health strip. */
    .health-strip {
        display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px;
        padding: 8px 12px; margin: 4px 0 8px 0; background: var(--sunken);
        border: 1px solid var(--hairline); border-radius: 6px;
        font-family: var(--font-mono); font-size: 11px; color: var(--dim);
    }
    .health-strip b {
        font-family: var(--font-sans); font-size: 11px; font-weight: 600; color: var(--ink-mid);
        letter-spacing: 0.08em; text-transform: uppercase;
        padding-right: 10px; border-right: 1px solid var(--hairline);
    }
    .health-item { display: inline-flex; align-items: center; gap: 5px; }
    .health-dot { width: 6px; height: 6px; border-radius: 50%; }

    /* Quiet tool layout (Position Sizer, artboard 05): underline inputs on
       the left, one sunken output panel on the right. Widgets are scoped by
       the keyed container class Streamlit emits (st-key-<key>). */
    .quiet-eyebrow {
        font-size: 10px; font-weight: 600; color: var(--dim);
        letter-spacing: 0.16em; text-transform: uppercase; margin: 6px 0 2px 0;
    }
    .st-key-quiet_inputs, .st-key-kelly_inputs { padding-right: 12px; }
    .st-key-quiet_inputs [data-testid="stWidgetLabel"] p,
    .st-key-kelly_inputs [data-testid="stWidgetLabel"] p {
        font-size: 11px !important; font-weight: 600; color: var(--dim);
        letter-spacing: 0.1em; text-transform: uppercase;
    }
    .st-key-quiet_inputs [data-baseweb="input"],
    .st-key-kelly_inputs [data-baseweb="input"] {
        background: transparent !important; border: none !important;
        border-bottom: 1px solid var(--hairline-strong, var(--hairline)) !important;
        border-radius: 0 !important;
    }
    .st-key-quiet_inputs [data-baseweb="base-input"],
    .st-key-kelly_inputs [data-baseweb="base-input"],
    .st-key-quiet_inputs [data-testid^="stNumberInputStep"],
    .st-key-kelly_inputs [data-testid^="stNumberInputStep"] {
        background: transparent !important; color: var(--dim) !important;
    }
    .st-key-quiet_inputs [data-baseweb="input"]:focus-within,
    .st-key-kelly_inputs [data-baseweb="input"]:focus-within {
        border-bottom-color: var(--accent) !important;
    }
    .st-key-quiet_inputs .stNumberInput input,
    .st-key-kelly_inputs .stNumberInput input {
        background: transparent !important; border: none !important; box-shadow: none !important;
        padding-left: 0; font-family: var(--font-mono); font-size: 19px; color: var(--ink);
    }
    .quiet-hint { font-size: 11px; color: var(--faint); margin: -6px 0 18px 0; }
    .quiet-output {
        background: var(--sunken); border: 1px solid var(--hairline-soft);
        border-radius: 6px; padding: 28px 32px;
    }
    .quiet-out-lbl {
        font-size: 10px; font-weight: 700; color: var(--dim);
        letter-spacing: 0.14em; text-transform: uppercase; margin-bottom: 8px;
    }
    .quiet-lead {
        font-family: var(--font-serif); font-size: 56px; line-height: 1;
        letter-spacing: -0.03em; color: var(--ink);
    }
    .quiet-lead-u { font-size: 18px; color: var(--dim); letter-spacing: 0; margin-left: 6px; }
    .quiet-lead-sub { font-family: var(--font-mono); font-size: 12.5px; color: var(--dim); margin-top: 8px; }
    .quiet-kv { margin: 24px 0 0 0; }
    .quiet-kv > div {
        display: flex; justify-content: space-between; gap: 12px; padding: 10px 0;
        border-bottom: 1px dotted var(--hairline); font-size: 13px;
    }
    .quiet-kv > div:last-child { border-bottom: none; }
    .quiet-kv dt { color: var(--dim); }
    .quiet-kv dd { margin: 0; font-family: var(--font-mono); color: var(--ink); text-align: right; }
    .quiet-note { margin-top: 18px; font-size: 11.5px; line-height: 1.5; color: var(--faint); }

    /* Market Breadth (artboard 07): gauge panel, MA bars, FII/DII flow bars. */
    .breadth-panel {
        background: var(--card-lift); border: 1px solid var(--hairline);
        border-radius: 6px; padding: 16px 18px; margin-bottom: 14px;
    }
    .breadth-lbl {
        font-size: 10px; font-weight: 700; color: var(--dim);
        letter-spacing: 0.14em; text-transform: uppercase; margin-bottom: 6px;
    }
    .breadth-h {
        font-family: var(--font-serif); font-size: 22px; letter-spacing: -0.01em;
        color: var(--ink); margin: 0 0 14px 0;
    }
    .breadth-panel .alloc { padding: 4px 0; }
    .breadth-panel .alloc-bar { height: 18px; }
    .gauge-row { display: grid; grid-template-columns: auto 1fr; gap: 22px; align-items: center; }
    .breadth-gauge { width: 200px; height: 116px; display: block; }
    .breadth-gauge-t { font-family: var(--font-mono); font-size: 9px; fill: var(--dim); }
    .gauge-num { font-family: var(--font-mono); font-size: 28px; font-weight: 500; color: var(--ink); }
    .gauge-sub { font-size: 12px; color: var(--dim); margin-top: 3px; }
    .gauge-note { margin-top: 12px; font-size: 13px; line-height: 1.5; color: var(--ink-mid); }
    .b-stats {
        display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px;
        margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--hairline);
    }
    .b-stat { padding: 8px 10px; background: var(--sunken); border-radius: 4px; }
    .b-stat-k { font-size: 10px; color: var(--dim); letter-spacing: 0.08em; text-transform: uppercase; }
    .b-stat-v { font-family: var(--font-mono); font-size: 16px; color: var(--ink); margin-top: 2px; }
    .breadth-foot { padding-top: 10px; font-size: 12px; line-height: 1.5; color: var(--dim); }
    .flow-legend { margin: -8px 0 12px 0; font-size: 12px; color: var(--dim); }
    .flow-bars { display: flex; align-items: flex-end; height: 100px; gap: 6px; padding: 0 4px; }
    .flow-col { flex: 1; display: flex; align-items: flex-end; gap: 2px; height: 100%; }
    .flow-b { flex: 1; min-height: 0; border-radius: 1px 1px 0 0; }
    .flow-b.flow-dii { opacity: 0.6; }
    .flow-axis {
        display: flex; justify-content: space-between; gap: 6px; padding: 4px 4px 0; margin-top: 6px;
        border-top: 1px solid var(--hairline); font-family: var(--font-mono); font-size: 10px; color: var(--dim);
    }
    .flow-axis span { flex: 1; text-align: center; }
    @media (max-width: 900px) { .gauge-row { grid-template-columns: 1fr; } }

    /* Intraday (artboard 10): chart toolbar, level strip, session read,
       market-depth ladder, session stats. */
    .chart-tools {
        display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px;
        padding: 10px 14px; background: var(--card-lift); border: 1px solid var(--hairline);
        border-radius: 6px 6px 0 0; border-bottom: none; font-size: 12px;
    }
    .ct-sym { font-weight: 600; font-size: 15px; color: var(--ink); }
    .ct-co { font-family: var(--font-mono); font-size: 12px; color: var(--dim); margin-left: 6px; font-weight: 400; }
    .ct-up { color: var(--bull); }
    .ct-dn { color: var(--bear); }
    .ct-tags { display: flex; gap: 6px; margin-left: auto; }
    .ct-tag {
        padding: 3px 8px; border-radius: 3px; font-size: 11px; font-weight: 500;
        border: 1px solid var(--hairline); color: var(--ink-mid);
    }
    .ct-tag.on { border-color: var(--accent); color: var(--accent); }
    .level-strip {
        display: grid; grid-template-columns: repeat(6, minmax(0, 1fr));
        border: 1px solid var(--hairline); border-radius: 6px; overflow: hidden; margin: 4px 0 12px 0;
    }
    .lc { padding: 10px 12px; border-right: 1px solid var(--hairline); background: var(--card-lift); }
    .lc:last-child { border-right: none; }
    .lc.hi { background: var(--tint-accent, var(--sunken)); }
    .lc-k { font-size: 10px; color: var(--dim); letter-spacing: 0.08em; text-transform: uppercase; font-weight: 500; }
    .lc-v { font-family: var(--font-mono); font-size: 14px; color: var(--ink); margin-top: 3px; white-space: nowrap; }
    .lc-s { font-family: var(--font-mono); font-size: 10px; color: var(--faint); margin-top: 1px; }
    .setup-card {
        padding: 16px 18px; background: var(--card-lift); border: 1px solid var(--hairline);
        border-radius: 6px; margin-bottom: 12px;
    }
    .sc-lbl {
        font-size: 10px; font-weight: 700; color: var(--accent);
        letter-spacing: 0.14em; text-transform: uppercase; margin-bottom: 6px;
    }
    .sc-h { font-family: var(--font-serif); font-size: 20px; color: var(--ink); margin: 0 0 8px 0; }
    .sc-p { font-size: 12.5px; line-height: 1.5; color: var(--dim); margin: 0 0 12px 0; }
    .sc-nums { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; }
    .sc-k { font-size: 10px; color: var(--faint); letter-spacing: 0.08em; text-transform: uppercase; }
    .sc-v { font-family: var(--font-mono); font-size: 13px; color: var(--ink); margin-top: 1px; }
    .depth-ladder {
        border: 1px solid var(--hairline); border-radius: 6px; overflow: hidden;
        background: var(--card-lift); margin-bottom: 12px;
    }
    .depth-head, .depth-row { display: grid; grid-template-columns: 1fr 1fr 1fr; padding: 4px 12px; }
    .depth-head {
        padding: 8px 12px; background: var(--sunken); border-bottom: 1px solid var(--hairline);
        font-size: 10px; font-weight: 600; color: var(--dim); letter-spacing: 0.08em; text-transform: uppercase;
    }
    .depth-head span:nth-child(2), .depth-row .d-price { text-align: center; }
    .depth-head span:nth-child(3), .depth-row .d-ord { text-align: right; }
    .depth-row { position: relative; align-items: center; font-family: var(--font-mono); font-size: 11.5px; }
    .d-qty { color: var(--dim); position: relative; z-index: 1; }
    .d-price { font-weight: 500; position: relative; z-index: 1; }
    .d-price.buy { color: var(--bull); }
    .d-price.sell { color: var(--bear); }
    .d-ord { color: var(--faint); font-size: 10.5px; position: relative; z-index: 1; }
    .d-bar { position: absolute; top: 0; bottom: 0; opacity: 0.18; }
    .d-bar.buy { right: 50%; background: var(--bull); }
    .d-bar.sell { left: 50%; background: var(--bear); }
    .depth-spread {
        padding: 8px 12px; text-align: center; background: var(--sunken);
        border-top: 1px solid var(--hairline); border-bottom: 1px solid var(--hairline);
        font-family: var(--font-mono); font-size: 11px; color: var(--dim);
    }
    .depth-spread b { color: var(--ink); font-weight: 500; margin: 0 4px; }
    .sess-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; padding: 12px 14px; }
    @media (max-width: 900px) { .level-strip { grid-template-columns: repeat(3, minmax(0, 1fr)); } }

    /* Analyze Stock (artboard 02): ticker head, hero pillar bars, dotted
       key/value cards, bull / bear / risk / verdict grid. */
    .stock-hd {
        display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between;
        gap: 12px 28px; padding: 4px 0 2px 0;
    }
    .sh-sym {
        font-family: var(--font-serif); font-size: 40px; line-height: 1;
        letter-spacing: -0.02em; color: var(--ink);
    }
    .sh-co { display: block; margin-top: 6px; font-size: 13px; color: var(--dim); }
    .sh-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
    .sh-price { text-align: right; }
    .sh-now {
        font-family: var(--font-mono); font-size: 30px; font-weight: 500;
        letter-spacing: -0.01em; color: var(--ink); font-variant-numeric: tabular-nums;
    }
    .sh-ccy { font-size: 17px; font-weight: 400; color: var(--dim); margin-right: 4px; }
    .sh-paise { color: var(--dim); }
    .sh-row { display: flex; justify-content: flex-end; gap: 4px; margin-top: 6px; }
    .sh-note { margin-top: 4px; font-size: 11px; color: var(--faint); }
    .st-key-as_actions [data-testid="stButton"] button,
    .st-key-as_actions [data-testid="stPopover"] button {
        min-height: 34px; padding: 4px 10px; font-size: 12px; font-weight: 500;
    }
    .st-key-as_actions button p { white-space: nowrap; font-size: 12px; }
    .hv-pillars { display: grid; gap: 7px; width: 100%; margin-top: 8px; }
    .hv-pillar {
        display: grid; grid-template-columns: 78px 1fr 52px; gap: 10px;
        align-items: center; font-size: 12px;
    }
    .hv-pl-n { color: var(--dim); }
    .hv-pl-bar {
        position: relative; height: 6px; background: var(--hairline);
        border-radius: 999px; overflow: hidden;
    }
    .hv-pl-fill { position: absolute; top: 0; bottom: 0; left: 0; border-radius: inherit; }
    .hv-pl-v { font-family: var(--font-mono); text-align: right; color: var(--ink); }
    .kit-card-b { padding: 12px 14px 14px; }
    dl.kv { display: grid; grid-template-columns: 1fr auto; margin: 0; font-size: 13px; }
    dl.kv dt, dl.kv dd { padding: 6px 0; border-bottom: 1px dotted var(--hairline); }
    dl.kv dt { color: var(--dim); font-size: 12px; }
    dl.kv dd {
        margin: 0; text-align: right; white-space: nowrap; color: var(--ink);
        font-family: var(--font-mono); font-variant-numeric: tabular-nums;
    }
    dl.kv dt.hi, dl.kv dd.hi { background: var(--tint-accent); }
    dl.kv dt.hi { margin-left: -6px; padding-left: 6px; }
    dl.kv dd.hi { margin-right: -6px; padding-right: 6px; }
    dl.kv > :nth-last-child(-n+2) { border-bottom: none; }
    .kv-note {
        margin-top: 10px; padding: 8px 10px; font-size: 11.5px; line-height: 1.5; color: var(--dim);
        background: var(--sunken); border-left: 2px solid var(--accent); border-radius: 0 4px 4px 0;
    }
    .kv-note b { color: var(--ink); font-weight: 500; }
    .thesis-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .th-block {
        min-width: 0; padding: 11px 13px; background: var(--surface);
        border: 1px solid var(--hairline); border-radius: 4px;
    }
    .th-lbl {
        margin-bottom: 6px; font-size: 10px; font-weight: 700;
        letter-spacing: 0.12em; text-transform: uppercase;
    }
    .th-block.bull .th-lbl { color: var(--bull); }
    .th-block.bear .th-lbl { color: var(--bear); }
    .th-block.risk .th-lbl { color: var(--amber); }
    .th-block.verd .th-lbl { color: var(--accent); }
    .th-block ul { margin: 0; padding-left: 16px; font-size: 12.5px; line-height: 1.5; color: var(--ink-mid); }
    .th-more { margin-top: 4px; font-size: 11px; color: var(--faint); }
    .th-empty { font-size: 12px; font-style: italic; color: var(--faint); }
    .th-block.verd {
        grid-column: span 2;
        background: color-mix(in srgb, var(--accent) 5%, var(--surface));
        border-color: color-mix(in srgb, var(--accent) 25%, var(--hairline));
    }
    .th-body {
        margin: 0; font-family: var(--font-serif); font-size: 17px; line-height: 1.4; color: var(--ink);
    }
    @media (max-width: 900px) {
        .thesis-grid { grid-template-columns: 1fr; }
        .th-block.verd { grid-column: auto; }
        .sh-price { text-align: left; }
        .sh-row { justify-content: flex-start; }
    }

    /* Live Ticker (artboard 06): dense price table with one-shot row flash
       on a tick, and the index rail beside it. */
    .lt-meta {
        display: flex; flex-wrap: wrap; align-items: center; gap: 4px 14px;
        font-family: var(--font-mono); font-size: 12px; color: var(--dim);
    }
    .lt-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; }
    .ticker-panel {
        max-height: 640px; overflow: auto; background: var(--card-lift);
        border: 1px solid var(--hairline); border-radius: 6px;
    }
    table.ticker-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
    table.ticker-table th {
        position: sticky; top: 0; z-index: 1; padding: 9px 12px; text-align: right; white-space: nowrap;
        background: var(--sunken); border-bottom: 1px solid var(--hairline);
        font-size: 10px; font-weight: 500; color: var(--dim); letter-spacing: 0.08em; text-transform: uppercase;
    }
    table.ticker-table th:first-child, table.ticker-table td:first-child { text-align: left; }
    table.ticker-table td {
        padding: 6px 12px; text-align: right; white-space: nowrap; color: var(--ink);
        line-height: 1.3; border-bottom: 1px solid var(--hairline-soft);
        font-family: var(--font-mono); font-variant-numeric: tabular-nums;
    }
    table.ticker-table td:first-child { font-family: var(--font-sans); }
    table.ticker-table tbody tr:hover td { background: color-mix(in srgb, var(--accent) 4%, transparent); }
    table.ticker-table .sym { font-weight: 600; }
    table.ticker-table .co { display: block; font-size: 11px; color: var(--dim); }
    .tk-up { color: var(--bull) !important; }
    .tk-dn { color: var(--bear) !important; }
    .tk-flat { color: var(--dim) !important; }
    @keyframes tk-flash-up-kf { from { background: rgba(111,170,132,.22); } to { background: transparent; } }
    @keyframes tk-flash-dn-kf { from { background: rgba(199,112,112,.22); } to { background: transparent; } }
    tr.tk-flash-up td { animation: tk-flash-up-kf 1.6s ease-out 1; }
    tr.tk-flash-dn td { animation: tk-flash-dn-kf 1.6s ease-out 1; }
    @media (prefers-reduced-motion: reduce) {
        tr.tk-flash-up td, tr.tk-flash-dn td { animation: none; }
    }
    .mini-index {
        padding: 12px 14px; margin-bottom: 10px; background: var(--card-lift);
        border: 1px solid var(--hairline); border-radius: 6px;
    }
    .mini-index.sunk { background: var(--sunken); }
    .mi-name { font-size: 10px; font-weight: 600; color: var(--dim); letter-spacing: 0.1em; text-transform: uppercase; }
    .mi-val {
        margin-top: 4px; font-family: var(--font-mono); font-size: 20px; color: var(--ink);
        letter-spacing: -0.01em; font-variant-numeric: tabular-nums;
    }
    .mi-sub { margin-top: 3px; font-family: var(--font-mono); font-size: 11.5px; }
    .mini-spark { display: block; width: 100%; height: 36px; margin-top: 8px; }
    .mi-kv { display: flex; justify-content: space-between; gap: 8px; margin-top: 8px;
             font-family: var(--font-mono); font-size: 12px; color: var(--ink); }
    .mi-kv .mi-name { font-size: 9px; margin-bottom: 2px; }

    /* Angel One (artboard 11): quiet status panel, masked credentials,
       activity log. */
    .ao-status {
        padding: 22px 24px; background: var(--card-lift);
        border: 1px solid var(--hairline); border-radius: 6px; margin-bottom: 12px;
    }
    .ao-line { display: flex; align-items: center; gap: 8px; }
    .ao-dot { width: 8px; height: 8px; border-radius: 50%; }
    .ao-label { font-size: 11px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; }
    .ao-h {
        margin: 12px 0 8px 0; font-family: var(--font-serif); font-size: 26px;
        line-height: 1.2; letter-spacing: -0.01em; color: var(--ink);
    }
    .ao-h em { color: var(--accent); }
    .ao-hint { margin: 0 0 18px 0; max-width: 62ch; font-size: 13px; line-height: 1.55; color: var(--dim); }
    .ao-stats {
        display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1px;
        background: var(--hairline); border: 1px solid var(--hairline); border-radius: 4px; overflow: hidden;
    }
    .ao-stat { padding: 11px 13px; background: var(--sunken); min-width: 0; }
    .ao-stat-k {
        font-size: 10px; font-weight: 600; color: var(--dim);
        letter-spacing: 0.1em; text-transform: uppercase;
    }
    .ao-stat-v {
        margin-top: 4px; font-family: var(--font-mono); font-size: 18px; color: var(--ink);
        font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .ao-stat-s { margin-left: 5px; font-size: 11px; color: var(--faint); }
    .ao-log { list-style: none; margin: 0; padding: 0; }
    .ao-log li {
        display: grid; grid-template-columns: 46px 1fr auto; gap: 10px; align-items: start;
        padding: 9px 0; border-bottom: 1px dotted var(--hairline); font-size: 13px;
    }
    .ao-log li:last-child { border-bottom: none; }
    .ao-lt { font-family: var(--font-mono); font-size: 11px; color: var(--dim); padding-top: 2px; }
    .ao-lk { font-weight: 600; color: var(--ink); }
    .ao-ls { margin-top: 2px; font-size: 11.5px; color: var(--dim); }
    .ao-log-empty { font-size: 12px; font-style: italic; color: var(--faint); padding: 4px 0; }
    .ao-steps { margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.6; color: var(--ink-mid); }
    .ao-steps li { margin-bottom: 8px; }
    .ao-steps b { color: var(--ink); }
    @media (max-width: 900px) { .ao-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }

    @media (max-width: 900px) {
        .gate-strip, .fu-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .gate-cell { border-bottom: 1px solid var(--hairline-soft); }
    }
    </style>"""
        .replace("/*__COLOR_TOKENS__*/", f"      /* colour tokens: dashboard/shared/tokens.py */\n{_css_vars()}")
        .replace("/*__NOISE_SVG__*/", _NOISE_SVG_URI),
        unsafe_allow_html=True,
    )




# ── Design helper functions (NSE Pro — from trading-dashboard-design skill) ───
def _glass_metric(label: str, value: str, delta: str = "", delta_pos: bool = True) -> str:
    d_color = "#6faa84" if delta_pos else "#c77070"
    d_sym   = "▲" if delta_pos else "▼"
    d_html  = (f'<div style="font-size:12px;color:{d_color};margin-top:4px;font-weight:600;'
               f'font-family:\'JetBrains Mono\',monospace">'
               f'{d_sym} {delta}</div>') if delta else ""
    return (
        f'<div class="glass-panel" style="text-align:center;min-height:80px">'
        f'<div style="font-size:11px;color:#5f636c;text-transform:uppercase;letter-spacing:1.2px;'
        f'font-weight:600">{label}</div>'
        f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:23px;font-weight:700;color:#e8e6e0;'
        f'margin-top:6px;letter-spacing:-.3px">{value}</div>'
        f'{d_html}</div>'
    )

def _section_div(label: str, icon: str = "") -> None:
    st.markdown(
        f'<div class="sec-div"><div class="sec-div-label">{icon}&nbsp;{label}</div>'
        f'<div class="sec-div-line"></div></div>',
        unsafe_allow_html=True,
    )

def _spacer(size: str = "md") -> None:
    px = {"sm": "12px", "md": "24px", "lg": "40px"}.get(size, "24px")
    st.markdown(f'<div style="height:{px}"></div>', unsafe_allow_html=True)

def _signal_card(ticker, action, price, entry, stop, target, reason, score=None, sector="") -> str:
    COLORS = {
        "BUY":  ("#6faa84", "rgba(111,170,132,.12)"),
        "SELL": ("#c77070", "rgba(199,112,112,.12)"),
        "HOLD": ("#d6a64c", "rgba(214,166,76,.12)"),
        "WATCH":("#c88a3f", "rgba(200,138,63,.12)"),
    }
    tc, bc = COLORS.get(action, COLORS["HOLD"])
    rr = (target - entry) / (entry - stop) if (entry - stop) > 0.01 else 0
    sc_html = (f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:28px;font-weight:700;'
               f'color:{tc}">{score}</div>'
               f'<div style="font-size:10px;color:#5f636c">SCORE</div>') if score is not None else ""
    sect_html = (f'<span style="font-size:11px;color:#5f636c;font-weight:400;margin-left:8px">{sector}</span>'
                 if sector else "")
    return (
        f'<div style="background:{bc};border:1px solid {tc}44;'
        f'border-left:3px solid {tc};border-radius:10px;padding:16px 20px;margin:8px 0;'
        f'display:flex;align-items:flex-start;gap:16px">'
        f'<div style="min-width:60px;text-align:center">{sc_html}'
        f'<div style="background:{bc};color:{tc};border:1px solid {tc};border-radius:6px;'
        f'padding:4px 10px;font-size:13px;font-weight:700;letter-spacing:1px;margin-top:4px">{action}</div></div>'
        f'<div style="flex:1">'
        f'<div style="font-size:18px;font-weight:700;color:#e8e6e0">{ticker}{sect_html}</div>'
        f'<div style="font-size:12px;color:#5f636c;margin:4px 0">{reason}</div>'
        f'<div style="display:flex;gap:20px;margin-top:10px;font-size:13px;font-family:\'JetBrains Mono\',monospace">'
        f'<div><span style="color:#5f636c;font-size:11px;font-family:\'Inter\'">'
        f'LTP</span><br><b style="color:#c3c1ba">₹{price:.2f}</b></div>'
        f'<div><span style="color:#5f636c;font-size:11px;font-family:\'Inter\'">'
        f'ENTRY</span><br><b style="color:#c3c1ba">₹{entry:.2f}</b></div>'
        f'<div><span style="color:#5f636c;font-size:11px;font-family:\'Inter\'">'
        f'STOP</span><br><b style="color:#c77070">₹{stop:.2f}</b></div>'
        f'<div><span style="color:#5f636c;font-size:11px;font-family:\'Inter\'">'
        f'TARGET</span><br><b style="color:#6faa84">₹{target:.2f}</b></div>'
        f'<div><span style="color:#5f636c;font-size:11px;font-family:\'Inter\'">'
        f'R:R</span><br><b style="color:{tc}">{rr:.1f}x</b></div>'
        f'</div></div></div>'
    )

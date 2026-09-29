"""dashboard/shared/cache.py - shared cached data + display/validation helpers.

FIXES applied in this revision
───────────────────────────────
C_TICKER  Tata Motors demerger correction (effective 1-Oct-2025), UPDATED: a
    second renaming since this fix was first written means the original
    comment below had the two resulting entities backwards. Per Yahoo
    Finance's own company-history field: the entity that kept continuous
    identity was renamed "Tata Motors Limited" → "Tata Motors Passenger
    Vehicles Limited" (PV + EVs + Jaguar Land Rover) and now trades under
    TMPV.NS — i.e. TMPV.NS is the *renamed continuation* of the original
    company, not a fresh spin-off. The commercial-vehicles business was
    the genuinely NEW entity (incorporated 2024 as "TML Commercial
    Vehicles Limited", renamed "Tata Motors Limited" again in Oct 2025)
    and trades under the brand-new ticker TMCV.NS. The OLD "TATAMOTORS"
    ticker itself is retired on Yahoo — neither successor trades under it,
    and any code still referencing TATAMOTORS.NS gets a hard 404. Both
    entities are now listed explicitly under their correct current tickers
    so users can disambiguate.

C_VEDANTA  Vedanta Ltd completed a four-way demerger effective 15-Jun-2026,
    splitting into Vedanta Aluminium Metal Ltd (VAML.NS), Vedanta Oil & Gas
    Ltd (VOGL.NS, ex-Malco Energy), Vedanta Power Ltd (VEDPOWER.NS, ex-Talwandi
    Sabo Power), and Vedanta Iron & Steel Ltd (VISL.NS), with VEDL.NS
    continuing as the residual critical-minerals/Hindustan Zinc entity.
    STOCK_SEARCH_MAP previously had NO entry for "Vedanta" at all (not even
    the parent) — that's why neither the parent nor any of the four new
    spin-offs could be found via company-name search anywhere in the app.
    All five are now listed explicitly. Note: as very recent listings, the
    four new entities may still have thin/incomplete daily-bar history on
    Yahoo Finance (the app's data source) for a while after listing — a
    "DATA_UNAVAILABLE" trend-quality score specifically on these names can
    be a genuine upstream data-depth gap (score_stock needs ~200 trading
    days for SMA_200, which a June-2026 listing doesn't have yet), not
    necessarily an app bug.

C1  _tomorrow_watchlist bucket assignment — added an explicit precedence
    comment documenting that the elif chain order is deliberate (breakout
    checked before reversal buckets), and added debug logging when a stock's
    score/momentum/tech combination would have matched more than one bucket,
    so threshold drift is visible in logs rather than silently invisible.

C2  Added warm_caches() — a callable entry point intended to be invoked by a
    scheduled job (cron / APScheduler) before market open, which pre-populates
    _home_top_picks and _tomorrow_watchlist so the first real user of the day
    never pays the ~2 min cold-scan cost. This file only provides the hook;
    wiring an actual scheduler is a deployment-level concern outside this
    module's scope.

C3  get_composite_score now wraps score_stock() in try/except and returns a
    sentinel CompositeScore-shaped object with action="UNAVAILABLE" on
    failure, consistent with every other scoring function in this file.

C4  _deep_confirmation now sanity-bounds earnings_days — a result more than
    100 days in the past is treated as a stale/incorrect upstream date and
    surfaced as None ("unknown") rather than as a confidently wrong negative
    number.

C5  load_ticker_df now logs a warning when the post-dropna frame is empty,
    so "why did this ticker return nothing" is traceable from one place
    instead of requiring each caller to add its own diagnostic.

C6  Extracted _score_to_dict(s) — a single shared helper that maps a
    CompositeScore object to the standard result dict. _score_for_cc,
    _score_watchlist, _home_top_picks, and _tomorrow_watchlist (which needs a
    slightly different field subset) all now route through it, so a future
    schema change only has to happen in one place.

C7  _tomorrow_watchlist now logs per-bucket result counts after each scan and
    warns if any bucket is empty, so silently-empty buckets (the original bug
    this function had) are visible in logs going forward rather than only
    discoverable by a user noticing an empty tab.

C11 _sector_ranks_tuple now distinguishes "no sector data available" (empty
    DataFrame, expected) from "sector ranking fetch failed" (exception,
    unexpected) via a log level distinction — exceptions log at WARNING,
    empty-but-successful results log at DEBUG. The return value is still ()
    in both cases (callers can't be changed without touching every scorer),
    but the failure mode is now distinguishable in logs.
"""
from __future__ import annotations
import os, sys, sqlite3, warnings, io, json, math, datetime
import logging
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import plotly.io as pio
from plotly.subplots import make_subplots
import streamlit as st
# FIX WARN1 — narrowed from a blanket `filterwarnings("ignore")` so numpy's
# RuntimeWarnings (invalid value / divide by zero / all-NaN slice) stay visible.
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import trade_store as _store
_log = logging.getLogger("dashboard.cache")


# ─────────────────────────────────────────────────────────────────────────────
# Company name → ticker map  (used for search autocomplete)
# ─────────────────────────────────────────────────────────────────────────────
STOCK_SEARCH_MAP = {
    # Large-cap / Nifty 50
    "Reliance Industries": "RELIANCE.NS",
    "Tata Consultancy Services (TCS)": "TCS.NS",
    "HDFC Bank": "HDFCBANK.NS",
    "Bharti Airtel": "BHARTIARTL.NS",
    "ICICI Bank": "ICICIBANK.NS",
    "Infosys": "INFY.NS",
    "State Bank of India (SBI)": "SBIN.NS",
    "Hindustan Unilever (HUL)": "HINDUNILVR.NS",
    "ITC": "ITC.NS",
    "Larsen & Toubro (L&T)": "LT.NS",
    "Kotak Mahindra Bank": "KOTAKBANK.NS",
    "Axis Bank": "AXISBANK.NS",
    "Bajaj Finance": "BAJFINANCE.NS",
    "Asian Paints": "ASIANPAINT.NS",
    "Maruti Suzuki": "MARUTI.NS",
    "NTPC": "NTPC.NS",
    "ONGC": "ONGC.NS",
    "Wipro": "WIPRO.NS",
    "UltraTech Cement": "ULTRACEMCO.NS",
    "Titan Company": "TITAN.NS",
    "HCL Technologies": "HCLTECH.NS",
    "Sun Pharma": "SUNPHARMA.NS",
    "Power Grid": "POWERGRID.NS",
    "Coal India": "COALINDIA.NS",
    "Nestle India": "NESTLEIND.NS",
    "Bajaj Finserv": "BAJAJFINSV.NS",
    # FIX C_TICKER: Tata Motors demerged effective 1-Oct-2025 into two
    # separately listed companies. The old combined entity no longer exists
    # as a single stock — searching "Tata Motors" must let the user pick
    # which successor business they mean.
    "Tata Motors (Commercial Vehicles - CV)": "TMCV.NS",
    "Tata Motors Passenger Vehicles (incl. JLR, EVs)": "TMPV.NS",
    "Adani Enterprises": "ADANIENT.NS",
    "JSW Steel": "JSWSTEEL.NS",
    "Grasim Industries": "GRASIM.NS",
    "Tech Mahindra": "TECHM.NS",
    "IndusInd Bank": "INDUSINDBK.NS",
    "Cipla": "CIPLA.NS",
    "Dr. Reddy's Laboratories": "DRREDDY.NS",
    "Eicher Motors": "EICHERMOT.NS",
    "Hindalco Industries": "HINDALCO.NS",
    "BPCL": "BPCL.NS",
    "Divi's Laboratories": "DIVISLAB.NS",
    "Tata Consumer Products": "TATACONSUM.NS",
    "Britannia Industries": "BRITANNIA.NS",
    "Hero MotoCorp": "HEROMOTOCO.NS",
    "Apollo Hospitals": "APOLLOHOSP.NS",
    "Bajaj Auto": "BAJAJ-AUTO.NS",
    "SBI Life Insurance": "SBILIFE.NS",
    "HDFC Life Insurance": "HDFCLIFE.NS",
    "Tata Power": "TATAPOWER.NS",
    "Adani Ports": "ADANIPORTS.NS",
    "Mahindra & Mahindra (M&M)": "M&M.NS",
    "LTIMindtree": "LTM.NS",
    "Shriram Finance": "SHRIRAMFIN.NS",
    # FIX C_VEDANTA: Vedanta Ltd demerged effective 15-Jun-2026 into a
    # residual entity (critical minerals / Hindustan Zinc) plus four newly
    # listed standalone businesses. None of these five had any entry here
    # before — "Vedanta" itself was missing entirely, not just the splits.
    "Vedanta Limited (residual — Zinc, Copper, Critical Minerals)": "VEDL.NS",
    "Vedanta Aluminium Metal": "VAML.NS",
    "Vedanta Oil & Gas": "VOGL.NS",
    "Vedanta Power": "VEDPOWER.NS",
    "Vedanta Iron & Steel": "VISL.NS",
    # Nifty Next 50
    "Cholamandalam Finance": "CHOLAFIN.NS",
    "Muthoot Finance": "MUTHOOTFIN.NS",
    "HDFC AMC": "HDFCAMC.NS",
    "ICICI Lombard": "ICICIGI.NS",
    "ICICI Prudential Life": "ICICIPRULI.NS",
    "SBI Cards": "SBICARD.NS",
    "Persistent Systems": "PERSISTENT.NS",
    "Coforge": "COFORGE.NS",
    "Mphasis": "MPHASIS.NS",
    "L&T Technology Services": "LTTS.NS",
    "Bosch India": "BOSCHLTD.NS",
    "TVS Motor Company": "TVSMOTOR.NS",
    "Bharat Electronics (BEL)": "BEL.NS",
    "Siemens India": "SIEMENS.NS",
    "ABB India": "ABB.NS",
    "Havells India": "HAVELLS.NS",
    "Voltas": "VOLTAS.NS",
    "Cummins India": "CUMMINSIND.NS",
    "Torrent Pharma": "TORNTPHARM.NS",
    "Aurobindo Pharma": "AUROPHARMA.NS",
    "Mankind Pharma": "MANKIND.NS",
    "Marico": "MARICO.NS",
    "Dabur India": "DABUR.NS",
    "Godrej Consumer Products": "GODREJCP.NS",
    "Colgate Palmolive": "COLPAL.NS",
    "United Spirits (McDowell's)": "UNITDSPR.NS",
    "Trent": "TRENT.NS",
    "Nykaa (FSN E-Commerce)": "NYKAA.NS",
    "Ambuja Cements": "AMBUJACEM.NS",
    "ACC": "ACC.NS",
    "Oberoi Realty": "OBEROIRLTY.NS",
    "DLF": "DLF.NS",
    "Adani Green Energy": "ADANIGREEN.NS",
    "PFC (Power Finance)": "PFC.NS",
    "REC Limited": "RECLTD.NS",
    "Canara Bank": "CANBK.NS",
    "Bank of Baroda": "BANKBARODA.NS",
    "Pidilite Industries": "PIDILITIND.NS",
    "Berger Paints": "BERGEPAINT.NS",
    "Indus Towers": "INDUSTOWER.NS",
    "Zydus Lifesciences": "ZYDUSLIFE.NS",
    "Lupin": "LUPIN.NS",
    "Lodha (Macrotech)": "LODHA.NS",
    "IRCTC": "IRCTC.NS",
    "Info Edge (Naukri)": "NAUKRI.NS",
    "Eternal Ltd (Zomato)": "ETERNAL.NS",
    # Midcap / Popular stocks
    "IDFC First Bank": "IDFCFIRSTB.NS",
    "Federal Bank": "FEDERALBNK.NS",
    "Bandhan Bank": "BANDHANBNK.NS",
    "AU Small Finance Bank": "AUBANK.NS",
    "Punjab National Bank (PNB)": "PNB.NS",
    "Union Bank of India": "UNIONBANK.NS",
    "IDBI Bank": "IDBI.NS",
    "RBL Bank": "RBLBANK.NS",
    "KPIT Technologies": "KPITTECH.NS",
    "Tata Elxsi": "TATAELXSI.NS",
    "Cyient": "CYIENT.NS",
    "Angel One": "ANGELONE.NS",
    "Balkrishna Industries (BKT)": "BALKRISIND.NS",
    "Exide Industries": "EXIDEIND.NS",
    "Ashok Leyland": "ASHOKLEY.NS",
    "Motherson Sumi (Samvardhana)": "MOTHERSON.NS",
    "Alkem Laboratories": "ALKEM.NS",
    "Glenmark Pharma": "GLENMARK.NS",
    "Granules India": "GRANULES.NS",
    "Laurus Labs": "LAURUSLABS.NS",
    "IPCA Laboratories": "IPCALAB.NS",
    "GlaxoSmithKline Pharma": "GLAXO.NS",
    "Natco Pharma": "NATCOPHARM.NS",
    "Varun Beverages (VBL)": "VBL.NS",
    "Radico Khaitan": "RADICO.NS",
    "Emami": "EMAMILTD.NS",
    "Avenue Supermarts (DMart)": "DMART.NS",
    "IndiaMART": "INDIAMART.NS",
    "Ramco Cements": "RAMCOCEM.NS",
    "JK Cement": "JKCEMENT.NS",
    "Astral Poly Technik": "ASTRAL.NS",
    "APL Apollo Tubes": "APLAPOLLO.NS",
    "BHEL": "BHEL.NS",
    "RVNL (Rail Vikas Nigam)": "RVNL.NS",
    "KEC International": "KEC.NS",
    "Thermax": "THERMAX.NS",
    "NBCC India": "NBCC.NS",
    "Container Corporation (CONCOR)": "CONCOR.NS",
    "IRFC": "IRFC.NS",
    "IGL (Indraprastha Gas)": "IGL.NS",
    "MGL (Mahanagar Gas)": "MGL.NS",
    "Petronet LNG": "PETRONET.NS",
    "GAIL India": "GAIL.NS",
    "NHPC": "NHPC.NS",
    "SJVN": "SJVN.NS",
    "HPCL": "HINDPETRO.NS",
    "Indian Oil (IOC)": "IOC.NS",
    "Suzlon Energy": "SUZLON.NS",
    "Hindustan Zinc": "HINDZINC.NS",
    "NMDC": "NMDC.NS",
    "SAIL (Steel Authority)": "SAIL.NS",
    "Godrej Properties": "GODREJPROP.NS",
    "Phoenix Mills": "PHOENIXLTD.NS",
    "Prestige Estates": "PRESTIGE.NS",
    "Sobha Developers": "SOBHA.NS",
    "Aarti Industries": "AARTIIND.NS",
    "Deepak Nitrite": "DEEPAKNTR.NS",
    "SRF": "SRF.NS",
    "CDSL (Depository)": "CDSL.NS",
    "BSE": "BSE.NS",
    "MCX (Multi Commodity Exchange)": "MCX.NS",
    "CAMS": "CAMS.NS",
    "Max Healthcare": "MAXHEALTH.NS",
    "Fortis Healthcare": "FORTIS.NS",
    "Dr. Lal PathLabs": "LALPATHLAB.NS",
    "Metropolis Healthcare": "METROPOLIS.NS",
    "Indian Hotels (Taj)": "INDHOTEL.NS",
    "Polycab India": "POLYCAB.NS",
    "Dixon Technologies": "DIXON.NS",
    "Page Industries (Jockey)": "PAGEIND.NS",
    "MRF": "MRF.NS",
    "Jubilant Foodworks (Dominos)": "JUBLFOOD.NS",
    "Tata Communications": "TATACOMM.NS",
    "Sun TV Network": "SUNTV.NS",
    "Manappuram Finance": "MANAPPURAM.NS",
    "Tatasteel": "TATASTEEL.NS",
    # User portfolio stocks
    "Balrampur Chini Mills": "BALRAMCHIN.NS",
    "Xchanging Solutions": "XCHANGING.NS",
    "Bajaj Hindusthan Sugar": "BAJAJHIND.NS",
    "Dhanlaxmi Bank": "DHANBANK.NS",
}

# Reverse lookup: ticker → display name
_TICKER_TO_NAME = {v: k for k, v in STOCK_SEARCH_MAP.items()}


def get_display_name(ticker: str) -> str:
    t = ticker if ticker.endswith(".NS") else ticker + ".NS"
    return _TICKER_TO_NAME.get(t, ticker.replace(".NS", ""))


def _validate_ticker(raw: str):
    """
    Validate a user-entered NSE symbol before any API call.
    Returns (cleaned_symbol, error_message_or_None). cleaned_symbol has no
    .NS/.BO suffix (callers add it). Allows letters, digits, '-' and '&'
    (e.g. RELIANCE, M&M, BAJAJ-AUTO).
    """
    t = (raw or "").strip().upper().replace(" ", "")
    if not t:
        return "", None
    t = t.replace(".NS", "").replace(".BO", "")
    if not all(c.isalnum() or c in "-&" for c in t):
        return t, (f"'{t}' doesn't look like a valid NSE symbol "
                   f"(letters/digits only, e.g. RELIANCE, M&M, BAJAJ-AUTO).")
    if len(t) > 20:
        return t, f"'{t[:20]}…' is too long for an NSE symbol."
    return t, None


def _plain_english(action: str, entry: float, sl: float, tp: float, rr: float) -> str:
    """One-line plain-English description of the setup for non-traders.

    Descriptive only (CLAUDE.md rule 1): it states what the trend and the
    model's reference levels ARE — never what the reader should do. Levels
    are framed as the model's reference points, not an order ticket.
    """
    risk_amt = entry - sl
    rew_amt  = tp - entry
    levels = (f"Model reference levels: entry <b>₹{entry:,.2f}</b>, invalidation "
              f"<b>₹{sl:,.2f}</b> (≈ ₹{risk_amt:,.2f}/share away), target "
              f"<b>₹{tp:,.2f}</b> (≈ ₹{rew_amt:,.2f}/share away) — a 1 : {rr:.1f} "
              f"risk-to-reward structure.")
    if action in ("STRONG BUY", "BUY"):
        return f"📈 <b>Trend quality is strong.</b> Price structure and momentum are aligned. {levels}"
    if action == "WATCHLIST":
        return ("👀 <b>Trend is forming but not confirmed.</b> Several components are "
                "positive, but the score sits below the strong-trend band.")
    if action == "HOLD":
        return ("🟡 <b>Neutral trend.</b> No clear edge in either direction on the "
                "current readings.")
    if action == "CAUTION":
        return ("⚠️ <b>Momentum is fading.</b> The trend is weakening and the score is "
                "in the deteriorating band.")
    if action == "EXIT":
        return ("🔴 <b>Trend is broken.</b> Price is below key averages and momentum is "
                "negative — the weakest band of the score.")
    return "This stock is in a neutral zone — no strong edge either way on current readings."


def _trade_type(headline: str) -> tuple:
    """
    Categorise a setup into a trade type from its narrative headline.
    Returns (label, emoji, color). Zero extra data needed.
    """
    h = (headline or "").lower()
    if any(k in h for k in ("breakout", "52-week high", "52w high", "new high", "all-time high")):
        return ("Breakout", "🚀", "#00d4aa")
    if any(k in h for k in ("oversold", "bounce", "reversal", "support")):
        return ("Oversold Bounce", "🔄", "#5b8def")
    if any(k in h for k in ("momentum", "uptrend", "above sma", "strong trend", "trending")):
        return ("Momentum", "📈", "#c88a3f")
    if any(k in h for k in ("pullback", "dip")):
        return ("Pullback", "🎯", "#a78bfa")
    return ("Trend", "•", "#8899bb")


def _horizon_countdown(valid_until: str) -> str:
    """Render a pick's valid_until as a live countdown instead of a static
    date. The underlying date doesn't change between visits, but 'stale
    after 19 Aug' reads as fixed/dead on every reload — 'expires in 4 days'
    visibly counts down, and flips to a red warning once the window has
    actually passed, without any new data (valid_until was already computed
    by analysis.score._pick_horizon; this just re-reads it against today's
    date at render time)."""
    if not valid_until:
        return ""
    try:
        import datetime as _dtm
        _d = _dtm.date.fromisoformat(valid_until)
    except Exception:
        return f"stale after {valid_until}"
    import datetime as _dtm
    _days = (_d - _dtm.date.today()).days
    if _days < 0:
        return '<span style="color:#c77070;font-weight:700">⚠ past its window — reassess</span>'
    if _days == 0:
        return '<span style="color:#FF9800;font-weight:700">⚠ expires today</span>'
    if _days <= 2:
        return f'<span style="color:#FF9800;font-weight:600">expires in {_days}d</span>'
    return f'<span style="color:#6a8caf">expires in {_days}d</span>'


# ─────────────────────────────────────────────────────────────────────────────
# FIX CC-LIVE1 — bounded live price for Buy/Sell cards
#
# FIX CC-LOAD1 (see 02_command_centre.py) pulled per-pick live price, SL/TP
# re-anchoring AND qty sizing out of this section entirely, because doing
# all three for up to ~40 tickers on every rerun of a 20s-refreshing fragment
# was real, unbounded network cost paid whether or not anyone was looking.
# The Top Picks ticker strip (_top_picks_ticker, above) proved a safe middle
# ground since: ONE tiered batch call (Angel One -> Yahoo -> NSE -> Stooq,
# same as this), cached 60s so a 20s-refreshing enclosing fragment doesn't
# turn into a 20s-refreshing live-price fetch. This reuses that exact pattern
# for the full Buy/Sell card list (still <= ~40 tickers, one call, Angel
# One's batch endpoint handles up to 50/call) — PRICE ONLY. Qty and SL/TP
# re-anchoring are deliberately NOT added back here; those stay in Deep
# Dive's on-demand Live Snapshot, which is the boundary FIX CC-LOAD1 drew
# for good reason (position sizing off a stale-by-seconds price is a much
# worse failure mode than a stale-by-seconds price label).
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=60, show_spinner=False)
def _picks_live_prices(tickers: tuple) -> dict:
    """Live price + %chg for exactly the tickers currently on screen in
    Command Centre's Buy/Sell cards. Returns {} entries silently omitted for
    any ticker no tier could resolve — callers fall back to '(last close)'
    per-ticker rather than failing the whole section."""
    if not tickers:
        return {}
    from utils.live_price import get_live_prices_batch
    try:
        raw = get_live_prices_batch(list(tickers), max_workers=20)
    except Exception as _e:
        _log.debug("cache._picks_live_prices: batch fetch failed: %s", _e)
        return {}
    return {t: d for t, d in (raw or {}).items() if d and d.get("price")}


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_ticker_df(ticker: str, period: str = "2y") -> pd.DataFrame:
    """
    Fetch OHLCV + compute all technical indicators.

    Always fetches at least 2 years so that SMA_200, RSI(14), MACD(26) etc.
    are valid at the *most recent* row.  The UI chart period controls what
    slice is *displayed*, not how much data is loaded.

    FIX C5: logs a warning when the post-dropna frame is empty (typically
    new listings with < 200 trading days of history), so this is traceable
    from one place rather than every caller needing its own diagnostic.
    """
    from data.fetcher import fetch_single
    from utils.indicators import add_all_indicators
    df = fetch_single(ticker, period=period)
    df = add_all_indicators(df)
    _pre_drop_len = len(df)
    # Drop warm-up rows where core indicators are NaN so iloc[-1] is always valid
    df.dropna(subset=["RSI", "ATR", "SMA_200"], inplace=True)
    if df.empty and _pre_drop_len > 0:
        # FIX C5: previously silent — now traceable in logs
        _log.warning(
            "cache.load_ticker_df(%s, period=%s): %d rows fetched but 0 "
            "remain after dropping NaN warm-up rows (needs SMA_200 → "
            "≥200 trading days). Likely a new listing or data gap.",
            ticker, period, _pre_drop_len,
        )
    return df


def _trim_to_period(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """
    Return a date-sliced copy of df matching the UI display period.
    Indicators were computed on the full dataset so they remain accurate
    at the most-recent row after slicing.

    FIX TZ1 (2026-09-05): the YTD branch built a NAIVE pd.Timestamp
    (`pd.Timestamp(year, 1, 1)`) and compared it against the df's
    DatetimeIndex. When the fetched frame comes back tz-aware (Angel
    One + newer yfinance both return UTC+05:30 IST index), pandas
    raises `InvalidComparison: Cannot compare tz-naive and tz-aware
    datetime-like objects` — bubbles all the way up as "Analysis
    failed" on every ticker in Analyze Stock. Match the index's tz
    when building the cutoff.
    """
    if df.empty:
        return df
    last_ts = df.index[-1]
    _DAYS = {"1d": 8, "5d": 12, "1m": 35, "6m": 185, "1y": 375, "2y": 740}
    if period in _DAYS:
        cutoff = last_ts - pd.Timedelta(days=_DAYS[period])
        return df[df.index >= cutoff]
    if period == "ytd":
        _tz = getattr(df.index, "tz", None)
        cutoff = pd.Timestamp(last_ts.year, 1, 1, tz=_tz) if _tz is not None \
                 else pd.Timestamp(last_ts.year, 1, 1)
        return df[df.index >= cutoff]
    return df  # "max" or anything else → full history


@st.cache_data(ttl=600)
def load_vix_data():
    """Load VIX + Nifty daily history via Stooq (no rate limits on cloud)."""
    from data.fetcher import fetch_single
    try:
        vix   = fetch_single("^INDIAVIX", period="1y")
    except Exception as _e:
        _log.debug("cache.%s degraded: %s", "load_vix_data", _e)
        vix   = pd.DataFrame()
    try:
        nifty = fetch_single("^NSEI", period="1y")
    except Exception as _e:
        _log.debug("cache.%s degraded: %s", "load_vix_data", _e)
        nifty = pd.DataFrame()
    return vix, nifty


# ─────────────────────────────────────────────────────────────────────────────
# CC_TICKER — fast, parallel, GAINERS-ONLY snapshot for Command Centre's
# ticker strip.
#
# Previously the strip mixed the user's raw watchlist (which can legitimately
# include stocks that are down today) with a handful of Top Picks buy
# candidates, and priced them via trade_utils._portfolio_live_prices — which
# fetches one ticker at a time in a plain for-loop (see FIX TU4 in
# trade_utils.py), not in parallel. Two separate bugs from one root cause:
# (1) a "suggestions" strip that could show loss-making stocks because it was
# never actually gainers-only, it was "whatever's on your watchlist", and
# (2) real load-time cost from N sequential fetches instead of one batch call.
#
# Fixed by decoupling entirely: this scans only NIFTY 50 (fast, well-known,
# matches "like the ticker for nifty50 stocks") via get_live_prices_batch,
# the SAME parallel Angel-One-batch → threaded-Yahoo-fallback path Market
# Live already uses for its ~750-stock snapshot — just a much smaller list,
# so it's fast even on a cold cache. Sorted by today's % change and filtered
# to positive movers only, so what's shown is honestly "today's gainers",
# not a random mix that can include red numbers.
@st.cache_data(ttl=60, show_spinner=False)
def _nifty50_gainers_ticker(n: int = 12) -> list:
    """Top N NIFTY 50 gainers today, sorted best-first. Positive movers only —
    if the market is broadly red, this can legitimately return fewer than n
    (or none); it does not backfill with losers to hit a count."""
    from data.universe import get_universe as _gu
    from utils.live_price import get_live_prices_batch as _batch

    tickers = _gu("nifty50")
    raw = _batch(tickers, max_workers=20)

    rows = []
    for _t, _d in (raw or {}).items():
        if not _d:
            continue
        _price = _d.get("price")
        _chg   = _d.get("chg_pct")
        if _price is None or _chg is None:
            continue
        if _chg > 0:
            rows.append({"ticker": _t, "price": _price, "chg_pct": _chg})

    rows.sort(key=lambda r: -r["chg_pct"])
    return rows[:n]


# ─────────────────────────────────────────────────────────────────────────────
# FIX TP3 — Top Picks ticker strip (replaces the NIFTY 50 gainers strip above)
#
# _nifty50_gainers_ticker (above) showed generic NIFTY 50 gainers — informative,
# but disconnected from the app's own analysis. This scans the same Top Picks
# buy list shown in the Buy Candidates cards further down Command Centre
# (get_top_picks(), already fast-path cached from the 15-min scheduled scan —
# see FIX SPEED1), then prices those specific tickers with ONE parallel batch
# call, same pattern as _nifty50_gainers_ticker. Buys only, in the same
# score-ranked order get_top_picks() returns — no sells mixed in. Unlike the
# gainers strip this is NOT filtered to positive movers: a Top Pick can be a
# genuine buy setup on a day it's flat or slightly red (e.g. pulled back to a
# support level), so the strip shows its real live % change, colour-coded.
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=60, show_spinner=False)
def _top_picks_ticker(n: int = 12) -> list:
    """Top N Top-Picks BUY candidates for Command Centre's ticker strip,
    each priced live via one parallel batch call. Score-ranked order (same
    as get_top_picks()), not re-sorted by today's % change.

    FIX TP-LIVE-SHARE: previously called get_live_prices_batch() directly with
    ONLY this strip's 12 tickers, producing a cache entry that could not be
    reused by the Buy/Sell cards section further down the page (whose call to
    _picks_live_prices uses the union of buys+sells — a different tuple key,
    ergo a separate network fetch). Both fire on the same ~60s cadence during
    market hours, so the app was paying two full batch-quote round-trips per
    minute for overlapping tickers. Now the strip calls _picks_live_prices
    with the SAME union tuple the section will request, so both consumers
    hit exactly one shared cache entry. The strip still displays only the top
    n buys — it just doesn't waste the section's future fetch.
    """
    try:
        picks = get_top_picks()
    except Exception as _e:
        _log.debug("cache._top_picks_ticker: get_top_picks failed: %s", _e)
        return []

    buys_all = (picks or {}).get("buys") or []
    sells_all = (picks or {}).get("sells") or []
    if not buys_all:
        return []

    # Union tuple matches what _render_top_picks_section builds in
    # 02_command_centre.py (see FIX CC-LIVE1 there) — same order, same
    # dedupe, so this hits the same _picks_live_prices cache entry.
    union = tuple(sorted({b["ticker"] for b in buys_all} |
                         {s["ticker"] for s in sells_all}))
    raw = _picks_live_prices(union)

    rows = []
    for b in buys_all[:n]:
        d = (raw or {}).get(b["ticker"])
        if not d:
            continue
        price = d.get("price")
        chg   = d.get("chg_pct")
        if price is None or chg is None:
            continue
        rows.append({"ticker": b["ticker"], "price": price, "chg_pct": chg, "score": b.get("score")})
    return rows


@st.cache_data(ttl=600)
def get_vix_info():
    # Route through utils.vix — has 10-min TTL and proper crumb auth
    # (trading.signals had a missing urllib.request import bug)
    try:
        from utils.vix import get_india_vix_regime
        return get_india_vix_regime()
    except Exception as _e:
        _log.debug("cache.%s degraded: %s", "get_vix_info", _e)
        return {"vix": 18.0, "regime": "normal", "allow_buy": True, "vix_pct_chg": 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# FIX C6 — single shared CompositeScore → dict mapper
# ─────────────────────────────────────────────────────────────────────────────

def _score_to_dict(s, extended: bool = False) -> dict:
    """
    Map a CompositeScore object to the standard result dict used across
    _score_for_cc, _score_watchlist, _home_top_picks, and _tomorrow_watchlist.

    FIX C6: previously each of those four functions duplicated this mapping
    independently — any schema change (new field, renamed field) had to be
    made in four places by hand. Now it's made here once.

    extended=True adds the richer fields _home_top_picks needs (narrative,
    sector, component scores) on top of the base fields every caller wants.
    """
    base = {
        "ticker":   s.ticker if hasattr(s, "ticker") else None,
        "price":    s.price,
        "score":    s.score,
        "grade":    s.grade,
        "action":   s.action,
        "headline": s.headline,
        "entry":    s.entry,
        "sl":       s.stop_loss,
        "tp":       s.target,
        "rr":       s.risk_reward,
    }
    if extended:
        base.update({
            "narrative": getattr(s, "narrative", ""),
            "sector":    getattr(s, "sector", ""),
            "technical": getattr(s, "technical_score", 0),
            "momentum":  getattr(s, "momentum_score", 0),
            "volume":    getattr(s, "volume_score", 0),
            "sentiment": getattr(s, "sentiment_score", 0),
            "horizon":     getattr(s, "horizon", ""),      # FIX HZ1
            "valid_until": getattr(s, "valid_until", ""),  # FIX HZ1
        })
    return base


def _unavailable_dict(ticker: str, reason: str = "", extended: bool = False) -> dict:
    """Sentinel result dict for a ticker that failed to score. FIX C6 / C3."""
    base = {
        "ticker": ticker, "price": 0, "score": 0, "grade": "?",
        "action": "UNAVAILABLE",
        "headline": f"Data unavailable{f' ({reason})' if reason else ''}",
        "entry": 0, "sl": 0, "tp": 0, "rr": 0,
    }
    if extended:
        base.update({
            "narrative": "", "sector": "",
            "technical": 0, "momentum": 0, "volume": 0, "sentiment": 0,
            "horizon": "", "valid_until": "",  # FIX HZ1
        })
    return base


# PATCH 3a: TTL reduced from 1800 → 300 (5 min) so single-stock scores
# used by the watchlist stay near-live during market hours instead of
# showing 30-min-old data.
@st.cache_data(ttl=300, show_spinner=False)
def _score_for_cc(ticker: str, vix_regime: str = "normal") -> dict:
    """Score one stock for Command Centre. Pass vix_regime so we don't re-fetch VIX 5×."""
    try:
        import sys, os as _os
        sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        from analysis.score import score_stock
        _vix_info = {
            "regime": vix_regime, "vix": None,
            "allow_buy": vix_regime not in ("fear", "panic"),
        }
        s = score_stock(ticker, vix_info=_vix_info)
        # FIX C6: route through the shared mapper
        _d = _score_to_dict(s)
        _d["ticker"] = ticker
        return _d
    except Exception as _e:
        # FIX C6: route through the shared sentinel
        return _unavailable_dict(ticker, f"{type(_e).__name__}: {str(_e)[:70]}")


# PATCH 3b: TTL reduced from 1800 → 300 (5 min) — watchlist scores now
# refresh every 5 min during market hours, matching Top Picks cadence.
@st.cache_data(ttl=300, show_spinner=False)
def _score_watchlist(tickers: tuple, vix_regime: str = "normal", sector_ranks: tuple = ()) -> dict:
    """
    Score a whole watchlist IN PARALLEL (one thread per stock) and cache the
    result for 5 min. Calls score_stock directly (not the cached single-stock
    wrapper) so it is safe to run inside worker threads. Sector strength is
    folded in via sector_ranks. Returns {ticker: score_dict}.
    """
    import concurrent.futures as _cf
    import sys, os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

    _vix = {"regime": vix_regime, "vix": None,
            "allow_buy": vix_regime not in ("fear", "panic")}
    _sec_df = _sector_df_from_tuple(sector_ranks)

    def _one(tk):
        try:
            from analysis.score import score_stock
            s = score_stock(tk, vix_info=_vix, sector_scores_df=_sec_df)
            # FIX C6: route through the shared mapper
            _d = _score_to_dict(s)
            _d["ticker"] = tk
            return tk, _d
        except Exception as e:
            return tk, _unavailable_dict(tk, type(e).__name__)

    out: dict = {}
    if not tickers:
        return out

    # FIX WL-SNAP1: serve as many tickers as possible from the persisted
    # all-scored snapshot (written every 15 min by scripts/warm_top_picks.py
    # — same run as the top-picks snapshot). Watchlists are almost always a
    # subset of the niftytotalmarket universe the warmer already scores, so
    # the typical case is zero live work. Live scoring only fires for
    # tickers not covered by the snapshot (e.g. an off-universe symbol the
    # user has added by hand, or when the snapshot is stale/missing).
    #
    # Correctness note: the snapshot's score dict was produced by the SAME
    # _score_to_dict path _one() below uses (just with extended=True), so
    # every field callers read here (ticker/price/score/grade/action/
    # headline/entry/sl/tp/rr) is present with identical semantics. The
    # extra extended fields on snapshot entries (sector/narrative/etc.) are
    # a superset and safely ignored by callers that don't need them.
    _snap = _persisted_all_scores_snapshot()
    _pending: list = []
    if _snap:
        for tk in tickers:
            if tk in _snap:
                out[tk] = _snap[tk]
            else:
                _pending.append(tk)
    else:
        _pending = list(tickers)

    if not _pending:
        return out

    try:
        with _cf.ThreadPoolExecutor(max_workers=min(8, max(1, len(_pending)))) as ex:
            for tk, sc in ex.map(_one, _pending):
                out[tk] = sc
    except Exception as _e:
        _log.debug("cache.%s degraded: %s", "_score_watchlist", _e)
        for tk in _pending:
            _tk, _sc = _one(tk)
            out[_tk] = _sc
    return out


# Curated liquid large/mid-cap universe for the home-page "Top Picks" scan.
# Kept ~36 names so a full scan finishes fast (Angel One: ~15-25 s) and stays cached.
# NOTE (FIX C_TICKER): the old TATAMOTORS.NS ticker is retired on Yahoo (see
# the C_TICKER note above) and was swapped for TMPV.NS here — the renamed
# continuation of the original entity (PV + EVs + Jaguar Land Rover), and by
# far the larger/more market-cap-relevant of the two post-demerger entities,
# which is what a "top blue-chip picks" list should track. The smaller,
# domestic-only commercial-vehicles entity (TMCV.NS) is tracked separately
# in the sector-rotation / auto-sector groupings instead.
_HOME_SCAN_UNIVERSE = [
    "RELIANCE.NS","TCS.NS","HDFCBANK.NS","ICICIBANK.NS","INFY.NS","SBIN.NS",
    "BHARTIARTL.NS","LT.NS","ITC.NS","AXISBANK.NS","KOTAKBANK.NS","HINDUNILVR.NS",
    "BAJFINANCE.NS","MARUTI.NS","SUNPHARMA.NS","TMPV.NS","NTPC.NS","TITAN.NS",
    "ULTRACEMCO.NS","ASIANPAINT.NS","WIPRO.NS","ADANIENT.NS","JSWSTEEL.NS","POWERGRID.NS",
    "TATASTEEL.NS","HCLTECH.NS","ONGC.NS","COALINDIA.NS","BAJAJFINSV.NS","TECHM.NS",
    "DRREDDY.NS","CIPLA.NS","HINDALCO.NS","GRASIM.NS","EICHERMOT.NS","TRENT.NS",
]


def _sector_df_from_tuple(sector_ranks: tuple):
    """Rebuild a sector-rank DataFrame (index=sector, col=Rank) from a hashable tuple."""
    if not sector_ranks:
        return None
    try:
        return pd.DataFrame(
            [{"Rank": int(r)} for _, r in sector_ranks],
            index=[str(s) for s, _ in sector_ranks],
        )
    except Exception as _e:
        _log.debug("cache.%s degraded: %s", "_sector_df_from_tuple", _e)
        return None


# PATCH 3c: TTL reduced from 1800 → 300 (5 min) so Top Picks auto-refresh
# every 5 min during market hours without needing a manual "Scan Now" click.
# First scan still takes ~2 min; every reload within 5 min is instant from cache.
# FIX 1: Removed duplicate @st.cache_data decorator (was stacked twice — caused
# the cached result to be wrapped in an extra layer and never properly invalidated).
@st.cache_data(ttl=300, show_spinner=False)
def _home_top_picks(vix_regime: str = "normal", n: int = 20, sector_ranks: tuple = ()) -> dict:
    """
    Scan the FULL NSE universe (~745 liquid large/mid/small/micro-caps) and
    return the strongest long candidates and the clearest SELL/EXIT
    candidates for the day.

    Each stock's CompositeScore folds in trend, momentum, RSI, volume, VIX
    sentiment AND sector strength (via sector_ranks) — "self-analysis +
    volatility" in one number. Returns {"buys": [...], "sells": [...]}.

    Cached 5 min: the first scan takes ~2 min (parallelised), every rerun
    within 5 min is instant. Cache auto-expires so picks stay live during
    market hours without needing a manual "Scan Now" click.

    FIX C2: see warm_caches() below for a hook intended to be invoked by a
    scheduled job before market open, so this 2-min cold-scan cost is paid
    by a background job rather than the first real user of the day.

    FIX TP2 (universe widen + count raise): previously scanned only
    get_universe("nifty500") (~504 tickers, matching NSE's real Nifty 500
    index) and capped results at n=10 buys / 10 sells. Now scans
    get_universe("niftytotalmarket") (~745 tickers — nifty500 + the
    NIFTY_MICROCAP250 band added in the universe-expansion work) and
    defaults to n=20 each, so the section actually reflects the wider
    universe data/universe.py already built. Scan time scales with universe
    size (still parallelised, still covered by the 15-min scheduled
    pre-warm job in scripts/warm_top_picks.py — see that file's matching
    n=20 update).
    """
    import concurrent.futures as _cf
    import sys, os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from data.universe import get_universe
    _UNIV = get_universe("niftytotalmarket")  # the repo's full liquid NSE universe (~745)
    buys, sells = [], []

    _vix = {"regime": vix_regime, "vix": None,
            "allow_buy": vix_regime not in ("fear", "panic")}
    _sec_df = _sector_df_from_tuple(sector_ranks)

    def _one(tk):
        """Score directly via score_stock (not the cached wrapper) — safe in threads."""
        try:
            from analysis.score import score_stock
            s = score_stock(tk, vix_info=_vix, sector_scores_df=_sec_df)
            # FIX C6: route through the shared mapper (extended fields needed here)
            _d = _score_to_dict(s, extended=True)
            _d["ticker"] = tk
            return _d
        except Exception as _e:
            _log.debug("cache._home_top_picks._one degraded for %s: %s", tk, _e)
            return _unavailable_dict(tk, type(_e).__name__, extended=True)

    results = []
    try:
        # FIX TP5: 10 -> 16 workers. Was fine at ~2 min for the old 504-
        # ticker nifty500 scan; after FIX TP2 widened this to niftytotalmarket
        # (~745), 10 workers pushed the scan toward ~3 min, tight against the
        # 5-min GitHub Actions cadence this pairs with (see warm-top-picks.yml
        # FIX TP5 comment). 16 is a deliberately moderate bump, not a large
        # one — Angel One's documented rate limiting is on get_batch_quotes
        # and the token-lookup path (see data/angel_fetcher.py's AO1/AO2 fix
        # notes), not necessarily the per-symbol quote endpoint this loop
        # uses via score_stock(), but that boundary isn't fully
        # characterised, so this errs cautious rather than maximising
        # throughput.
        with _cf.ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(_one, _UNIV))
    except Exception as _e:
        _log.debug("cache.%s degraded: %s", "_home_top_picks", _e)
        results = [_one(tk) for tk in _UNIV]

    # FIX TP-HEALTH1: track per-scan counts so the app (and warmer logs) can
    # tell "no picks because market is quiet" apart from "no picks because
    # 40 % of the universe failed to score". Previously _home_top_picks
    # silently dropped every failed ticker and returned a shorter list with
    # no signal, so Stooq/Yahoo/Angel degradations only surfaced when a user
    # noticed the Buy Candidates column had shrunk. Counts now flow through
    # meta and are rendered as a small chip on Command Centre when the
    # unavailable-fraction crosses UNAVAIL_WARN_FRACTION.
    _n_scanned = len(results)
    _n_unavail = 0
    for s in results:
        act = s.get("action", "")
        sc  = s.get("score", 0)
        # FIX 2: Skip zero-score AND unavailable results — score_stock returns
        # score=0 with action=DATA_UNAVAILABLE when data fetch fails. Previously
        # score <= 0 guard was missing the DATA_UNAVAILABLE string check fully.
        if sc <= 0 or act in ("UNAVAILABLE", "DATA_UNAVAILABLE"):
            _n_unavail += 1
            continue
        # Long side: STRONG BUY / BUY / WATCHLIST all surface so we always
        # have enough candidates. Cards show each stock's true action label.
        if act in ("STRONG BUY", "BUY", "WATCHLIST"):
            # FIX TP1 (backend side): the Command Centre page already expects
            # each buy to carry a "tier" field and the response to carry a
            # "meta" dict (see the FIX TP1 comment in
            # dashboard/pages/02_command_centre.py) — this function never
            # actually set either, so _picks.get("meta", {}) was always {},
            # which made the page's elif branch fire on EVERY scan that found
            # at least one buy ("0 genuine strong BUY-grade setup(s) today"),
            # even when the top picks were real STRONG BUY / BUY setups. That
            # made every scan read as if it were all weak backfill, which is
            # what looked like "no positive-return stocks" to the user.
            s["tier"] = "watch" if act == "WATCHLIST" else "strong"
            buys.append(s)
        elif act in ("EXIT", "CAUTION"):
            sells.append(s)

    buys.sort(key=lambda x: -x.get("score", 0))
    sells.sort(key=lambda x: x.get("score", 0))   # lowest score = weakest first

    buys = buys[:n]
    _n_strong = sum(1 for b in buys if b.get("tier") == "strong")
    _n_ok = _n_scanned - _n_unavail
    # FIX TP-VIX1: stamp the input vix_regime onto the payload so callers can
    # see WHICH regime the picks were scored under. This matters because the
    # persisted snapshot (written every 15 min) is served regardless of the
    # caller's current vix_regime, so if VIX flips calm→fear intraday the
    # served picks may reflect the old regime for up to 20 min. Recording it
    # lets the page show "scored under: calm regime" and warn on mismatch.
    meta = {
        "no_strong_picks": _n_strong == 0,
        "n_strong_buys": _n_strong,
        "n_scanned": _n_scanned,
        "n_scored_ok": _n_ok,
        "n_unavailable": _n_unavail,
        "vix_regime": vix_regime,
    }
    # FIX WL-SNAP1: return the FULL scored map (ticker -> score_dict) for
    # every ticker the scan produced a valid score for, so watchlist scoring
    # (which is almost always a subset of niftytotalmarket) can be served
    # instantly from this same 15-min scan instead of paying its own live
    # scoring cost. Kept out of `meta` to avoid inflating the top-picks
    # snapshot readers touch every render — the warmer writes this to a
    # separate KV entry that only _persisted_all_scores_snapshot reads.
    _all_scored = {
        s["ticker"]: s for s in results
        if s.get("ticker")
        and s.get("score", 0) > 0
        and s.get("action", "") not in ("UNAVAILABLE", "DATA_UNAVAILABLE")
    }
    return {"buys": buys, "sells": sells[:n], "meta": meta, "all_scored": _all_scored}


# ─────────────────────────────────────────────────────────────────────────────
# FIX SPEED1 — persisted Top Picks (killing the 2-min cold-scan wait)
# ─────────────────────────────────────────────────────────────────────────────
_TOP_PICKS_KV_KEY  = "top_picks_snapshot"
_TOP_PICKS_KV_USER = "_system"          # not per-user — one shared scan result
_TOP_PICKS_MAX_AGE_SECONDS = 1200       # 20 min — tolerates one missed 15-min cron tick
# FIX SPEED-DEV1 (2026-09-15): NSE_ACCEPT_STALE_SNAPSHOTS=1 makes every
# persisted-snapshot age check pass regardless of wall-clock age. For local
# dev where the GH-Actions warmer never runs against your machine's KV, a
# stale snapshot is dramatically better than paying the 2-min live-scan
# tax on every Command Centre / Watchlist page load. Prod stays on the
# 20-min tick unless someone deliberately opts in.
_ACCEPT_STALE = os.environ.get("NSE_ACCEPT_STALE_SNAPSHOTS", "").lower() in ("1", "true", "yes")

# FIX WL-SNAP1: separate KV entry for the full scored map. Kept out of the
# top-picks snapshot so the small/fast reads on that path aren't slowed by
# hauling ~745 tickers worth of extended score dicts around every render.
_ALL_SCORES_KV_KEY = "all_scored_snapshot"

def _persisted_top_picks_snapshot() -> dict | None:
    """Cheap peek at the Postgres KV snapshot written by scripts/warm_top_picks.py.

    Returns the snapshot dict (with "source"="persisted" and "generated_at"
    fields attached) if a fresh one exists, else None. Does NOT fall back to
    a live scan — that's get_top_picks()'s job. Use this when the caller
    wants to know cheaply whether a warm snapshot is available BEFORE
    deciding whether to block on a live scan or spawn a background thread
    (e.g. the Command Centre fragment's decision to show its "~2 minute
    cold scan" banner should be gated on this returning None, not just on
    session_state being empty).

    Extracted from get_top_picks() so both paths use identical parsing and
    freshness logic — a change to _TOP_PICKS_MAX_AGE_SECONDS or the KV shape
    now only needs to happen here.
    """
    try:
        snap = _store.kv_get(_TOP_PICKS_KV_KEY, user_id=_TOP_PICKS_KV_USER)
    except Exception as _e:
        _log.warning("cache._persisted_top_picks_snapshot: kv_get failed: %s", _e)
        return None
    if not (snap and isinstance(snap, dict)):
        return None
    _gen_at = snap.get("generated_at")
    if not _gen_at:
        return None
    _age = _store.snapshot_age_seconds(_gen_at)
    if _age is None:
        _log.debug("cache._persisted_top_picks_snapshot: bad generated_at %r", _gen_at)
        return None
    if _age > _TOP_PICKS_MAX_AGE_SECONDS and not _ACCEPT_STALE:
        return None
    data = snap.get("data")
    if not (isinstance(data, dict) and "buys" in data):
        return None
    out = dict(data)
    out.setdefault("sells", [])
    out["source"] = "persisted"
    out["generated_at"] = _gen_at
    if _age > _TOP_PICKS_MAX_AGE_SECONDS:
        out["stale"] = True  # UI can surface this via a chip; behaviour is opt-in via NSE_ACCEPT_STALE_SNAPSHOTS
    return out


def _persisted_all_scores_snapshot() -> dict | None:
    """Cheap peek at the full-universe score map written by the warmer.

    Returns {ticker: score_dict} for every ticker the last scheduled scan
    produced a valid score for, else None if the snapshot is missing or
    older than _TOP_PICKS_MAX_AGE_SECONDS. Same freshness contract as
    _persisted_top_picks_snapshot — they're written together by
    scripts/warm_top_picks.py in the same run, so if one is fresh the
    other will be too.

    Consumers should treat a returned entry as authoritative for that
    ticker on the ~15-min timescale of the scheduled scan and fall back
    to live scoring ONLY for tickers not present in the map.
    """
    try:
        snap = _store.kv_get(_ALL_SCORES_KV_KEY, user_id=_TOP_PICKS_KV_USER)
    except Exception as _e:
        _log.warning("cache._persisted_all_scores_snapshot: kv_get failed: %s", _e)
        return None
    if not (snap and isinstance(snap, dict)):
        return None
    _gen_at = snap.get("generated_at")
    if not _gen_at:
        return None
    _age = _store.snapshot_age_seconds(_gen_at)
    if _age is None:
        _log.debug("cache._persisted_all_scores_snapshot: bad generated_at %r", _gen_at)
        return None
    if _age > _TOP_PICKS_MAX_AGE_SECONDS and not _ACCEPT_STALE:
        return None
    data = snap.get("data")
    if not isinstance(data, dict):
        return None
    return data


def get_top_picks(vix_regime: str = "normal", n: int = 20, sector_ranks: tuple = (),
                  force_live: bool = False) -> dict:
    """
    Fast-path wrapper around _home_top_picks().

    FIX SPEED1: _home_top_picks() itself is unchanged (still the correct thing
    to call for an in-process live scan). The problem this fixes is upstream —
    the ~2-min cold scan was being paid by whichever real user's browser
    session happened to hit an expired/empty st.cache_data entry (first visit
    after a deploy, or after any 5-min gap with no traffic). st.cache_data is
    process-wide, not shared across Streamlit Cloud restarts, so that cost
    recurred constantly for a low-traffic single-user app.

    scripts/warm_top_picks.py now runs on a schedule (GitHub Actions, every
    15 min in market hours — see .github/workflows/warm-top-picks.yml) and
    writes a fresh scan result to trade_store (shared Postgres — reachable
    from both the Action and the deployed app, unlike st.cache_data or
    SQLite). This function reads that snapshot first (via
    _persisted_top_picks_snapshot); only if it's missing or older than
    _TOP_PICKS_MAX_AGE_SECONDS does it fall back to a live scan, so a
    missed/late cron run still degrades gracefully instead of breaking.

    force_live=True is Command Centre's "Scan Now": skip the snapshot and the
    5-min in-process cache and score the universe afresh. Without it the
    button just handed back the snapshot already on screen.
    """
    if force_live:
        _home_top_picks.clear()
        result = dict(_home_top_picks(vix_regime=vix_regime, n=n, sector_ranks=sector_ranks))
        result["source"] = "live_scan"
        return result

    snap = _persisted_top_picks_snapshot()
    if snap is not None:
        return snap

    # FIX SPEED-DEV1 (2026-09-15): NSE_SKIP_LIVE_TOP_PICKS=1 short-circuits
    # the ~2-min live full-universe scan and returns an empty placeholder.
    # For local dev on a machine where the GH-Actions warmer has never run
    # against your Postgres KV, this is what unblocks first-paint. The
    # Command Centre UI can surface a "Top Picks not primed — run scripts/
    # warm_top_picks.py" chip on the empty payload.
    if os.environ.get("NSE_SKIP_LIVE_TOP_PICKS", "").lower() in ("1", "true", "yes"):
        return {"buys": [], "source": "skipped_dev", "generated_at": None}

    result = _home_top_picks(vix_regime=vix_regime, n=n, sector_ranks=sector_ranks)
    result = dict(result)
    result["source"] = "live_scan"
    return result


@st.cache_data(ttl=3600, show_spinner=False)   # 1hr cache — EOD signal, not intraday
def _tomorrow_watchlist(n: int = 15) -> dict:
    """
    Scan the Nifty 500 universe for NEXT-SESSION setups (based on today's close),
    distinct from intraday Top Picks. Reuses the composite-score infrastructure
    (score_stock folds in trend, momentum, RSI, volume, VIX + sector strength).

    Score component ranges (from analysis/score.py):
        technical_score  /40  — RSI, MACD, SMA stack, ADX
        momentum_score   /25  — 5d / 20d / 60d returns
        volume_score     /15  — Volume ratio + OBV trend

    Bucket precedence (FIX C1): a stock is tested against buckets in this
    fixed order — breakout, breakdown, bullish reversal, bearish reversal —
    and assigned to the FIRST bucket it matches. The breakout (sc>=52) and
    bearish-reversal (45<=sc<=68) ranges overlap; breakout wins ties because
    it's checked first. This is deliberate: a stock with both decent momentum
    AND a positive composite action is more useful flagged as a breakout
    candidate than buried in reversal-watch. If you change these thresholds,
    keep this comment in sync with the actual elif order below.

    Returns: {
        "breakout_candidates": [...],
        "breakdown_watch":     [...],
        "reversal_watch":      [...],
        "scan_time": "DD Mon HH:MM"
    }
    """
    import concurrent.futures as _cf
    import datetime as _dtm
    import sys, os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from data.universe import get_universe

    scan_time = _dtm.datetime.now().strftime("%d %b %H:%M")
    out = {"breakout_candidates": [], "breakdown_watch": [], "reversal_watch": [],
           "scan_time": scan_time}
    try:
        _UNIV = get_universe("nifty500")
    except Exception as _e:
        _log.debug("cache._tomorrow_watchlist universe failed: %s", _e)
        return out

    def _one(tk):
        try:
            from analysis.score import score_stock
            s = score_stock(tk)
            # FIX 3: pattern_score no longer exists on CompositeScore (removed in
            # PATTERN_REMOVAL_MIGRATION). Access only valid fields.
            # FIX C6: this caller needs technical/momentum/volume but not the
            # full extended set (narrative/sector/sentiment) — build directly
            # rather than forcing _score_to_dict's extended shape on it.
            return {"ticker": tk, "price": s.price, "score": s.score, "grade": s.grade,
                    "action": s.action, "headline": s.headline, "entry": s.entry,
                    "sl": s.stop_loss, "tp": s.target, "rr": s.risk_reward,
                    "technical": s.technical_score, "momentum": s.momentum_score,
                    "volume": s.volume_score,
                    # FIX HZ1-WL: score_stock() already computes a horizon label
                    # ("Swing (3-10 trading days)" / "Positional (2-6 weeks)")
                    # and a valid_until date for every stock it scores, but this
                    # function was dropping both before they reached the output
                    # dict — exactly the gap flagged as "unclear holding period"
                    # on this page. They were already being computed; just never
                    # threaded through.
                    "horizon": getattr(s, "horizon", ""),
                    "valid_until": getattr(s, "valid_until", "")}
        except Exception as _e:
            _log.debug("cache._tomorrow_watchlist score failed for %s: %s", tk, _e)
            return None

    results = []
    try:
        with _cf.ThreadPoolExecutor(max_workers=10) as ex:
            results = [r for r in ex.map(_one, _UNIV) if r]
    except Exception as _e:
        _log.debug("cache._tomorrow_watchlist scan degraded: %s", _e)
        results = [r for r in (_one(tk) for tk in _UNIV) if r]

    def _item(s, signal_type, key_level):
        return {"ticker": s["ticker"], "score": s["score"], "headline": s["headline"],
                "signal_type": signal_type, "key_level": key_level,
                "action": s["action"], "entry": s["entry"], "sl": s["sl"], "tp": s["tp"],
                "rr": s.get("rr"),
                "horizon": s.get("horizon", ""), "valid_until": s.get("valid_until", "")}

    # FIX C1: track how many stocks would have matched more than one bucket,
    # so threshold overlap drift is visible in logs.
    _multi_match_count = 0

    # Breakout funnel — how many names survive each breakout gate in order,
    # so an empty Breakout tab says WHICH gate emptied it (market tone vs. a
    # threshold that no longer fits the delivery-aware volume split).
    _funnel = {"eligible": 0, "buy_action": 0, "score_ge_52": 0,
               "momentum_ge_8": 0, "volume_ge_5": 0}

    for s in results:
        act, sc = s.get("action", ""), s.get("score", 0)
        if sc <= 0 or act in ("UNAVAILABLE", "DATA_UNAVAILABLE"):
            continue

        # Component scores — calibrated to actual ranges:
        #   technical /40 · momentum /25 · volume /15
        tech = s.get("technical", 0)   # out of 40
        mom  = s.get("momentum",  0)   # out of 25
        vol  = s.get("volume",    0)   # out of 15

        _funnel["eligible"] += 1
        if act in ("STRONG BUY", "BUY"):
            _funnel["buy_action"] += 1
            if sc >= 52:
                _funnel["score_ge_52"] += 1
                if mom >= 8:
                    _funnel["momentum_ge_8"] += 1
                    if vol >= 5:
                        _funnel["volume_ge_5"] += 1

        ent = s.get("entry", 0)
        kl  = f"₹{ent:,.0f}" if ent else "—"

        # FIX 4: Old thresholds (mom>=15, vol>=9) required top-40% on BOTH
        # components simultaneously - almost impossible to satisfy together.
        # New thresholds are ~33rd percentile of each component range.
        #
        # Task 4.2 M1 (audit docs/TOMORROW_WATCHLIST_AUDIT_2026-09.md): dropped
        # "WATCHLIST" from the breakout gate. Rationale: a WATCHLIST action
        # carries a "Mixed signals - worth watching for entry" headline, which
        # the "Breakout setup" bucket label directly contradicts. A user
        # reading a "Breakout" chip under a "Mixed Signals" narrative is the
        # exact "marked BUY but you'd never actually buy" pattern the audit
        # confirmed. Only genuinely BUY/STRONG BUY actions belong in the
        # Breakout Watch tab; WATCHLIST-only names still qualify for the
        # bullish-reversal bucket where the "watch" framing is honest.
        _is_breakout  = act in ("STRONG BUY", "BUY") and sc >= 52 and mom >= 8 and vol >= 5
        _is_breakdown = (act in ("EXIT", "CAUTION") or sc < 40) and tech < 22 and vol >= 4
        _is_bull_rev  = 35 <= sc <= 58 and mom >= 8 and tech < 25
        _is_bear_rev  = 45 <= sc <= 68 and mom < 5 and tech >= 22

        # FIX C1: count overlapping matches for visibility (doesn't change behavior)
        _match_count = sum([_is_breakout, _is_breakdown, _is_bull_rev, _is_bear_rev])
        if _match_count > 1:
            _multi_match_count += 1

        # Precedence: breakout → breakdown → bullish reversal → bearish reversal
        if _is_breakout:
            out["breakout_candidates"].append(_item(s, "🚀 Breakout setup", kl))
        elif _is_breakdown:
            out["breakdown_watch"].append(_item(s, "🔻 Breakdown risk",
                                                f"₹{s.get('sl', 0):,.0f}" if s.get("sl") else kl))
        elif _is_bull_rev:
            out["reversal_watch"].append(_item(s, "🔄 Bullish divergence", kl))
        elif _is_bear_rev:
            out["reversal_watch"].append(_item(s, "🔄 Bearish divergence", kl))

    out["breakout_candidates"].sort(key=lambda x: -x["score"])
    out["breakdown_watch"].sort(key=lambda x: x["score"])
    out["reversal_watch"].sort(key=lambda x: -x["score"])
    out["breakout_candidates"] = out["breakout_candidates"][:n]
    out["breakdown_watch"]     = out["breakdown_watch"][:n]
    out["reversal_watch"]      = out["reversal_watch"][:n]
    # Gate-strip inputs on the page: how many names the scan covered and
    # how many it actually scored. Older persisted snapshots lack these keys.
    out["n_universe"] = len(_UNIV)
    out["n_scored"]   = len(results)
    out["breakout_funnel"] = dict(_funnel)
    # Bear-regime scoring (#168/#198) holds Technical at 20/40 and RS at 5/10,
    # so no name can reach the BUY label the breakout gate needs and few fall
    # low enough for breakdown. Record it so the page can say the empty
    # buckets are regime-suppressed rather than looking like a broken scan.
    try:
        from analysis.score import _BEAR_REGIMES, _live_regime_label, _regime_weights_enabled
        _rl = _live_regime_label()
        out["regime_label"] = _rl
        out["bear_scoring_active"] = bool(_rl in _BEAR_REGIMES and _regime_weights_enabled())
    except Exception as _rg_e:
        _log.debug("cache._tomorrow_watchlist: regime flag unavailable: %s", _rg_e)

    # FIX C7: log per-bucket counts and warn on empty buckets, so a silently-
    # empty bucket (the original threshold bug) is visible in logs going
    # forward rather than only discoverable by a user noticing an empty tab.
    _counts = {
        "breakout_candidates": len(out["breakout_candidates"]),
        "breakdown_watch":     len(out["breakdown_watch"]),
        "reversal_watch":      len(out["reversal_watch"]),
    }
    _log.info(
        "cache._tomorrow_watchlist scan complete: %d stocks scored, "
        "buckets=%s, multi-bucket-matches=%d",
        len(results), _counts, _multi_match_count,
    )
    for _bucket, _count in _counts.items():
        if _count == 0:
            _log.warning(
                "cache._tomorrow_watchlist: bucket '%s' is EMPTY this scan "
                "(%d stocks scored total). If this persists across multiple "
                "scans, thresholds may need recalibration.",
                _bucket, len(results),
            )
    if _counts["breakout_candidates"] == 0:
        _log.warning(
            "cache._tomorrow_watchlist breakout funnel: eligible=%d → BUY/STRONG BUY=%d "
            "→ score>=52=%d → momentum>=8=%d → volume>=5=%d",
            *_funnel.values(),
        )

    # Task 4.2 F1 (audit docs/TOMORROW_WATCHLIST_AUDIT_2026-09.md): log every
    # shortlisted pick to verdict_ledger with source="tomorrow_watchlist" so
    # the Verdict Calibration page can answer "how did yesterday's TW picks
    # actually perform" - previously ~45 daily picks were surfaced then
    # thrown away because the bespoke card renderer bypassed the
    # pick_freshness helper that normally handles this.
    #
    # Dedup is handled by verdict_log's (logged_date, ticker, horizon, source)
    # PRIMARY KEY, so re-invocations inside the 1h cache window or via the
    # warmer script are safe no-ops. Every failure path in log_verdict()
    # itself is swallowed and logged at debug, per its docstring - the
    # ledger is a background concern that must never break the scan.
    try:
        from analysis.verdict_ledger import log_verdict as _vl_log
        from analysis.final_verdict import combine as _fv_combine
        _shortlist = (
            out["breakout_candidates"]
            + out["breakdown_watch"]
            + out["reversal_watch"]
        )
        for _pick in _shortlist:
            _tk = _pick.get("ticker")
            if not _tk:
                continue
            try:
                # Rebuild a FinalVerdict for this pick from its composite
                # score + action, then persist. Horizon defaults to "medium"
                # via combine's own default - matches how pick_freshness
                # does it for Top Picks so calibration slices align across
                # sources.
                _fv = _fv_combine(
                    composite_score=_pick.get("score"),
                    composite_action=_pick.get("action"),
                )
                _vl_log(
                    ticker=_tk,
                    final_verdict=_fv,
                    entry_price=(float(_pick["entry"]) if _pick.get("entry") else None),
                    composite_score=(float(_pick["score"]) if _pick.get("score") is not None else None),
                    source="tomorrow_watchlist",
                )
            except Exception as _one_e:
                _log.debug(
                    "cache._tomorrow_watchlist ledger write failed for %s: %s",
                    _tk, _one_e,
                )
    except Exception as _vl_e:
        # verdict_ledger unavailable - ledger writes are best-effort only
        _log.debug("cache._tomorrow_watchlist ledger disabled: %s", _vl_e)

    return out


_TW_KV_KEY  = "tomorrow_watchlist_snapshot"
_TW_KV_USER = "_system"           # not per-user — one shared scan result
_TW_MAX_AGE_SECONDS = 6 * 3600    # 6h — this is an EOD/next-session scan, not intraday;
                                  # a once-daily cron has a lot more slack than Top Picks'
                                  # 15-min cadence, so a generous window here is correct,
                                  # not sloppy — see warm-tomorrow-watchlist.yml schedule.


def get_tomorrow_watchlist(n: int = 15) -> dict:
    """
    Fast-path wrapper around _tomorrow_watchlist(), mirroring get_top_picks()'s
    snapshot pattern exactly (see that function's docstring for the full
    rationale — same fix, same reason: st.cache_data is process-local and
    doesn't survive a Streamlit Cloud redeploy/restart, so whichever real
    user's browser session hits an expired/empty cache pays the full ~2 min
    cold scan).

    scripts/warm_tomorrow_watchlist.py runs on a schedule (GitHub Actions —
    see .github/workflows/warm-tomorrow-watchlist.yml, once after market
    close) and writes a fresh scan result to trade_store. This function reads
    that snapshot first; only if it's missing or older than
    _TW_MAX_AGE_SECONDS does it fall back to a live scan (which
    17_tomorrow_watchlist.py's own FIX W1 stale-while-revalidate handles
    non-blockingly either way).
    """
    try:
        snap = _store.kv_get(_TW_KV_KEY, user_id=_TW_KV_USER)
        if snap and isinstance(snap, dict):
            _gen_at = snap.get("generated_at")
            _age = _store.snapshot_age_seconds(_gen_at)
            if _gen_at and _age is None:
                _log.debug("cache.get_tomorrow_watchlist: bad generated_at %r", _gen_at)
            if _age is not None and _age <= _TW_MAX_AGE_SECONDS:
                data = snap.get("data")
                if isinstance(data, dict) and "scan_time" in data:
                    data = dict(data)
                    data["source"] = "persisted"
                    data["generated_at"] = _gen_at
                    return data
    except Exception as _e:
        _log.warning("cache.get_tomorrow_watchlist: persisted snapshot read failed, "
                     "falling back to live scan: %s", _e)

    result = _tomorrow_watchlist(n=n)
    result = dict(result)
    result["source"] = "live_scan"
    return result


@st.cache_data(ttl=600, show_spinner=False)
def get_tw_ledger(limit: int = 400) -> pd.DataFrame:
    """Recent Tomorrow's Watchlist rows from verdict_ledger, forward returns
    joined. Never raises: load_ledger already returns an empty frame on error."""
    from analysis.verdict_ledger import load_ledger
    return load_ledger(source="tomorrow_watchlist", limit=limit)


@st.cache_data(ttl=1800, show_spinner=False)
def get_regime_snapshot() -> "dict | None":
    """Live market-regime snapshot as a dict, or None when feeds are down."""
    try:
        from analysis.regime import snapshot_live
        return snapshot_live().as_dict()
    except Exception as _e:
        _log.debug("cache.get_regime_snapshot failed: %s", _e)
        return None


@st.cache_data(ttl=1800, show_spinner=False)
def get_fii_dii_recent(days: int = 10) -> "dict | None":
    """Latest FII/DII cash-market day plus 5-session FII sum, from the
    fii_dii_daily table the cron fills. None when the table is empty."""
    try:
        from analysis.fii_dii import load_history
        df = load_history(days=days)
        if df is None or df.empty or "fii_net" not in df.columns:
            return None
        df = df.sort_values("date")
        last = df.iloc[-1]
        return {
            "date":   str(last.get("date", "")),
            "fii":    float(last["fii_net"]) if pd.notna(last["fii_net"]) else None,
            "dii":    float(last["dii_net"]) if pd.notna(last.get("dii_net")) else None,
            "fii_5d": float(df["fii_net"].tail(5).fillna(0).sum()),
            "n_5d":   int(df["fii_net"].tail(5).notna().sum()),
        }
    except Exception as _e:
        _log.debug("cache.get_fii_dii_recent failed: %s", _e)
        return None


@st.cache_data(ttl=900, show_spinner=False)
def get_recent_posture_changes(limit: int = 6, lookback_rows: int = 3000) -> list:
    """Most recent verdict changes in verdict_ledger, one per ticker.

    A change is two consecutive logged days for the same (ticker, source,
    horizon) whose `verdict` differs, so like is compared with like. Returns
    dicts: ticker, source, date, prev, new, prev_score, new_score. [] on error.
    """
    try:
        from analysis.verdict_ledger import load_ledger
        df = load_ledger(limit=lookback_rows)
        need = {"ticker", "verdict", "logged_at", "logged_date", "source"}
        if df is None or df.empty or not need.issubset(df.columns):
            return []
        if "horizon" not in df.columns:
            df = df.assign(horizon="")
        df = df.sort_values("logged_at")
        changes = []
        for (tk, src, _hz), g in df.groupby(["ticker", "source", "horizon"], dropna=False):
            g = g.drop_duplicates("logged_date", keep="last")
            rows = g.to_dict("records")
            for prev, cur in zip(rows, rows[1:]):
                if prev.get("verdict") and cur.get("verdict") and prev["verdict"] != cur["verdict"]:
                    changes.append({
                        "ticker": tk, "source": src, "date": str(cur["logged_date"]),
                        "at": str(cur["logged_at"]),
                        "prev": prev["verdict"], "new": cur["verdict"],
                        "prev_score": prev.get("composite_score"),
                        "new_score": cur.get("composite_score"),
                    })
        changes.sort(key=lambda c: c["at"], reverse=True)
        seen, out = set(), []
        for c in changes:
            if c["ticker"] in seen:
                continue
            seen.add(c["ticker"])
            out.append(c)
            if len(out) >= limit:
                break
        return out
    except Exception as _e:
        _log.debug("cache.get_recent_posture_changes failed: %s", _e)
        return []


@st.cache_data(ttl=3600, show_spinner=False)   # 1-hour cache — heavy multi-fetch
def _sector_ranking():
    """
    Rank all NSE sectors by constituent momentum (cached 1 h). Returns a
    DataFrame indexed by sector with a 'Rank' column for score_stock(), or None.
    """
    try:
        from analysis.sector_strength import rank_sectors
        return rank_sectors()
    except Exception as _e:
        _log.debug("cache.%s degraded: %s", "_sector_ranking", _e)
        return None


def _sector_ranks_tuple() -> tuple:
    """
    Hashable ((sector, rank), …) form of _sector_ranking() for cached scorers.

    FIX C11: distinguishes "no sector data available" (empty/None result,
    expected and logged at DEBUG) from "sector ranking fetch raised an
    exception" (unexpected, logged at WARNING). The return value is still ()
    in both cases — callers can't be changed without touching every scorer
    that consumes this tuple — but the failure mode is now distinguishable
    in logs, so a transient network blip silently stripping sector-strength
    signal from every score is no longer indistinguishable from "there's
    legitimately no sector data this run."
    """
    try:
        df = _sector_ranking()
    except Exception as _e:
        # FIX C11: unexpected exception — warn, this is a real failure
        _log.warning("cache._sector_ranks_tuple: _sector_ranking() raised: %s", _e)
        return ()

    if df is None or df.empty:
        # FIX C11: expected "no data" case — debug level, not a failure
        _log.debug("cache._sector_ranks_tuple: no sector ranking data available this run")
        return ()

    try:
        return tuple((str(idx), int(row["Rank"])) for idx, row in df.iterrows())
    except Exception as _e:
        _log.warning("cache._sector_ranks_tuple: failed to convert ranking to tuple: %s", _e)
        return ()


@st.cache_data(ttl=600)
def get_composite_score(ticker: str):
    """
    Deep-dive score over a 2-YEAR lookback (was 1y). The longer window means
    every signal is computed on a full, valid history — SMA_200 (296 valid rows
    vs ~49 on 1y), RSI divergence, candlestick patterns, ADX, volume trend and
    momentum all have enough warmup, so the composite reflects real multi-signal
    analysis, not just the latest bar. Sector strength + VIX are folded in too.

    FIX C3: previously this was the only scoring function in this file with
    no error handling. A bad ticker or network failure would raise uncaught,
    leaving nothing in the cache for a retry and relying entirely on the
    caller's try/except. Now wraps score_stock() and returns a sentinel
    object (duck-typed to look like a CompositeScore with action=UNAVAILABLE)
    on failure, consistent with every sibling function in this file.
    """
    from analysis.score import score_stock

    class _UnavailableScore:
        """Minimal duck-typed stand-in so callers expecting CompositeScore
        attributes (cs.price, cs.action, etc.) don't crash on AttributeError."""
        def __init__(self, ticker, reason=""):
            self.ticker          = ticker
            self.price            = 0.0
            self.score            = 0.0
            self.grade            = "?"
            self.action           = "UNAVAILABLE"
            self.headline         = f"Data unavailable{f' ({reason})' if reason else ''}"
            self.narrative        = "Scoring failed for this ticker — try again shortly."
            self.entry            = 0.0
            self.stop_loss        = 0.0
            self.target            = 0.0
            self.risk_reward      = 0.0
            self.sector           = "—"
            self.sector_rank      = None
            self.vix_regime       = "normal"
            self.technical_score  = 0.0
            self.momentum_score   = 0.0
            self.volume_score     = 0.0
            self.sentiment_score  = 0.0
            self.company_name     = None
            self.horizon          = ""    # FIX HZ1
            self.valid_until      = ""    # FIX HZ1
            self.rsi              = 50.0  # FIX WL1
            self.return_1d        = 0.0   # FIX WL1

    try:
        vix_info = get_vix_info()
        sectors  = _sector_ranking()
        return score_stock(ticker, period="2y", vix_info=vix_info,
                           sector_scores_df=sectors)
    except Exception as _e:
        # FIX C3: previously uncaught — now returns a usable sentinel
        _log.warning("cache.get_composite_score(%s) failed: %s", ticker, _e)
        return _UnavailableScore(ticker, f"{type(_e).__name__}: {str(_e)[:70]}")


@st.cache_data(ttl=600, show_spinner=False)
def _deep_confirmation(ticker: str) -> dict:
    """
    Confirmation layer on top of the composite score:
      • Multi-timeframe — weekly trend (filters daily false signals)
      • Relative strength — 1-month return vs Nifty (is it a leader?)
      • Earnings proximity — days to next result (avoid buying into a gap)
      • Signal agreement — how many of 9 checks are bullish (conviction)

    FIX C4: earnings_days is now sanity-bounded. If the upstream
    get_earnings_date() returns a date more than 100 days in the past (most
    likely a stale/unrefreshed last-quarter date from the data source rather
    than a genuine upcoming result), it's treated as unknown (None) rather
    than surfaced as a confidently wrong large negative number.
    """
    out = {"weekly": None, "rel_strength": None, "rs_pct": None,
           "earnings_days": None, "bull": 0, "total": 0, "signals": []}
    try:
        from data.fetcher import fetch_single
        from utils.indicators import add_all_indicators
        df  = add_all_indicators(fetch_single(ticker, period="2y")).dropna(axis=1, how="all")
        cur = df.iloc[-1]
        price = float(cur["Close"])

        # Weekly trend
        wk = df["Close"].resample("W").last().dropna()
        if len(wk) >= 11:
            _wma10 = float(wk.rolling(10).mean().iloc[-1])
            _wkchg = (wk.iloc[-1] / wk.iloc[-5] - 1) * 100 if len(wk) >= 5 else 0
            out["weekly"] = ("uptrend" if wk.iloc[-1] > _wma10 and _wkchg > 0
                             else "downtrend" if wk.iloc[-1] < _wma10 and _wkchg < 0
                             else "sideways")

        # Relative strength vs Nifty (1 month ≈ 22 sessions)
        try:
            nf = fetch_single("^NSEI", period="6mo")["Close"].dropna()
            if len(nf) >= 22 and len(df) >= 22:
                _s1 = (price / float(df["Close"].iloc[-22]) - 1) * 100
                _n1 = (float(nf.iloc[-1]) / float(nf.iloc[-22]) - 1) * 100
                out["rs_pct"] = round(_s1 - _n1, 1)
                out["rel_strength"] = "outperforming" if out["rs_pct"] > 0 else "underperforming"
        except Exception as _e:
            _log.debug("cache.%s degraded: %s", "_deep_confirmation", _e)
            pass

        # Earnings proximity
        try:
            from data.events import get_earnings_date
            import datetime as _ed_dt
            ed = get_earnings_date(ticker)
            if ed:
                _raw_days = (ed - _ed_dt.datetime.now()).days
                # FIX C4: sanity-bound — more than 100 days in the past is
                # almost certainly a stale upstream date, not a real result
                # that happened over 3 months ago and was never refreshed.
                if _raw_days < -100:
                    _log.debug(
                        "cache._deep_confirmation(%s): earnings_days=%d looks "
                        "stale (>100d in the past) — treating as unknown.",
                        ticker, _raw_days,
                    )
                    out["earnings_days"] = None
                else:
                    out["earnings_days"] = _raw_days
        except Exception as _e:
            _log.debug("cache.%s degraded: %s", "_deep_confirmation", _e)
            pass

        # Signal agreement (9 checks)
        rsi = float(cur.get("RSI", 50))
        sigs = [
            ("RSI not overbought (<70)",    rsi < 70),
            ("MACD above signal",           float(cur.get("MACD", 0)) > float(cur.get("MACD_Signal", 0))),
            ("Above 20-day avg",            price > float(cur.get("SMA_20",  price * 1.1))),
            ("Above 50-day avg",            price > float(cur.get("SMA_50",  price * 1.1))),
            ("Above 200-day avg",           price > float(cur.get("SMA_200", price * 1.1))),
            ("Trend has strength (ADX>20)", float(cur.get("ADX", 0)) > 20),
            ("Volume supportive",           float(cur.get("Volume_Ratio", 1)) >= 1.0),
            ("No bearish divergence",       not bool(cur.get("RSI_Bear_Div", 0))),
            ("Weekly trend not down",       out["weekly"] != "downtrend"),
        ]
        out["signals"] = sigs
        out["bull"]    = sum(1 for _, ok in sigs if ok)
        out["total"]   = len(sigs)
    except Exception as _e:
        _log.debug("cache.%s degraded: %s", "_deep_confirmation", _e)
        pass
    return out


# Task 2.4 F5: TTL reduced from 1800 -> 300 (5 min). Sparkline shows 22
# daily closes; the 30-min TTL froze the last-day tick during market hours
# even though it changes with every scan cycle. 300s matches the Top Picks /
# watchlist cadence so the sparkline's trailing bar moves in lock-step with
# the composite score it sits next to. Data cost is negligible (one 3mo
# fetch per ticker per 5 min).
@st.cache_data(ttl=300, show_spinner=False)
def _sparkline_closes(ticker: str, n: int = 22) -> list:
    """Last `n` daily closes for a mini sparkline (cached 5 min per Task 2.4 F5)."""
    try:
        from data.fetcher import fetch_single
        c = fetch_single(ticker, period="3mo")["Close"].dropna().tolist()
        return [round(float(x), 2) for x in c[-n:]]
    except Exception as _e:
        _log.debug("cache.%s degraded: %s", "_sparkline_closes", _e)
        return []


def _sparkline_svg(prices: list, w: int = 120, h: int = 28) -> str:
    """Inline SVG sparkline from a price list — green if up over the window, else red."""
    if not prices or len(prices) < 2:
        return ""
    lo, hi = min(prices), max(prices)
    rng = (hi - lo) or 1
    pts = " ".join(
        f"{i/(len(prices)-1)*w:.1f},{h - (p-lo)/rng*(h-4) - 2:.1f}"
        for i, p in enumerate(prices)
    )
    col = "#00d4aa" if prices[-1] >= prices[0] else "#ff4757"
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" style="display:block">'
            f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="1.6" '
            f'stroke-linejoin="round" stroke-linecap="round"/></svg>')


# ─────────────────────────────────────────────────────────────────────────────
# FIX C2 — cache warm-up hook for scheduled jobs
# ─────────────────────────────────────────────────────────────────────────────

def warm_caches() -> dict:
    """
    Pre-populate the two expensive full-universe scans (_home_top_picks and
    _tomorrow_watchlist) so the first real user of the day doesn't pay the
    ~2 min cold-scan cost.

    This function is intended to be invoked by a scheduled job (cron,
    APScheduler, a Streamlit-external worker, etc.) shortly before market
    open (e.g. 9:00 AM IST) and once after market close (for the next-session
    watchlist). Wiring an actual scheduler is a deployment-level concern
    outside this module — this is just the callable entry point.

    Returns a dict summarising what was warmed and how long each took, for
    logging/monitoring by whatever calls this.
    """
    import time as _time
    results = {}

    _t0 = _time.time()
    try:
        _vix = get_vix_info()
        _sectors = _sector_ranks_tuple()
        _home_top_picks(vix_regime=_vix.get("regime", "normal"), sector_ranks=_sectors)
        results["home_top_picks"] = {"ok": True, "seconds": round(_time.time() - _t0, 1)}
    except Exception as _e:
        _log.warning("cache.warm_caches: _home_top_picks warm-up failed: %s", _e)
        results["home_top_picks"] = {"ok": False, "error": str(_e)}

    _t1 = _time.time()
    try:
        _tomorrow_watchlist()
        results["tomorrow_watchlist"] = {"ok": True, "seconds": round(_time.time() - _t1, 1)}
    except Exception as _e:
        _log.warning("cache.warm_caches: _tomorrow_watchlist warm-up failed: %s", _e)
        results["tomorrow_watchlist"] = {"ok": False, "error": str(_e)}

    _log.info("cache.warm_caches complete: %s", results)
    return results

"""Investor Guide - NSE Smart Investor (multipage page; body verbatim from app.py).

LAYOUT (2026-09-29): artboard 09 of the 2026-09-12 "proposed layout" mockup,
the second "quiet" page: a document, not a dashboard. Each tab is a chapter
in a centred reading column: eyebrow, serif title, reading time, a table of
contents linking to numbered section heads, a drop-cap lede, prose, quiet
document tables, callouts in place of coloured alert boxes, and a pointer to
the next chapter.
"""
import os, sys
import html
import re
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import streamlit as st
from dashboard.shared.design import apply_design
from dashboard.shared.nav import render_sidebar
from dashboard.shared.chart_helpers import render_top_bar
from dashboard.shared.trade_utils import _display_label, _action_emoji  # Phase 2 UI honesty
from dashboard.shared.ui_components import signal_table

apply_design()
render_sidebar(current="Investor Guide")
render_top_bar()

_CHAPTERS = [
    "Scores & signals", "Indicators", "Stop-loss & risk", "News signals",
    "Paper trading", "Universe & coverage", "TQS scanner",
]


# ── Document helpers (guide-only) ─────────────────────────────────────────
def _inline(text: str) -> str:
    """Escape, then turn **bold** and `code` into HTML for the builders below."""
    out = html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"\*(.+?)\*", r"<em>\1</em>", out)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", out)


def _open(n: int, title: str, minutes: int, sections: list) -> None:
    """Eyebrow, serif title, meta line and table of contents for chapter n."""
    toc = "".join(f'<a href="#g{n}-{i}"><span>{s}</span><span class="n">{i:02d}</span></a>'
                  for i, s in enumerate(sections, 1))
    st.markdown(
        f'<div class="gd-eyebrow">Investor guide · <span>chapter {n} of {len(_CHAPTERS)}</span></div>'
        f'<div class="gd-title" role="heading" aria-level="2">{title}</div>'
        f'<div class="gd-meta"><span>{minutes} min read</span><span>·</span>'
        f"<span>descriptive, never buy / sell / hold</span></div>"
        f'<nav class="gd-toc" aria-label="In this chapter"><div class="gd-toc-t">In this chapter</div>'
        f"{toc}</nav>",
        unsafe_allow_html=True,
    )


def _lede(text: str) -> None:
    st.markdown(f'<p class="gd-lede">{_inline(text)}</p>', unsafe_allow_html=True)


def _h3(n: int, i: int, title: str) -> None:
    st.markdown(f'<div class="gd-h3" id="g{n}-{i}" role="heading" aria-level="3">'
                f'<span class="n">{i:02d}</span>{title}</div>', unsafe_allow_html=True)


def _quote(text: str) -> None:
    st.markdown(f'<blockquote class="gd-quote">{_inline(text)}</blockquote>', unsafe_allow_html=True)


def _callout(text: str, warn: bool = False) -> None:
    st.markdown(f'<div class="gd-callout{" warn" if warn else ""}">{_inline(text)}</div>',
                unsafe_allow_html=True)


def _table(rows: list) -> None:
    """Quiet document table from a list of same-keyed dicts."""
    cols = list(rows[0].keys())
    st.markdown(signal_table([(c, "l") for c in cols],
                             [[_inline(str(r[c])) for c in cols] for r in rows]),
                unsafe_allow_html=True)


def _next(n: int) -> None:
    if n < len(_CHAPTERS):
        st.markdown(f'<div class="gd-next">Next: chapter {n + 1}, {_CHAPTERS[n]} (tab above).</div>',
                    unsafe_allow_html=True)


# ───────────────────────── page body (de-indented from app.py) ─────────────────────────
st.markdown('<h1 class="page-title-serif guide-doc">Investor <em>Guide</em></h1>'
            '<p class="page-subtitle">How to read this dashboard: every score, signal and term, '
            'in seven short chapters.</p>', unsafe_allow_html=True)

tab_g1, tab_g2, tab_g3, tab_g4, tab_g5, tab_g6, tab_g7 = st.tabs(_CHAPTERS)

# ── CHAPTER 1: SCORES & SIGNALS ───────────────────────────────────────────
with tab_g1, st.container(key="guide_ch_1"):
    _open(1, "What the composite score <em>measures</em>, and what it doesn't.", 5, [
        "The composite score, 0 – 90", "The four sub-components",
        "Fundamentals and the revenue-growth signal", "VIX regime, the market fear gauge",
    ])
    _lede(
        "Every stock gets a Composite Score out of 90 describing trend quality. It combines "
        "four factors: Technical (40 points), Momentum (25), Volume (15) and Market Sentiment "
        "(10, from the VIX regime and sector strength)."
    )
    _h3(1, 1, "The composite score, 0 – 90")
    st.markdown(
        "Candlestick patterns are still detected and shown in the narrative, but a 5-year "
        "validation found they added no ranking power, so they no longer contribute points, "
        "which makes top grades slightly stricter."
    )
    _quote(
        "The score is a strong gauge of trend persistence. It is not a forecast of future returns."
    )
    _callout(
        "**What this score is, and isn't.** A 5-year validation study (86,589 observations across "
        "bull, bear and sideways markets, re-run July 2026) found high-score stocks reliably stay "
        "in uptrends (+0.40 rank correlation), but only ≈ +0.02 correlation with next-month "
        "returns, and its rankings become unreliable, and can invert, as VIX rises. Read the "
        "signals as *trend health*, then apply your own entry and risk rules."
    )
    _callout(
        "**Honest caveat.** The 5-year efficacy study (docs/SCORE_EFFICACY_2026-09-24.md) found "
        "only a weak tendency for higher-scored stocks to outperform lower-scored ones on the same "
        "date (rank correlation ≈ +0.02). The edge showed up in trending-up markets, vanished in "
        "range-bound ones and ran backwards in fear regimes, and it was negative in 2025. Scores "
        "describe trend quality today, not future returns.",
        warn=True,
    )
    st.markdown(
        "Grade and trend label come from two separate threshold scales in `analysis/score.py`, "
        "so they don't line up row for row."
    )
    st.markdown("**Grade scale (composite, 0–90)**")
    _table([
        {"Score range": "88 – 90", "Grade": "A+"},
        {"Score range": "75 – 87", "Grade": "A"},
        {"Score range": "62 – 74", "Grade": "B"},
        {"Score range": "48 – 61", "Grade": "C"},
        {"Score range": "32 – 47", "Grade": "D"},
        {"Score range": "0 – 31",  "Grade": "F"},
    ])
    st.markdown("**Trend label scale (composite, 0–90)**")
    _table([
        {"Score range": "80 – 90", "Label": f"{_display_label('STRONG BUY')} {_action_emoji('STRONG BUY')}", "What it means": "Very strong trend quality, everything aligned. Not a return forecast."},
        {"Score range": "65 – 79", "Label": f"{_display_label('BUY')} {_action_emoji('BUY')}",               "What it means": "Healthy uptrend with good momentum."},
        {"Score range": "52 – 64", "Label": f"{_display_label('WATCHLIST')} {_action_emoji('WATCHLIST')}",   "What it means": "Mixed signals, confirmation incomplete."},
        {"Score range": "40 – 51", "Label": f"{_display_label('HOLD')} {_action_emoji('HOLD')}",             "What it means": "Neutral trend, no edge either way on current readings."},
        {"Score range": "25 – 39", "Label": f"{_display_label('CAUTION')} {_action_emoji('CAUTION')}",       "What it means": "Weak or deteriorating trend, momentum fading."},
        {"Score range": "0 – 24",  "Label": f"{_display_label('EXIT')} {_action_emoji('EXIT')}",             "What it means": "Trend broken, though beaten-down names can rebound sharply in fear regimes."},
    ])

    _h3(1, 2, "The four sub-components")
    _table([
        {"Component": "Technical (40 pts)",  "What it measures": "RSI, MACD, Bollinger Bands, SMA trends: is the stock in a healthy uptrend?"},
        {"Component": "Momentum (25 pts)",   "What it measures": "Recent price performance over 5 / 20 / 60 days. Is the trend persisting?"},
        {"Component": "Volume (15 pts)",     "What it measures": "Is trading volume higher than normal? Big moves on high volume are more reliable."},
        {"Component": "Sentiment (10 pts)",  "What it measures": "Market backdrop: India-VIX regime (6 pts) + the stock's sector strength rank (4 pts)."},
        {"Component": "Candlestick (0 pts, info only)", "What it measures": "Patterns (Hammer, Engulfing, Doji…) are detected and mentioned in the narrative, but a 5-year study found they add no ranking power, so they are not scored."},
    ])

    _h3(1, 3, "Fundamentals and the revenue-growth signal")
    st.markdown(
        "The **Analyze Stock** page also shows fundamentals (revenue growth, EPS growth, ROE, "
        "debt/equity) from audited financial statements, read through a sector-aware lens: banks, "
        "NBFCs and insurers are assessed on P/B and ROE, not leverage. These are **separate from "
        "the Trend Quality Score**, deliberately, because research found blending them in reduced "
        "signal quality."
    )
    _callout(
        "**Revenue growth** has been the strongest return-predictive signal identified in platform "
        "research (2022–2025 validation: the highest rank-correlation with 6–12-month forward "
        "returns of any metric tested, with returns rising across growth quintiles in both bull and "
        "bear regimes). It is a measured, research-backed observation, not a recommendation, and "
        "historical relationships may not persist. High growth alone says nothing about valuation, "
        "risk or timing."
    )

    _h3(1, 4, "VIX regime, the market fear gauge")
    st.markdown(
        "**India VIX** measures how much volatility the market expects over the next 30 days. "
        "High VIX means fear; very low VIX means complacency. Both call for care, for different "
        "reasons."
    )
    _table([
        {"VIX level": "< 12",  "Regime": "Complacency", "Meaning": "Market very relaxed; historically many corrections began from low-VIX readings"},
        {"VIX level": "12–16", "Regime": "Normal 🟢",   "Meaning": "Healthy range; trend signals have historically been most reliable here"},
        {"VIX level": "16–22", "Regime": "Elevated 🟡", "Meaning": "Some fear; score rankings start to lose reliability"},
        {"VIX level": "22–28", "Regime": "Fear 🔴",     "Meaning": "Significant fear; larger daily swings, rankings can invert"},
        {"VIX level": "> 28",  "Regime": "Panic 🔴",    "Meaning": "Extreme volatility; historically extremes have sometimes preceded sharp rebounds"},
    ])
    _next(1)

# ── CHAPTER 2: INDICATORS ─────────────────────────────────────────────────
with tab_g2, st.container(key="guide_ch_2"):
    _open(2, "Technical indicators in <em>plain English</em>.", 4, [
        "The indicators behind the score", "Candlestick patterns",
    ])
    _lede(
        "Every number on the Analyze Stock page comes from a handful of standard indicators. "
        "None of them is a signal on its own; read them together, and against the trend."
    )
    _h3(2, 1, "The indicators behind the score")
    _table([
        {"Indicator": "RSI (14)",          "Range": "0 – 100",      "Normal": "30–70",      "Meaning": "Relative Strength Index. Below 30 = oversold (potential bounce). Above 70 = overbought (potential pullback). Not a standalone signal."},
        {"Indicator": "MACD",              "Range": "Positive/neg", "Normal": "Near zero",  "Meaning": "Moving Average Convergence Divergence. MACD crossing above its signal line = bullish. Below = bearish."},
        {"Indicator": "Bollinger Bands",   "Range": "Price levels", "Normal": "Within band","Meaning": "Upper/lower bands = 2 standard deviations from the 20-day average. Price near upper = overbought. Near lower = oversold."},
        {"Indicator": "SMA 20 / 50 / 200", "Range": "Price level",  "Normal": "Price > SMA","Meaning": "Simple Moving Average. Price above SMA200 = long-term uptrend. SMA20 > SMA50 > SMA200 = strong bull alignment."},
        {"Indicator": "ADX",               "Range": "0 – 100",      "Normal": "20–40",      "Meaning": "Average Directional Index. Above 25 = trending. Below 20 = ranging, where breakouts fail more often."},
        {"Indicator": "ATR",               "Range": "₹ value",      "Normal": "Varies",     "Meaning": "Average True Range: average daily movement in rupees. Used to set stop-losses (typically 1.5–2× ATR below entry)."},
        {"Indicator": "Volume ratio",      "Range": "> 0",          "Normal": "0.8–1.2",    "Meaning": "Today's volume ÷ 20-day average volume. Above 1.5 = above-average interest. Above 2.5 = institutional activity."},
        {"Indicator": "Stochastic K",      "Range": "0 – 100",      "Normal": "20–80",      "Meaning": "Momentum oscillator. Below 20 = oversold, above 80 = overbought. Best used with other signals."},
        {"Indicator": "VWAP %",            "Range": "% value",      "Normal": "±1%",        "Meaning": "Price vs Volume-Weighted Average Price. Positive = above where most volume traded today. Negative = below."},
    ])

    _h3(2, 2, "Candlestick patterns")
    _callout(
        "**Context only.** The textbook reliability stars below are the traditional view. The "
        "platform's own 40,000-observation study found patterns added no ranking power, so they "
        "appear in the narrative but **never count toward the score**."
    )
    _table([
        {"Pattern": "Hammer 🔨",           "Type": "Bullish reversal",     "Textbook reliability": "★★★★",  "What it means": "Long lower wick at a low. Sellers pushed lower but buyers stepped in."},
        {"Pattern": "Shooting Star ⭐",    "Type": "Bearish reversal",     "Textbook reliability": "★★★★",  "What it means": "Long upper wick at a high. Buyers pushed higher but sellers overwhelmed them."},
        {"Pattern": "Doji",                "Type": "Indecision",           "Textbook reliability": "★★★",   "What it means": "Open = close. Neither side in control; the next candle's direction matters."},
        {"Pattern": "Bullish Engulfing",   "Type": "Bullish reversal",     "Textbook reliability": "★★★★★", "What it means": "Large green candle engulfs the prior red one, after a downtrend."},
        {"Pattern": "Bearish Engulfing",   "Type": "Bearish reversal",     "Textbook reliability": "★★★★★", "What it means": "Large red candle engulfs the prior green one, after an uptrend."},
        {"Pattern": "Morning Star ☀️",    "Type": "Bullish reversal",     "Textbook reliability": "★★★★★", "What it means": "Big red → small candle → big green. Classic bottom formation at support."},
        {"Pattern": "Evening Star 🌙",     "Type": "Bearish reversal",     "Textbook reliability": "★★★★★", "What it means": "Big green → small candle → big red. Classic top formation at resistance."},
        {"Pattern": "Three White Soldiers","Type": "Bullish continuation", "Textbook reliability": "★★★★",  "What it means": "Three consecutive bullish candles after a base."},
    ])
    _next(2)

# ── CHAPTER 3: STOP-LOSS & RISK ───────────────────────────────────────────
with tab_g3, st.container(key="guide_ch_3"):
    _open(3, "Protecting capital: <em>stops</em>, reward-to-risk and sizing.", 5, [
        "Stop-loss vocabulary", "Risk : reward", "Position sizing", "Common mistakes",
    ])
    _lede(
        "A stop-loss is the price at which a losing trade is closed to prevent further losses. "
        "It is set before entry, not after the trade has moved against you."
    )
    _h3(3, 1, "Stop-loss vocabulary")
    _table([
        {"Term": "Stop-loss (SL)", "Meaning": "The exit price if the trade is wrong. Set before entering."},
        {"Term": "ATR stop",       "Meaning": "Stop 1.5–2× the Average True Range below entry; adjusts for each stock's typical daily movement."},
        {"Term": "Structure stop", "Meaning": "Stop just below a key support level (previous swing low, major moving average)."},
        {"Term": "Trailing stop",  "Meaning": "Stop that moves up as the price rises, locking in gains while letting winners run."},
        {"Term": "Breakeven stop", "Meaning": "Once a trade gains 1R, the stop moves to entry, so the trade can no longer lose money."},
    ])

    _h3(3, 2, "Risk : reward")
    st.markdown(
        "The **risk : reward ratio** compares how much a trade could lose with how much it could "
        "gain. At 1.5 : 1, every ₹100 at risk aims for ₹150."
    )
    _quote("Reward-to-risk sets the win rate a strategy needs just to break even.")
    _table([
        {"R:R ratio": "3:1 or higher", "Meaning": "Profitable long-term even at a 35% win rate"},
        {"R:R ratio": "2:1",           "Meaning": "Standard target; a 45% win rate profits consistently"},
        {"R:R ratio": "1.5:1",         "Meaning": "The common minimum; needs a win rate above 55%"},
        {"R:R ratio": "1:1",           "Meaning": "Break-even at best unless the win rate is very high (> 65%)"},
        {"R:R ratio": "< 1:1",         "Meaning": "Risking more than the potential reward; a mathematically losing shape"},
    ])

    _h3(3, 3, "Position sizing")
    st.markdown(
        "A common discipline is to risk no more than 1–2% of total capital on a single trade.  \n\n"
        "**Formula:** shares = (capital × risk %) ÷ (entry price − stop-loss price)  \n\n"
        "**Example:** ₹5,00,000 × 2% = ₹10,000 maximum loss. Entry ₹1,000 and stop ₹950 means "
        "₹50 risk per share, so 10,000 ÷ 50 = **200 shares** (₹2,00,000 invested, maximum loss "
        "₹10,000). The **Position Sizer** page does this arithmetic."
    )

    _h3(3, 4, "Common mistakes")
    _table([
        {"Mistake": "No stop-loss",                   "Consequence": "One bad trade can wipe out months of gains", "Discipline": "Set the stop before entering"},
        {"Mistake": "Moving the stop down",           "Consequence": "Turns a small loss into a large one",        "Discipline": "Stops only move in the trade's favour"},
        {"Mistake": "Averaging down losers",          "Consequence": "More capital trapped in a losing position",  "Discipline": "A hit stop closes the trade"},
        {"Mistake": "Holding losers, selling winners","Consequence": "A book of bad trades",                       "Discipline": "Let winners run, cut losers at the stop"},
        {"Mistake": "Trading on tips or news alone",  "Consequence": "No edge, random outcomes",                   "Discipline": "Check the score and chart for confirmation"},
        {"Mistake": "Overtrading",                    "Consequence": "Brokerage and taxes eat the gains",          "Discipline": "Fewer, better-researched decisions; the score alone is not a returns filter"},
    ])
    _next(3)

# ── CHAPTER 4: NEWS SIGNALS ───────────────────────────────────────────────
with tab_g4, st.container(key="guide_ch_4"):
    _open(4, "How news <em>moves</em> prices, and how to read it beside the score.", 4, [
        "Sentiment tags", "News alongside scores", "Indian market event calendar",
    ])
    _lede(
        "News is one of the fastest-moving market catalysts. The dashboard fetches recent news "
        "for each stock and tags it Positive, Negative or Neutral from its keywords."
    )
    _h3(4, 1, "Sentiment tags")
    _table([
        {"News type": "🟢 Positive", "Examples": "Strong quarterly results, big order wins, government policy support, rating upgrades, new product launches"},
        {"News type": "🔴 Negative", "Examples": "Profit warning, regulatory fine, management exit, debt downgrade, sector headwinds, fraud allegations"},
        {"News type": "⚪ Neutral",  "Examples": "AGM dates, routine management changes, product announcements without financials"},
    ])

    _h3(4, 2, "News alongside scores")
    _table([
        {"Score band": "Uptrend ▲",       "News": "Positive 🟢", "Combined read": "Aligned: trend and news point the same way",       "Historically": "The most internally consistent bullish case; still not a return forecast"},
        {"Score band": "Uptrend ▲",       "News": "Negative 🔴", "Combined read": "Conflict: healthy trend, fundamental headwind",     "Historically": "Uncertainty is elevated; trends can break quickly on bad news"},
        {"Score band": "Neutral 🟡",      "News": "Positive 🟢", "Combined read": "Possible improvement: score rises only if price follows", "Historically": "News often leads price; the score reacts once the tape moves"},
        {"Score band": "Neutral 🟡",      "News": "Negative 🔴", "Combined read": "Breakdown risk: no trend support under bad news",    "Historically": "Structurally the weakest support; downside moves tend to be larger"},
        {"Score band": "Broken trend ▼▼", "News": "Positive 🟢", "Combined read": "Mixed: broken trend despite good news",             "Historically": "Beaten-down names with good news can rebound sharply"},
        {"Score band": "Broken trend ▼▼", "News": "Negative 🔴", "Combined read": "Both bearish: trend and news aligned down",         "Historically": "The most internally consistent bearish case; still not a forecast"},
    ])

    _h3(4, 3, "Indian market event calendar")
    _table([
        {"Event": "Quarterly results (Q1–Q4)",           "When": "Apr / Jul / Oct / Jan", "Impact": "High: a stock can move 5–20% in one day around results."},
        {"Event": "RBI Monetary Policy Committee",       "When": "Every 2 months",        "Impact": "High for banks and rate-sensitive sectors (real estate, auto, NBFCs)."},
        {"Event": "Union Budget",                        "When": "1 Feb each year",       "Impact": "Very high and sector-specific; VIX often spikes before and eases the same day."},
        {"Event": "FII / DII flows",                     "When": "Daily",                 "Impact": "Medium: sustained FII selling weighs on the Nifty; FII buying supports rallies."},
        {"Event": "SEBI circulars / regulatory actions", "When": "As they occur",         "Impact": "Medium–high for specific sectors (fintech, brokers, insurance)."},
    ])
    _next(4)

# ── CHAPTER 5: PAPER TRADING ──────────────────────────────────────────────
with tab_g5, st.container(key="guide_ch_5"):
    _open(5, "Paper trading, <em>step by step</em>.", 4, [
        "The five steps", "The three numbers that define an edge",
    ])
    _lede(
        "Paper trading lets you practise decision-making with zero financial risk. Think of it "
        "as a flight simulator before flying a real plane."
    )
    _h3(5, 1, "The five steps")
    st.markdown("""
**1 · Find a setup.** Open **Analyze Stock** and search for a stock. Note the composite score
(0–90) and its trend label: they describe trend quality, not future returns. Check whether the
news is positive or neutral.

**2 · Open a paper trade.** Click **Paper Trade** in the ticker header on Analyze Stock, or use
the "Open New Paper Trade" form on **Paper Trades**. Entry, stop-loss and target are pre-filled
from the analysis; check the reward-to-risk shown before confirming.

**3 · Track the position.** Visit **Paper Trades** daily for live P&L on every open position.
If the price reaches the stop, **Close @ Stop**; if it reaches the target, **Close @ Target**.

**4 · Review performance** after 10–20 paper trades in the Performance Statistics section: win
rate, payoff ratio and expectancy (below).

**5 · Real money comes later, if at all.** Only after 30+ paper trades with positive expectancy,
starting with the smallest quantity and the same 1–2% risk per trade.
""")

    _h3(5, 2, "The three numbers that define an edge")
    _table([
        {"Number": "Win rate",     "Target": "> 45%",               "Note": "In the efficacy study, score band alone only weakly separated 20-day outcomes"},
        {"Number": "Payoff ratio", "Target": "> 1.5 : 1",           "Note": "Average win ÷ average loss; entering only at 1.5 : 1 or better helps"},
        {"Number": "Expectancy",   "Target": "Positive ₹ / trade",  "Note": "Cutting losses at the stop and letting winners reach target moves it up"},
    ])
    _callout(
        "**Remember:** the model describes historical patterns, and no model is always right. "
        "Paper trading exists to test whether the signals work for you before any real money is "
        "involved."
    )
    _next(5)

# ── CHAPTER 6: UNIVERSE & COVERAGE ────────────────────────────────────────
with tab_g6, st.container(key="guide_ch_6"):
    _open(6, "How many stocks the platform <em>actually scans</em>.", 2, [
        "The four universes", "What it means for you",
    ])
    _lede(
        "Pages like Live Ticker, Smart Screener, Top Picks and Tomorrow's Watchlist scan a "
        "universe, a defined list of NSE tickers, rather than every stock on the exchange. The "
        "universe decides how broad, and how slow, a scan is."
    )
    _h3(6, 1, "The four universes")
    _table([
        {"Universe": "Nifty 50",           "Size": "50",   "Composition": "The 50 largest, most liquid blue-chip stocks."},
        {"Universe": "Nifty 100",          "Size": "~100", "Composition": "Nifty 50 + Nifty Next 50."},
        {"Universe": "Nifty 500",          "Size": "~500", "Composition": "Nifty 100 + Nifty Midcap 150 + Nifty Smallcap 250: the standard large + mid + small cap index."},
        {"Universe": "Nifty Total Market", "Size": "~745", "Composition": "Nifty 500 + Nifty Microcap 250 (ranks 501–750). The broadest scan, used for Live Ticker's market-wide breadth."},
    ])
    _callout(
        "**Recently fixed:** the Midcap 150 and Smallcap 250 lists had drifted well short of the "
        "real index membership, and the Microcap 250 tier didn't exist, so broad scans silently "
        "covered only ~330–450 stocks instead of ~745–750. Both lists were rebuilt from NSE's "
        "official constituent files and the Microcap tier was added."
    )
    _h3(6, 2, "What it means for you")
    st.markdown(
        "A wider universe surfaces more candidates but takes longer to scan and includes smaller, "
        "less liquid names. Check average daily volume before sizing a position in anything "
        "outside the Nifty 500."
    )
    _next(6)

# ── CHAPTER 7: TQS SCANNER ────────────────────────────────────────────────
with tab_g7, st.container(key="guide_ch_7"):
    _open(7, "The TQS scanner, <em>a separate model</em>.", 3, [
        "Four equal pillars", "Grades and signals",
    ])
    _lede(
        "The TQS Scanner page ranks a whole universe by pure trend health. It also runs 0 – 90, "
        "but it is a different model from the composite score in chapter 1."
    )
    _callout(
        "**Don't confuse the two.** The Analyze Stock composite blends Technical + Momentum + "
        "Volume + Sentiment for a single trend label. TQS uses four equally weighted pillars, "
        "purpose-built for ranking many stocks.",
        warn=True,
    )
    _h3(7, 1, "Four equal pillars")
    st.markdown("Each pillar is worth **22.5 points**, for a total of 90:")
    _table([
        {"Pillar": "P1 · Trend strength",        "What it measures": "Moving-average alignment (Close > SMA20 > SMA50 > SMA200), ADX trend intensity, and SMA200 slope."},
        {"Pillar": "P2 · Trend persistence",     "What it measures": "Rolling Sharpe ratio over 5, 20 and 60 days: steady, low-noise trends score above choppy ones."},
        {"Pillar": "P3 · Momentum quality",      "What it measures": "RSI in a healthy 55–70 band (not just \"high\"), plus MACD histogram direction and state."},
        {"Pillar": "P4 · Technical confirmation","What it measures": "OBV z-score and slope percentile (is volume confirming the move?), plus volume vs the 20-day average."},
    ])
    _h3(7, 2, "Grades and signals")
    st.markdown(
        "Grade and signal come from two separate threshold scales, so they don't line up row for "
        "row: a TQS of 76 shows grade **A** (≥ 70) and signal **STRONG TREND** (≥ 75) at once. "
        "Both describe the same number."
    )
    st.markdown("**Grade scale**")
    _table([
        {"TQS range": "80 – 90", "Grade": "A+"},
        {"TQS range": "70 – 79", "Grade": "A"},
        {"TQS range": "55 – 69", "Grade": "B"},
        {"TQS range": "40 – 54", "Grade": "C"},
        {"TQS range": "25 – 39", "Grade": "D"},
        {"TQS range": "0 – 24",  "Grade": "F"},
    ])
    st.markdown("**Signal scale** (as the scanner page labels it)")
    _table([
        {"TQS range": "75 – 90", "Signal": "STRONG TREND"},
        {"TQS range": "60 – 74", "Signal": "TRENDING"},
        {"TQS range": "45 – 59", "Signal": "NEUTRAL"},
        {"TQS range": "30 – 44", "Signal": "WEAK"},
        {"TQS range": "0 – 29",  "Signal": "AVOID (no trend)"},
    ])
    _callout(
        "**How it fits together:** use the TQS Scanner to rank a universe and find the healthiest "
        "trends quickly, then open **Analyze Stock** on those names for the full picture: "
        "fundamentals, news, reference levels and the composite score."
    )

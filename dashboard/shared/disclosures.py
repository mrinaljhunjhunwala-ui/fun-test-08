"""dashboard/shared/disclosures.py — transparency notices for score & backtest pages.

Reusable, side-effect-free-of-data renderers so every surface shows the same
professional, non-alarmist disclosure:

  • render_survivorship_notice()    — present-day-universe / survivorship-bias notice
  • render_backtest_assumptions()   — commission, execution, slippage, survivorship status
  • render_score_methodology()      — "What this score measures" (trend quality, not returns)
  • render_regime_reliability_note() — VIX-regime reliability note from the 5y regime study

The score components implement Phase 1 (UI honesty) of the score research:
docs/SCORE_EFFICACY_REPORT.md and docs/REGIME_STUDY_REPORT.md found the composite score
correlates with trend persistence (+0.41 Spearman) far more than with future
returns (+0.04), and that its ranking power degrades/inverts in elevated-fear
regimes. Wording here reflects those findings; the scoring engine is unchanged.
"""
from __future__ import annotations
import logging
import streamlit as st

_log = logging.getLogger("dashboard.disclosures")


def render_survivorship_notice() -> None:
    """Concise survivorship-bias disclosure shown near the top of backtest pages."""
    st.info(
        "ℹ️ **Survivorship bias — please read.** Backtests run on the **present-day** "
        "stock universe (today's index membership). Companies that were delisted, "
        "merged, or dropped from the index during the test window are **not** included, "
        "and historical constituent changes are **not yet modelled**. Results therefore "
        "reflect today's survivors and can **overstate** what was achievable at the time. "
        "Treat them as directional research, not a guarantee of past or future returns."
    )


def render_backtest_assumptions() -> None:
    """Expandable 'assumptions & limitations' section (commission, execution, slippage,
    survivorship). Cost figures are read from the live backtest constants so the
    disclosure can never drift from what the engine actually charges."""
    # FIX COST-MODEL: figures come from trading.costs, the same model that
    # prices every backtest fill, so the disclosure can't drift from it.
    from trading import costs as _c
    _s = _c.DEFAULT
    _rt_2l = _c.representative_round_trip_pct(200_000) * 100
    _rt_20k = _c.representative_round_trip_pct(20_000) * 100

    with st.expander("📋 Backtest assumptions & limitations", expanded=False):
        st.markdown(
            f"""
**Commission — modelled per fill.** Indian equity-delivery charges on every buy and sell:
STT {_s.stt_delivery * 100:.1f}% on **both** legs · stamp duty {_s.stamp_delivery_buy * 100:.3f}% on buys ·
NSE {_s.exchange_txn * 100:.5f}% + SEBI fee · GST 18% · DP ₹{_s.dp_per_sell:.0f} per sell ·
brokerage lower of ₹{_s.brokerage_cap:.0f} or {_s.brokerage_pct * 100:.1f}% (min ₹{_s.brokerage_min:.0f}).
Round trip ≈ **{_rt_2l:.2f}%** on a ₹2L order and ≈ **{_rt_20k:.2f}%** on a ₹20K order
(fixed fees weigh more on small orders). Rates as of {_s.as_of}.
{"Each fill is charged exactly for its own order size." if _c.supports_callable_commission() else
 "This backtesting engine version takes one flat rate per trade, so every trade is charged the "
 "₹2L-order round trip; orders much smaller (larger) than ₹2L are under- (over-) charged."}

**Execution — realistic timing.** Signals are computed on each bar's **close**; orders fill at
the **next bar's open**. There are no same-bar fills and no look-ahead (verified by an automated
no-look-ahead regression test).

**Slippage — not modelled.** Fills assume the exact OHLC price. Real-world fills, especially in
mid- and small-caps, include bid–ask spread and market impact, so **live results will typically be
worse** than the backtest. Treat reported returns as an optimistic upper bound.

**Survivorship — not modelled.** The backtest uses the **current** index universe; delisted /
removed names and historical constituent changes are excluded (see the notice above).

**Data.** Daily bars. All indicators use only trailing (historical) windows.
"""
        )


def render_score_methodology(expanded: bool = False) -> None:
    """'What this score measures' — reusable methodology transparency component.

    Shown wherever the trend-quality (composite) score is a primary element.
    Grounded in the 5-year regime study (docs/REGIME_STUDY_REPORT.md, re-run July
    2026 after FIX EFF1, 86,589 obs).
    """
    with st.expander("ℹ️ What this score measures", expanded=expanded):
        st.markdown(
            "This is a **Trend Quality Score (max 90)** built from four price-based "
            "pillars:\n"
            "- **Trend strength** — moving-average alignment (price vs SMA 20/50/200) and ADX\n"
            "- **Trend persistence** — multi-horizon momentum (5/20/60-day returns)\n"
            "- **Momentum quality** — RSI zone and MACD state\n"
            "- **Technical confirmation** — volume behaviour (accumulation vs "
            "distribution). Candlestick patterns are shown for context but are "
            "not scored — a 5-year study found they added no ranking power.\n\n"
            "**What a high score means:** the stock is in a strong uptrend that has "
            "historically tended to *persist* (5-year validation: +0.40 rank correlation "
            "with staying in an uptrend over the following month).\n\n"
            "**What it does not mean:** the score is **not a direct forecast of future "
            "returns** (the same validation found only ≈ +0.02 correlation with next-month "
            "returns). A strong trend can drift sideways; a weak one can rebound. Use the "
            "score to assess trend health, then apply your own entry, risk and position "
            "rules. Not SEBI-registered investment advice."
        )


def render_regime_reliability_note() -> None:
    """VIX-regime reliability note for score surfaces.

    The 5-year regime study (research/regime_study.py, re-run July 2026 after
    FIX EFF1 — see docs/REGIME_STUDY_REPORT.md) found score rankings work in
    complacency-VIX regimes but degrade and invert as VIX rises (Spearman vs
    60-day forward return): complacency +0.15, normal −0.03, elevated −0.01,
    fear −0.06. "Normal" VIX is NOT reliably informative despite sitting
    between the two calm-sounding regimes — it's near-zero, not positive —
    so it deliberately gets neither the reassurance nor the warning message
    below; a claim either way would overstate what the data shows. This
    renderer surfaces that finding next to live scores, using the existing
    VIX plumbing. Read-only; degrades silently if VIX is unavailable.
    """
    try:
        from utils.vix import get_india_vix_regime
        info = get_india_vix_regime() or {}
        regime = str(info.get("regime", "unknown")).lower()
        vix = info.get("vix")
        vix_txt = f" (India VIX {vix:.1f})" if isinstance(vix, (int, float)) else ""
    except Exception as e:
        _log.debug("vix_warning_banner: VIX regime lookup failed, skipping banner: %s", e)
        return

    if regime in ("elevated", "fear", "panic"):
        st.warning(
            f"⚠️ **Reduced reliability in the current market regime{vix_txt}.** "
            "Historical testing (5-year study, 86,589 observations) shows trend-quality "
            "rankings become **less reliable — and can invert — as VIX rises**, "
            "when beaten-down stocks often rebound harder than trending ones. "
            "Interpret scores with additional caution and rely more on position sizing "
            "and stops.",
            icon="🌪️",
        )
    elif regime == "complacency":
        st.caption(
            f"🟢 Calm market regime{vix_txt} — historically the conditions where "
            "trend-quality rankings have been most informative (see Investor Guide → "
            "scores)."
        )
    # "normal" VIX deliberately gets no message here — see docstring above.

    try:
        from analysis.score import _live_regime_label
        note = trend_regime_note(_live_regime_label())
    except Exception as e:
        _log.debug("trend regime note skipped: %s", e)
        note = None
    if note:
        st.caption(note)


# 5y replay, weekly rank IC vs 20-session excess return (docs/DECAY_ATTRIBUTION_2026-10-01.md):
# trend_up +0.045 (t +5.2), range +0.000, trend_down -0.029 (t -1.5).
_LOW_INFO_TREND_REGIMES = {"range": "range-bound", "trend_down": "falling"}


def trend_regime_note(label) -> "str | None":
    """Descriptive caption for NIFTY trend regimes where the 5-year replay found the
    composite score carried no ranking information. None for every other regime."""
    name = _LOW_INFO_TREND_REGIMES.get(str(label or "").lower())
    if not name:
        return None
    return (f"ℹ️ **Historically low-information regime** — NIFTY is currently {name}. "
            "In the 5-year replay the composite score did not rank stocks in this regime "
            "(weekly rank correlation with 20-session excess return ≈ 0), while it did in "
            "rising markets. Scores here describe the chart; they have not predicted returns.")


def evidence_gated_pattern_label(
    patterns: list[str] | tuple[str, ...] | None,
    volume_ratio: float | None,
) -> tuple[str, str] | None:
    """Return (headline, caveat) for a candlestick-pattern display, or None
    when there is nothing to show.

    SH2 rule: patterns are informational context, never "signals". A pattern
    without volume confirmation is even weaker; the caveat must say so. The
    5-year study (RESEARCH_SCORE_VARIANTS.md, 40,663 obs) found the pattern
    component had zero-to-negative ranking power and was removed from the
    composite. This helper keeps the display honest wherever patterns are
    still surfaced for narrative context.

    Contract:
      - patterns empty/None → return None (nothing to render)
      - volume_ratio >= 1.5 → "volume confirms" caveat (still context, not signal)
      - volume_ratio < 1.5 or unknown → "no volume confirmation" caveat
      - headline never contains the word "signal"
    """
    if not patterns:
        return None
    joined = ", ".join(str(p) for p in patterns if p)
    if not joined:
        return None
    headline = f"Candlestick context (informational only): {joined}"
    try:
        vr = float(volume_ratio) if volume_ratio is not None else None
    except (TypeError, ValueError):
        vr = None
    if vr is not None and vr >= 1.5:
        caveat = (f"Volume {vr:.2f}× the 20-day average — pattern has some "
                  "volume support. Not a scored input.")
    elif vr is not None:
        caveat = (f"Volume only {vr:.2f}× the 20-day average — pattern is "
                  "unconfirmed context, not a trading signal.")
    else:
        caveat = ("Volume data unavailable — treat as unconfirmed context, "
                  "not a trading signal.")
    return headline, caveat


def oversold_reliability_suffix(regime: str | None) -> str:
    """Return a caveat suffix for oversold labels (RSI/Stoch) that reflects
    the VIX regime, or '' when no caveat is warranted.

    The 5-year regime study (REGIME_STUDY_REPORT.md) found that trend-quality
    rankings — the same family the oversold-bounce heuristic sits in — degrade
    or invert as VIX rises. An "Oversold" label in a fear regime therefore
    reads more like noise than a signal; SH2 requires the display to say so.
    """
    r = (regime or "").strip().lower()
    if r in ("elevated", "fear", "panic"):
        return " · reliability reduced in high-fear regime"
    return ""


def render_revenue_growth_evidence() -> None:
    """Brief evidence disclosure for revenue growth (Revenue Growth Visibility
    Phase 1, R4). Same honesty discipline as the trend-quality components:
    states the research finding, avoids recommendation/forecast/certainty.
    Source: docs/FUNDAMENTAL_QUALITY_REPORT.md (4,266 point-in-time observations)."""
    st.caption(
        "🔬 Revenue growth has been the **strongest return-predictive signal** "
        "identified in platform research (2022–2025 validation). Historical "
        "relationships may not persist in future market environments — a "
        "measured observation, not a buy signal."
    )

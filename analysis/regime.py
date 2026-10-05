"""
analysis/regime.py — composite market-regime classifier for NSE.

Problem this exists to solve
────────────────────────────
The 5-year score-efficacy run (see docs/SCORE_EFFICACY_REPORT.md and the
`accuracy_report.csv` produced by research/score_efficacy.py) showed that the
composite score is genuinely predictive in some periods (62-66 % hit rate,
+4 % avg net return on train half) and roughly a coin flip in others (~49 %
hit rate, 0 % return on holdout half). The difference between the two isn't
random — it lines up with WHAT REGIME the market is in.

A single-metric proxy for regime is what utils/vix.py has today (VIX zone
alone). That is thin: VIX is volatility, which correlates with regime but
isn't regime itself. A market can sit in low-VIX complacency for months while
mean-reverting; another can spend weeks in an "elevated" band while trending
upward. Traders who lose in mean-reverting regimes on trend-following signals
would rather know the regime IS trend vs range, not just what VIX is.

This module returns a COMPOSITE regime label that combines:

  1. VIX zone                           (already in utils/vix)
  2. Nifty vs its own 200-day SMA       (trend proxy)
  3. Market breadth                     (% of Nifty-500 above SMA-50)
  4. 20-day realised vol change         (regime SHIFT signal)

Each of these is a well-studied classic regime indicator on its own; combining
them with simple voting gives a more stable read than any one alone. Nothing
here is a new invention — this is textbook top-down analysis, packaged so the
scoring layer can consume it consistently.

The output is a `RegimeSnapshot` dataclass with:
  * a single label — one of {"trend_up", "trend_down", "range", "risk_off"}
  * per-component evidence, so the UI can explain WHY, not just SHOW the label
  * a confidence bucket (low/medium/high) based on how many components agree

Downstream, analysis/score.py will read this to modulate its BUY/STRONG BUY
thresholds and its narrative — that wiring is Phase 2 of the sprint and does
not live here. This module is deliberately UI-free and I/O-scoped to only
what it needs from data.fetcher.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

_log = logging.getLogger("analysis.regime")


# ─────────────────────────────────────────────────────────────────────────────
# Data class
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RegimeSnapshot:
    """One evaluation of the composite regime classifier."""
    label:      str                          # trend_up | trend_down | range | risk_off | unknown
    confidence: str                          # low | medium | high
    components: Dict[str, str] = field(default_factory=dict)   # per-component labels
    metrics:    Dict[str, float] = field(default_factory=dict) # raw numbers
    reasons:    List[str] = field(default_factory=list)        # human-readable one-liners

    def as_dict(self) -> Dict:
        return {
            "label": self.label,
            "confidence": self.confidence,
            "components": self.components,
            "metrics": self.metrics,
            "reasons": self.reasons,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Individual components
# ─────────────────────────────────────────────────────────────────────────────

def _vix_component(vix: Optional[float]) -> str:
    """Match utils/vix.get_india_vix_regime buckets."""
    if vix is None or not np.isfinite(vix):
        return "unknown"
    if vix < 12:  return "complacency"
    if vix < 16:  return "normal"
    if vix < 22:  return "elevated"
    if vix < 28:  return "fear"
    return "panic"


def _trend_component(nifty_close: Optional[pd.Series]) -> str:
    """Nifty vs its own SMA200 — the single most-quoted trend indicator."""
    if nifty_close is None or len(nifty_close) < 210:
        return "unknown"
    sma200 = nifty_close.rolling(200).mean().iloc[-1]
    close  = float(nifty_close.iloc[-1])
    if not (np.isfinite(sma200) and sma200 > 0):
        return "unknown"
    ratio = close / float(sma200) - 1.0
    # Bands chosen so borderline "just crossed" cases aren't classified as strong.
    if ratio >  0.03: return "above"          # > 3% above SMA200 → clean uptrend
    if ratio > -0.03: return "at"             # within ±3% → neither
    return "below"                             # > 3% below → clean downtrend


def _breadth_component(pct_above_sma50: Optional[float]) -> str:
    """
    % of Nifty-500 above its own SMA-50.  This is the classic breadth check.
    > 60 %  = broad participation → trend intact
    < 40 %  = narrow, deteriorating → distribution/topping / mean-reverting
    40–60 %  = mixed
    """
    if pct_above_sma50 is None or not np.isfinite(pct_above_sma50):
        return "unknown"
    if pct_above_sma50 >= 60: return "broad"
    if pct_above_sma50 >= 40: return "mixed"
    return "narrow"


def _vol_trend_component(nifty_close: Optional[pd.Series]) -> str:
    """
    Direction of realised volatility — 20d std of returns, now vs 20 bars ago.
    Rising vol usually precedes/accompanies regime SHIFTS; falling vol
    indicates settling.
    """
    if nifty_close is None or len(nifty_close) < 40:
        return "unknown"
    r  = nifty_close.pct_change().dropna()
    v0 = r.iloc[-40:-20].std()
    v1 = r.iloc[-20:].std()
    if not (np.isfinite(v0) and np.isfinite(v1) and v0 > 0):
        return "unknown"
    delta = (v1 / v0) - 1.0
    if delta >  0.15: return "rising"
    if delta < -0.15: return "falling"
    return "stable"


# ─────────────────────────────────────────────────────────────────────────────
# Composite scoring rules
# ─────────────────────────────────────────────────────────────────────────────

# Rule table — voting by (vix_zone, trend, breadth). Deliberately simple so it
# stays interpretable; each rule has an evidence-based rationale in the docs.
#
# Rules of thumb (from the 5-year efficacy study):
#   trend_up   : Nifty above SMA200 + broad breadth + normal/elevated VIX
#                (the regime where composite score's momentum-heavy factors work)
#   trend_down : Nifty below SMA200 + narrow breadth + elevated/fear VIX
#                (mirror of trend_up, but on the short side)
#   range      : trend at/mixed + normal/complacency VIX
#                (the regime where the score was ~coin-flip on 2023-25 holdout)
#   risk_off   : panic/fear VIX regardless of trend
#                (fear regime returned +8.84% avg on 5y run — but only survivable
#                 for people who can stomach it; treat as its own bucket)
def _combine(vix_z: str, trend: str, breadth: str, vol: str
             ) -> "tuple[str, str, List[str]]":
    reasons: List[str] = []

    if vix_z in ("panic", "fear"):
        reasons.append(f"VIX regime is {vix_z} — treat as risk-off")
        conf = "high" if vix_z == "panic" else "medium"
        return "risk_off", conf, reasons

    # Trend-up: price above SMA200 AND (broad breadth OR breadth unknown).
    # FIX REGIME-BREADTH — original rule required breadth == "broad" strictly,
    # so any caller that couldn't supply breadth (the historical validation
    # path, plus snapshot_live() by design) ended up in "range" 100 % of the
    # time — including through 2020-21 which was unmistakably a trending bull.
    # Falling back to trend + VIX when breadth is missing keeps the classifier
    # informative in the fallback case; confidence is capped at medium so the
    # UI can tell the two situations apart.
    if trend == "above" and breadth in ("broad", "unknown"):
        reasons.append("Nifty > SMA200 by > 3 %")
        if breadth == "broad":
            reasons.append("> 60 % of Nifty-500 above their own SMA-50 (broad participation)")
            conf = "high" if vix_z in ("normal", "elevated") else "medium"
        else:
            reasons.append("breadth not used — classified on trend + VIX (as in the historical studies)")
            conf = "medium"
        return "trend_up", conf, reasons

    # Trend-down: mirror
    if trend == "below" and breadth in ("narrow", "unknown"):
        reasons.append("Nifty < SMA200 by > 3 %")
        if breadth == "narrow":
            reasons.append("< 40 % of Nifty-500 above SMA-50 (narrow, deteriorating)")
            conf = "high" if vix_z in ("elevated", "fear") else "medium"
        else:
            reasons.append("breadth not used — classified on trend + VIX (as in the historical studies)")
            conf = "medium"
        return "trend_down", conf, reasons

    # FIX REGIME-NODATA (replica-test S3, 2026-10-04): with neither trend nor
    # breadth known there is no evidence for ANY regime, yet this fell through
    # to "range" — so a full feed outage put "range-bound market" on the
    # Command Centre hero. (VIX fear/panic already returned risk_off above.)
    if trend == "unknown" and breadth == "unknown":
        reasons.append("trend and breadth data unavailable — no regime read")
        return "unknown", "low", reasons

    # Everything else is a range regime — the point being that in a range
    # the composite score's factor mix under-performs (see 2023-25 holdout).
    reasons.append(f"trend={trend}, breadth={breadth} — no clear directional consensus")
    if vol == "rising":
        reasons.append("realised vol rising — a regime shift may be underway")
    conf = "medium" if trend != "unknown" and breadth != "unknown" else "low"
    return "range", conf, reasons


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def classify(vix: Optional[float],
             nifty_close: Optional[pd.Series],
             pct_above_sma50: Optional[float]) -> RegimeSnapshot:
    """
    Combine the four components into a single RegimeSnapshot.

    All inputs are optional — this function returns "unknown" gracefully if
    any component is missing rather than raising. That matters because live
    callers (Streamlit pages) will hit this on every page load and one
    provider outage shouldn't crash the classifier.
    """
    vix_z   = _vix_component(vix)
    trend   = _trend_component(nifty_close)
    breadth = _breadth_component(pct_above_sma50)
    vol     = _vol_trend_component(nifty_close)

    label, conf, reasons = _combine(vix_z, trend, breadth, vol)

    components = {"vix": vix_z, "trend": trend, "breadth": breadth, "vol": vol}
    metrics: Dict[str, float] = {}
    if vix is not None and np.isfinite(vix):
        metrics["vix"] = float(vix)
    if nifty_close is not None and len(nifty_close) >= 200:
        sma200 = float(nifty_close.rolling(200).mean().iloc[-1])
        close  = float(nifty_close.iloc[-1])
        if np.isfinite(sma200) and sma200 > 0:
            metrics["nifty_above_sma200_pct"] = round((close / sma200 - 1.0) * 100, 2)
    if pct_above_sma50 is not None and np.isfinite(pct_above_sma50):
        metrics["pct_above_sma50"] = round(float(pct_above_sma50), 1)

    return RegimeSnapshot(label=label, confidence=conf,
                          components=components, metrics=metrics, reasons=reasons)


# ─────────────────────────────────────────────────────────────────────────────
# Historical vector (for backtesting the regime classifier itself)
# ─────────────────────────────────────────────────────────────────────────────

def classify_history(nifty_close: pd.Series,
                     vix_close:   Optional[pd.Series],
                     breadth_pct: Optional[pd.Series]) -> pd.Series:
    """
    Vectorised version of classify() over a whole history. Returns a
    pandas.Series of regime labels aligned to nifty_close.index.

    Used by research/regime_validation.py to tag every observation.csv row
    with its as-of regime and measure hit-rate lift per label. Live callers
    should use classify(), not this — this function assumes it can see the
    full history at once.
    """
    if nifty_close is None or nifty_close.empty:
        return pd.Series(dtype=object)

    sma200 = nifty_close.rolling(200).mean()
    ratio  = nifty_close / sma200.replace(0, np.nan) - 1.0

    trend = pd.Series("unknown", index=nifty_close.index, dtype=object)
    trend[ratio >  0.03] = "above"
    trend[ratio.between(-0.03, 0.03, inclusive="both")] = "at"
    trend[ratio < -0.03] = "below"
    trend[sma200.isna()] = "unknown"

    if vix_close is not None:
        vix_aligned = vix_close.reindex(nifty_close.index).ffill()
    else:
        vix_aligned = pd.Series(np.nan, index=nifty_close.index)
    vix_z = vix_aligned.apply(_vix_component)

    if breadth_pct is not None:
        breadth_aligned = breadth_pct.reindex(nifty_close.index).ffill()
        breadth_lbl = breadth_aligned.apply(_breadth_component)
    else:
        breadth_lbl = pd.Series("unknown", index=nifty_close.index, dtype=object)

    # Vol trend — 20-bar realised vol now vs 20 bars ago
    r  = nifty_close.pct_change()
    v  = r.rolling(20).std()
    delta = v / v.shift(20) - 1.0
    vol_lbl = pd.Series("unknown", index=nifty_close.index, dtype=object)
    vol_lbl[delta >  0.15] = "rising"
    vol_lbl[delta.between(-0.15, 0.15, inclusive="both")] = "stable"
    vol_lbl[delta < -0.15] = "falling"

    labels = []
    for vz, tr, br, vl in zip(vix_z, trend, breadth_lbl, vol_lbl):
        lbl, _, _ = _combine(vz, tr, br, vl)
        labels.append(lbl)
    return pd.Series(labels, index=nifty_close.index, name="regime")


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: live snapshot fetched fresh
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Cross-sectional dispersion filter  (FIX REGIME-DISP — Path B outcome)
# ─────────────────────────────────────────────────────────────────────────────
#
# The regime-axes search (research/regime_axes_search.py) tested three
# alternative regime detectors against the 5y observations and found ONE
# stable, honest signal: on the 2023-25 holdout half, BUY signals in the
# LOWEST cross-sectional-dispersion bucket (< 8.5) hit only 43.8 %, while
# higher-dispersion buckets hit 48-56 %. Low dispersion = everything moves
# together = individual stock picks matter less. High dispersion = real
# winners AND losers exist = stock-picking works.
#
# The direction of the effect flips between train and holdout — so we
# deliberately DO NOT use this as an "amplifier" for high-dispersion
# regimes (that would be overfitting to holdout). We use it only as a
# FLOOR: below the low-dispersion threshold, damp the BUY confidence. That
# is the ONE reliable finding from the axis search.
#
# Threshold 8.5 is the empirical boundary from the axes-search bucket
# analysis; adjustable if a re-run against a longer window recommends
# otherwise (see docs/ if we ever produce a regime-tuning report).
DISPERSION_LOW_THRESHOLD = 8.5


def cross_sectional_dispersion_20d(returns_frames: Dict[str, "pd.Series"]
                                   ) -> Optional[float]:
    """
    Standard deviation of the last-20-day return, computed across every
    ticker in `returns_frames`.

    `returns_frames` is a dict of ticker → Close-price series. Callers
    should pass whatever universe they already have loaded (Top Picks
    scan uses Nifty 50 in dashboard/shared/cache.py — reusing those frames
    keeps this free).

    Returns None if fewer than 20 tickers have enough history.
    """
    values: List[float] = []
    for _t, s in returns_frames.items():
        try:
            if s is None or len(s) < 21:
                continue
            r = (float(s.iloc[-1]) / float(s.iloc[-21]) - 1.0) * 100.0
            if np.isfinite(r):
                values.append(r)
        except Exception:
            continue
    if len(values) < 20:
        return None
    return float(np.std(values, ddof=1))


def dispersion_verdict(value: Optional[float]) -> Dict[str, str]:
    """
    Turn a dispersion number into a UI-ready verdict.

    Returns {'zone': ..., 'note': ...} where zone is one of
    {"low", "normal", "high", "unknown"} and note is a one-line phrase
    the narrative layer can paste into a BUY signal disclosure.
    """
    if value is None or not np.isfinite(value):
        return {"zone": "unknown", "note": ""}
    if value < DISPERSION_LOW_THRESHOLD:
        return {
            "zone": "low",
            "note": (
                f"Cross-sectional dispersion is {value:.1f} — below the "
                f"{DISPERSION_LOW_THRESHOLD} threshold. In this regime "
                f"individual uptrend signals have historically hit ~44 % vs "
                f"~50 % baseline — signals are less reliable in low-dispersion markets."
            ),
        }
    if value > 15.0:
        return {
            "zone": "high",
            "note": (
                f"Cross-sectional dispersion is {value:.1f} — high. Real "
                f"winners and losers exist right now; stock-picking has "
                f"more room than in a correlated regime."
            ),
        }
    return {"zone": "normal", "note": ""}


def snapshot_live() -> RegimeSnapshot:
    """
    Fetch VIX / Nifty / breadth right now and return a classification.
    Meant for Streamlit pages — wrap with @st.cache_data(ttl=1800) at the
    call site (breadth in particular is expensive to compute).
    """
    from data.fetcher import fetch_single

    vix_val:   Optional[float]      = None
    nifty:     Optional[pd.Series]  = None
    breadth:   Optional[float]      = None

    try:
        vdf = fetch_single("^INDIAVIX", period="1mo")
        if vdf is not None and not vdf.empty:
            vix_val = float(vdf["Close"].dropna().iloc[-1])
    except Exception as e:
        _log.debug("regime.snapshot_live: VIX fetch failed: %s", e)

    try:
        ndf = fetch_single("^NSEI", period="2y")
        if ndf is not None and not ndf.empty:
            nifty = ndf["Close"].astype(float).dropna()
    except Exception as e:
        _log.debug("regime.snapshot_live: Nifty fetch failed: %s", e)

    # Breadth: we deliberately do NOT recompute here — that's a 500-ticker scan.
    # A caller with a fresh breadth number (from get_market_breadth in cache.py)
    # should pass it via classify(). snapshot_live() omits it, so the regime
    # will read as unknown/low-confidence on the breadth axis only.
    return classify(vix=vix_val, nifty_close=nifty, pct_above_sma50=breadth)

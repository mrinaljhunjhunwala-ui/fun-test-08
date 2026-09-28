"""analysis/thesis/thesis_engine.py — orchestration + input assembly.

Phase A1. NO AI, NO narrative. Two layers:

  * `generate_thesis(inputs)` — PURE: runs the rules on a `ThesisInputs` snapshot and
    returns a `ThesisResult`. This is what the tests target; it touches no subsystem.

  * `build_inputs(ticker, ...)` — the integration seam. Assembles a `ThesisInputs` from
    the platform's EXISTING capabilities (composite score, deep-confirmation, fundamentals
    analytics, beta, sector). Each subsystem is optional and wrapped: if one is missing or
    pre-computed by the caller, the engine still produces a (smaller) thesis. The caller
    (the Analyze page) passes the score + deep-confirmation it already computed, so no
    work is repeated and `analysis/` never hard-imports `dashboard/`.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from .thesis_models import (
    ThesisInputs, ThesisResult,
    SRC_FUNDAMENTALS, SRC_TECHNICAL, SRC_MOMENTUM, SRC_DEEP, SRC_BETA, SRC_SENTIMENT,
    SRC_COMPOSITE, SRC_LIQUIDITY,
)
from . import thesis_rules as rules


# ─────────────────────────────── pure core ──────────────────────────────────────
import logging as _logging
_log = _logging.getLogger("analysis.thesis.thesis_engine")

def generate_thesis(inputs: ThesisInputs) -> ThesisResult:
    """Run the deterministic rules. No network, no subsystem calls."""
    bull = rules.bull_factors(inputs)
    bear = rules.bear_factors(inputs)
    risks = rules.key_risks(inputs)
    verdict, vscore, rationale = rules.compute_verdict(inputs, bull, bear, risks)

    present = []
    if inputs.composite_score is not None:
        present.append(SRC_COMPOSITE)
    if inputs.technical_score is not None:
        present.append(SRC_TECHNICAL)
    if inputs.momentum_score is not None:
        present.append(SRC_MOMENTUM)
    if any(v is not None for v in (inputs.weekly_trend, inputs.rel_strength,
                                   inputs.earnings_days, inputs.signal_total)):
        present.append(SRC_DEEP)
    if any(v is not None for v in (inputs.revenue_cagr, inputs.eps_cagr,
                                   inputs.roe, inputs.debt_to_equity,
                                   inputs.roce, inputs.fcf)):
        present.append(SRC_FUNDAMENTALS)
    if inputs.beta is not None:
        present.append(SRC_BETA)
    if inputs.news_sentiment is not None:
        present.append(SRC_SENTIMENT)
    if inputs.liquidity_tier is not None:
        present.append(SRC_LIQUIDITY)

    return ThesisResult(
        ticker=inputs.ticker,
        verdict=verdict,
        verdict_score=vscore,
        verdict_rationale=rationale,
        bull_factors=bull,
        bear_factors=bear,
        key_risks=risks,
        inputs_present=present,
        notes=rules.sector_notes(inputs),
    )


# ─────────────────────────── integration seam ───────────────────────────────────
def _from_composite(inp: ThesisInputs, cs: Any) -> None:
    """Copy fields off a CompositeScore (or its as_dict)."""
    if cs is None:
        return
    g = (lambda k, a: cs.get(k) if isinstance(cs, dict) else getattr(cs, a, None))
    inp.composite_score = g("score", "score")
    inp.action = g("action", "action")
    inp.grade = g("grade", "grade")
    # FIX BEAR-NEUTRAL: the thesis describes the stock, so it reads the raw
    # technical points; the composite holds them at 20 in bear regimes.
    _raw = g("technical_raw", "technical_raw")
    inp.technical_score = _raw if _raw is not None else g("technical", "technical_score")
    inp.momentum_score = g("momentum", "momentum_score")
    inp.volume_score = g("volume", "volume_score")
    inp.sentiment_score = g("sentiment", "sentiment_score")
    inp.risk_reward = g("rr", "risk_reward")
    inp.sector = inp.sector or g("sector", "sector")


def _from_deep(inp: ThesisInputs, dc: Any) -> None:
    if not isinstance(dc, dict):
        return
    inp.weekly_trend = dc.get("weekly")
    inp.rel_strength = dc.get("rel_strength")
    inp.rs_pct = dc.get("rs_pct")
    inp.earnings_days = dc.get("earnings_days")
    inp.signal_bull = dc.get("bull")
    inp.signal_total = dc.get("total")


def _from_fundamentals(inp: ThesisInputs, fa: Any) -> None:
    """`fa` is {'results': compute_all(...) dict, 'partial': bool}."""
    if not isinstance(fa, dict) or "results" not in fa:
        return
    r = fa["results"]

    def val(key):
        a = r.get(key)
        return a.value if (a is not None and a.available and a.value is not None) else None

    inp.revenue_cagr = val("revenue_cagr")
    inp.eps_cagr = val("eps_cagr")
    inp.roe = val("roe")
    inp.debt_to_equity = val("debt_to_equity")
    inp.roce = val("roce")
    inp.fcf = val("fcf")
    inp.fundamentals_partial = bool(fa.get("partial"))


def build_inputs(ticker: str, *,
                 composite: Any = None,
                 deep: Any = None,
                 fundamentals: Any = None,
                 beta: Optional[float] = None,
                 sector: Optional[str] = None,
                 news_sentiment: Optional[str] = None,
                 liquidity: Any = None) -> ThesisInputs:
    """Assemble a ThesisInputs from existing platform capabilities.

    Any piece passed in is used as-is (the UI passes the score + deep-confirmation it
    already has). Anything not passed is lazily, defensively loaded from the live
    subsystem; failures degrade to None rather than raising.
    """
    inp = ThesisInputs(ticker=ticker)

    # Composite score + components
    cs = composite
    if cs is None:
        try:
            from analysis.score import score_stock
            cs = score_stock(ticker)
        except Exception as _e:
            _log.debug("thesis.%s degraded: %s", "build_inputs", _e)
            cs = None
    _from_composite(inp, cs)

    # Deep-confirmation signals
    dc = deep
    if dc is None:
        try:
            from dashboard.shared.cache import _deep_confirmation
            dc = _deep_confirmation(ticker)
        except Exception as _e:
            _log.debug("thesis.%s degraded: %s", "build_inputs", _e)
            dc = None
    _from_deep(inp, dc)

    # Fundamentals analytics
    fa = fundamentals
    if fa is None:
        try:
            from analysis.fundamentals.service import default_service
            from analysis.fundamentals import analytics as _fa
            cf = default_service().get_fundamentals(ticker)
            fa = {"results": _fa.compute_all(cf), "partial": bool(getattr(cf, "is_partial", False))}
        except Exception as _e:
            _log.debug("thesis.%s degraded: %s", "build_inputs", _e)
            fa = None
    _from_fundamentals(inp, fa)

    # Beta
    if beta is None:
        try:
            from analysis.hedging import calculate_stock_beta
            b = calculate_stock_beta(ticker)
            beta = None if (b is None or (isinstance(b, float) and math.isnan(b))) else float(b)
        except Exception as _e:
            _log.debug("thesis.%s degraded: %s", "build_inputs", _e)
            beta = None
    inp.beta = beta

    # Sector
    if sector is None and inp.sector is None:
        try:
            from data.universe import get_sector
            sector = get_sector(ticker)
        except Exception as _e:
            _log.debug("thesis.%s degraded: %s", "build_inputs", _e)
            sector = None
    inp.sector = inp.sector or sector
    inp.news_sentiment = news_sentiment

    # Sector classification (Phase D1) — single source of truth; drives metric applicability
    try:
        from analysis.sector_classification import classify_sector
        inp.sector_profile = classify_sector(inp.sector,
                                             name=getattr(cs, "company_name", None)
                                             if cs is not None and not isinstance(cs, dict) else None)
    except Exception as _e:
        _log.debug("thesis.%s degraded: %s", "build_inputs", _e)
        inp.sector_profile = None

    # Liquidity (Phase C1) — from existing OHLCV; accept a pre-computed LiquidityContext
    lq = liquidity
    if lq is None:
        try:
            from analysis.liquidity import liquidity_for_ticker
            lq = liquidity_for_ticker(ticker)
        except Exception as _e:
            _log.debug("thesis.%s degraded: %s", "build_inputs", _e)
            lq = None
    if lq is not None:
        tier = getattr(lq, "liquidity_tier", None)
        inp.liquidity_tier = tier if tier and tier != "Unknown" else None
        inp.avg_daily_turnover = getattr(lq, "avg_daily_turnover_30d", None)
    return inp


def thesis_for_ticker(ticker: str, **pieces) -> ThesisResult:
    """Convenience: build_inputs(...) then generate_thesis(...)."""
    return generate_thesis(build_inputs(ticker, **pieces))

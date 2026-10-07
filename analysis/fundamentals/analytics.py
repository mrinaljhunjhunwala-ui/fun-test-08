"""analysis/fundamentals/analytics.py — pure analytics over the normalized schema.

Every function:
  * takes a CompanyFundamentals and returns an AnalyticResult,
  * returns value=None (NEVER 0) with available=False + a human reason when it can't
    be computed,
  * attaches confidence + the exact inputs used (periods, dates, source) — no silent
    failures, no fallback to zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from .models import CompanyFundamentals

Confidence = str  # "high" | "medium" | "low" | "none"


@dataclass
class AnalyticResult:
    metric: str
    value: Optional[float]                 # None when unavailable — never substituted with 0
    unit: str                              # "%" | "x"
    available: bool
    confidence: Confidence
    reason: str = ""                       # why unavailable, or a caveat on the value
    detail: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {"metric": self.metric, "value": self.value, "unit": self.unit,
                "available": self.available, "confidence": self.confidence,
                "reason": self.reason, "detail": self.detail}


def _na(metric: str, unit: str, reason: str) -> AnalyticResult:
    return AnalyticResult(metric=metric, value=None, unit=unit, available=False,
                          confidence="none", reason=reason)


def _series(records, value_attr) -> List[Tuple[date, float]]:
    """(period_end, value) pairs with both present, sorted oldest→newest."""
    pts = []
    for r in records:
        d = r.period.period_end if r.period else None
        v = getattr(r, value_attr, None)
        if d is not None and v is not None:
            pts.append((d, float(v)))
    pts.sort(key=lambda x: x[0])
    return pts


def _pick_endpoints(pts: List[Tuple[date, float]], years: int):
    """Return (start, end) spanning at most `years`, longest available within that."""
    end = pts[-1]
    cutoff = end[0] - timedelta(days=int(365.25 * years))
    within = [p for p in pts[:-1] if p[0] >= cutoff]
    start = min(within, key=lambda x: x[0]) if within else pts[0]
    return start, end


def _cagr_confidence(span_years: float, n_points: int) -> Confidence:
    if span_years >= 4 and n_points >= 4:
        return "high"
    if span_years >= 2:
        return "medium"
    return "low"


def _growth_cagr(cf, records, value_attr, metric, years) -> AnalyticResult:
    pts = _series(records, value_attr)
    if len(pts) < 2:
        return _na(metric, "%", f"need ≥2 annual data points; have {len(pts)}")
    start, end = _pick_endpoints(pts, years)
    span = (end[0] - start[0]).days / 365.25
    if span < 0.9:
        return _na(metric, "%", "data points span under a year")
    if start[1] <= 0:
        return _na(metric, "%", f"starting value non-positive ({start[1]:.0f}) — CAGR undefined")
    if end[1] <= 0:
        return _na(metric, "%", f"latest value non-positive ({end[1]:.0f}) — CAGR undefined")
    cagr = (end[1] / start[1]) ** (1.0 / span) - 1.0
    return AnalyticResult(
        metric=metric, value=round(cagr * 100, 2), unit="%", available=True,
        confidence=_cagr_confidence(span, len(pts)),
        reason="" if span >= years - 0.5 else f"computed over {span:.1f}y (history < {years}y requested)",
        detail={"start_date": str(start[0]), "end_date": str(end[0]),
                "start_value": start[1], "end_value": end[1],
                "span_years": round(span, 2), "points": len(pts),
                "source": cf.provider_name},
    )


# ───────────────────────────── the four analytics ──────────────────────────────
def revenue_cagr(cf: CompanyFundamentals, years: int = 5) -> AnalyticResult:
    return _growth_cagr(cf, cf.income_statements, "revenue", "Revenue CAGR", years)


def revenue_yoy(cf: CompanyFundamentals) -> AnalyticResult:
    """Latest annual revenue vs the annual statement before it (YoY, %).

    This is the definition the revenue-growth research supports
    (docs/REVENUE_GROWTH_RECONCILE_2026-10-07.md: "YoY (last 2)", per-date IC
    about +0.07 in the original window). Long CAGRs (5-10y) showed no signal.
    The two statements must be 9-15 months apart so a gap year is not
    silently annualised as YoY.
    """
    metric = "Revenue growth (YoY)"
    pts = _series(cf.income_statements, "revenue")
    if len(pts) < 2:
        return _na(metric, "%", f"need >=2 annual data points; have {len(pts)}")
    (d0, v0), (d1, v1) = pts[-2], pts[-1]
    gap = (d1 - d0).days
    if not 270 <= gap <= 460:
        return _na(metric, "%", f"last two statements are {gap} days apart, not a year")
    if v0 <= 0:
        return _na(metric, "%", f"prior-year revenue non-positive ({v0:.0f})")
    return AnalyticResult(
        metric=metric, value=round((v1 / v0 - 1.0) * 100, 2), unit="%", available=True,
        confidence="medium", reason="",
        detail={"start_date": str(d0), "end_date": str(d1), "start_value": v0,
                "end_value": v1, "source": cf.provider_name},
    )


def eps_cagr(cf: CompanyFundamentals, years: int = 5) -> AnalyticResult:
    pts = _series(cf.income_statements, "eps_diluted")
    if len(pts) < 2:
        pts = _series(cf.income_statements, "eps_basic")
    if len(pts) < 2:
        return _na("EPS CAGR", "%", "no usable diluted/basic EPS history (≥2 points)")
    # reuse the growth engine on whichever EPS series we found
    attr = "eps_diluted" if len(_series(cf.income_statements, "eps_diluted")) >= 2 else "eps_basic"
    return _growth_cagr(cf, cf.income_statements, attr, "EPS CAGR", years)


def roe(cf: CompanyFundamentals) -> AnalyticResult:
    inc = cf.latest_income()
    bals = cf.balance_sheets
    ni = inc.net_income if inc else None
    if ni is not None and bals and bals[0].total_equity is not None:
        eq_latest = bals[0].total_equity
        # average equity (latest + prior) is the textbook denominator when available
        if len(bals) >= 2 and bals[1].total_equity is not None:
            eq = (eq_latest + bals[1].total_equity) / 2.0
            basis = "average equity (2 periods)"
        else:
            eq, basis = eq_latest, "latest equity"
        if eq <= 0:
            return _na("ROE", "%", f"equity non-positive ({eq:.0f}) — ROE not meaningful")
        return AnalyticResult("ROE", round(ni / eq * 100, 2), "%", True, "high",
                              detail={"net_income": ni, "equity_basis": basis,
                                      "equity": eq, "source": cf.provider_name})
    # fallback: vendor-supplied ratio
    if cf.ratios and cf.ratios.roe is not None:
        return AnalyticResult("ROE", round(cf.ratios.roe * 100, 2), "%", True, "medium",
                              reason="from provider ratio (statements incomplete)",
                              detail={"source": f"{cf.provider_name} ratio"})
    return _na("ROE", "%", "missing net income or shareholders' equity")


def debt_to_equity(cf: CompanyFundamentals) -> AnalyticResult:
    bal = cf.latest_balance()
    if bal and bal.total_debt is not None and bal.total_equity is not None:
        if bal.total_equity <= 0:
            return _na("Debt/Equity", "x",
                       f"equity non-positive ({bal.total_equity:.0f}) — D/E not meaningful")
        return AnalyticResult("Debt/Equity", round(bal.total_debt / bal.total_equity, 2),
                              "x", True, "high",
                              detail={"total_debt": bal.total_debt,
                                      "total_equity": bal.total_equity,
                                      "source": cf.provider_name})
    if cf.ratios and cf.ratios.debt_to_equity is not None:
        return AnalyticResult("Debt/Equity", round(cf.ratios.debt_to_equity, 2), "x",
                              True, "medium",
                              reason="from provider ratio (statements incomplete)",
                              detail={"source": f"{cf.provider_name} ratio"})
    return _na("Debt/Equity", "x", "missing total debt or shareholders' equity")


def roce(cf: CompanyFundamentals) -> AnalyticResult:
    """Return on Capital Employed.

    Formula:  ROCE = EBIT / Capital Employed
              EBIT            = IncomeStatement.operating_income (latest)
              Capital Employed = BalanceSheet.total_assets − current_liabilities (latest)

    Data source: existing normalized statements only (no provider/network change).
    Edge cases: any input missing → unavailable (None, never 0); non-positive capital
    employed → unavailable (ROCE not meaningful).
    """
    inc = cf.latest_income()
    bal = cf.latest_balance()
    ebit = inc.operating_income if inc else None
    if ebit is None:
        return _na("ROCE", "%", "missing operating income (EBIT)")
    if bal is None or bal.total_assets is None or bal.current_liabilities is None:
        return _na("ROCE", "%", "missing total assets or current liabilities")
    ce = bal.total_assets - bal.current_liabilities
    if ce <= 0:
        return _na("ROCE", "%", f"capital employed non-positive ({ce:.0f}) — ROCE not meaningful")
    return AnalyticResult("ROCE", round(ebit / ce * 100, 2), "%", True, "high",
                          detail={"ebit": ebit, "capital_employed": ce,
                                  "total_assets": bal.total_assets,
                                  "current_liabilities": bal.current_liabilities,
                                  "source": cf.provider_name})


def free_cash_flow(cf: CompanyFundamentals) -> AnalyticResult:
    """Latest Free Cash Flow, in ₹ crore.

    Data source: CashFlow.free_cash_flow (already populated, or derived as OCF − capex in
    the provider). Never fabricated — missing → unavailable.
    """
    cfs = cf.latest_cashflow()
    fv = cfs.free_cash_flow if cfs else None
    if fv is None:
        return _na("Free Cash Flow", "₹cr", "missing free cash flow (no OCF/capex either)")
    return AnalyticResult("Free Cash Flow", round(fv / 1e7, 2), "₹cr", True, "high",
                          detail={"fcf_rupees": fv, "source": cf.provider_name})


def compute_all(cf: CompanyFundamentals, cagr_years: int = 5) -> Dict[str, AnalyticResult]:
    return {
        "revenue_cagr": revenue_cagr(cf, cagr_years),
        "eps_cagr":     eps_cagr(cf, cagr_years),
        "roe":          roe(cf),
        "debt_to_equity": debt_to_equity(cf),
        "roce":         roce(cf),
        "fcf":          free_cash_flow(cf),
    }

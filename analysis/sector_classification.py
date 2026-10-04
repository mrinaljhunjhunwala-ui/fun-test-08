"""analysis/sector_classification.py — single source of truth for sector-aware
metric applicability (Phase D1).

The platform analyses NSE equities with generic-equity rules that are WRONG for
financials (a bank's deposits are not "debt"; EV/EBITDA, ROCE and FCF are undefined for
lenders) and need a capex caveat for capital-intensive sectors. This module maps a raw
sector label (from `data.universe.get_sector`, or a Yahoo `info["sector"]`/industry, or a
company name hint) into a `SectorProfile` that says, per metric, whether it is
economically meaningful.

Every downstream consumer — Thesis Engine, Valuation Context, Portfolio Fit, future
analytics — imports `classify_sector` here. **No metric-applicability logic is hardcoded
anywhere else.**

NO new data, NO network — pure string classification.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Canonical groups
BANKS = "Banks"
NBFC = "NBFC"
INSURANCE = "Insurance"
FINANCIAL_SERVICES = "Financial Services"
IT_SERVICES = "IT Services"
CONSUMER = "Consumer"
HEALTHCARE = "Healthcare"
AUTO = "Auto"
CHEMICALS = "Chemicals"
CAPITAL_GOODS = "Capital Goods"
MANUFACTURING = "Manufacturing"
METALS = "Metals & Mining"
ENERGY_POWER = "Energy & Power"
INFRASTRUCTURE = "Infrastructure"
TELECOM = "Telecom"
OTHER = "Other"

_FINANCIAL_GROUPS = {BANKS, NBFC, INSURANCE, FINANCIAL_SERVICES}


@dataclass(frozen=True)
class SectorProfile:
    """Per-sector metric applicability. The booleans are the contract the rest of the
    platform reads — they encode *which metrics are economically meaningful*."""
    raw: Optional[str]
    group: str
    is_financial: bool
    is_capital_intensive: bool
    # metric applicability
    leverage_warning_applies: bool      # is a D/E leverage warning meaningful?
    ev_ebitda_meaningful: bool
    roce_meaningful: bool
    fcf_meaningful: bool
    fcf_capex_caveat: bool              # FCF meaningful but interpret with a capex caveat
    preferred_valuation: str           # how this sector is actually valued
    note: str                          # explanatory context (esp. for financials)

    def to_dict(self) -> dict:
        return {"raw": self.raw, "group": self.group, "is_financial": self.is_financial,
                "is_capital_intensive": self.is_capital_intensive,
                "leverage_warning_applies": self.leverage_warning_applies,
                "ev_ebitda_meaningful": self.ev_ebitda_meaningful,
                "roce_meaningful": self.roce_meaningful, "fcf_meaningful": self.fcf_meaningful,
                "fcf_capex_caveat": self.fcf_capex_caveat,
                "preferred_valuation": self.preferred_valuation, "note": self.note}


def _financial(group: str, raw, note: str, preferred: str) -> SectorProfile:
    """A financial: leverage/EV-EBITDA/ROCE/FCF are all NOT meaningful."""
    return SectorProfile(
        raw=raw, group=group, is_financial=True, is_capital_intensive=False,
        leverage_warning_applies=False, ev_ebitda_meaningful=False,
        roce_meaningful=False, fcf_meaningful=False, fcf_capex_caveat=False,
        preferred_valuation=preferred, note=note)


def _operating(group: str, raw, *, capital_intensive: bool, capex_caveat: bool,
               preferred: str = "P/E + EV/EBITDA") -> SectorProfile:
    """A non-financial operating business: industrial-style metrics all apply."""
    return SectorProfile(
        raw=raw, group=group, is_financial=False, is_capital_intensive=capital_intensive,
        leverage_warning_applies=True, ev_ebitda_meaningful=True,
        roce_meaningful=True, fcf_meaningful=True, fcf_capex_caveat=capex_caveat,
        preferred_valuation=preferred, note="")


# Notes (kept here so wording is consistent everywhere)
_NOTE_BANK = ("Banks fund operations with customer deposits, so debt/equity, EV/EBITDA, "
              "ROCE and free cash flow are not economically meaningful. Banks are assessed "
              "on P/B, ROE and asset quality (GNPA / NIM).")
_NOTE_NBFC = ("NBFCs are lenders — leverage is the business model, so D/E warnings, "
              "EV/EBITDA, ROCE and FCF do not apply. Assessed on P/B, ROE, NIM and asset "
              "quality.")
_NOTE_INSUR = ("Insurers are valued on embedded value (P/EV) and VNB margin, not P/E or "
               "EV/EBITDA; leverage and free cash flow do not apply.")
_NOTE_FIN = ("Financial-services firms (NBFCs, AMCs, exchanges, insurers) are not analysed "
             "on industrial-style leverage, EV/EBITDA, ROCE or FCF; assessed on P/B / ROE "
             "(or P/EV for insurers).")

# Exact-label routing (lower-cased). Covers the in-app get_sector taxonomy AND common
# Yahoo `info["sector"]` strings.
_EXACT = {
    # financials
    "banking": "bank", "bank": "bank", "banks": "bank",
    "finance": "fin", "financial services": "fin", "financials": "fin",
    "nbfc": "nbfc",
    "insurance": "insur",
    # non-financials
    "it": "it", "technology": "it", "software": "it", "infotech": "it",
    "information technology": "it",
    "fmcg": "consumer", "retail": "consumer", "consumer defensive": "consumer",
    "consumer cyclical": "consumer", "consumer staples": "consumer",
    "pharma": "health", "healthcare": "health", "health care": "health",
    "auto": "auto", "automobile": "auto",
    "chemicals": "chem", "basic materials": "chem",
    "capitalgoods": "capgoods", "capital goods": "capgoods", "industrials": "capgoods",
    "cement": "manuf",
    "metal": "metal", "metals": "metal", "mining": "metal",
    "energy": "energy", "utilities": "energy", "oil & gas": "energy",
    "realestate": "infra", "real estate": "infra", "realty": "infra",
    "telecom": "telecom", "communication services": "telecom",
    "conglomerate": "other", "other": "other",
}

# Keyword fallback (substring scan) — order matters; financials first.
_KEYWORDS = [
    ("insur", "insur"), ("life insurance", "insur"), ("assurance", "insur"),
    ("bank", "bank"),
    ("nbfc", "nbfc"), ("non banking", "nbfc"), ("non-banking", "nbfc"),
    ("housing finance", "nbfc"), ("credit", "nbfc"),
    ("financ", "fin"), ("capital market", "fin"), ("asset manage", "fin"),
    ("broker", "fin"), ("exchange", "fin"), ("amc", "fin"),
    # "hospitality" must precede "hospital" — the substring scan used to file
    # hotels as Healthcare (audit 2026-10-03).
    ("hospitality", "consumer"), ("hotel", "consumer"),
    ("pharma", "health"), ("hospital", "health"), ("diagnost", "health"),
    ("chemical", "chem"),
    ("capital goods", "capgoods"), ("engineering", "capgoods"), ("industrial", "capgoods"),
    ("infrastruct", "infra"), ("construction", "infra"), ("real estate", "infra"),
    ("realty", "infra"),
    ("power", "energy"), ("utilit", "energy"), ("oil", "energy"), ("gas", "energy"),
    ("metal", "metal"), ("mining", "metal"), ("steel", "metal"),
    ("cement", "manuf"),
    ("telecom", "telecom"), ("communication", "telecom"),
    ("software", "it"), ("technolog", "it"),
    ("fmcg", "consumer"), ("consumer", "consumer"), ("retail", "consumer"),
    ("food", "consumer"), ("beverage", "consumer"),
    ("auto", "auto"),
]


def _build(token: str, raw) -> SectorProfile:
    if token == "bank":
        return _financial(BANKS, raw, _NOTE_BANK, "P/B + ROE")
    if token == "nbfc":
        return _financial(NBFC, raw, _NOTE_NBFC, "P/B + ROE")
    if token == "insur":
        return _financial(INSURANCE, raw, _NOTE_INSUR, "P/EV (embedded value)")
    if token == "fin":
        return _financial(FINANCIAL_SERVICES, raw, _NOTE_FIN, "P/B + ROE")
    if token == "it":
        return _operating(IT_SERVICES, raw, capital_intensive=False, capex_caveat=False)
    if token == "consumer":
        return _operating(CONSUMER, raw, capital_intensive=False, capex_caveat=False)
    if token == "health":
        return _operating(HEALTHCARE, raw, capital_intensive=False, capex_caveat=False)
    if token == "auto":
        return _operating(AUTO, raw, capital_intensive=True, capex_caveat=False)
    if token == "chem":
        return _operating(CHEMICALS, raw, capital_intensive=True, capex_caveat=True)
    if token == "capgoods":
        return _operating(CAPITAL_GOODS, raw, capital_intensive=True, capex_caveat=True)
    if token == "manuf":
        return _operating(MANUFACTURING, raw, capital_intensive=True, capex_caveat=True)
    if token == "metal":
        return _operating(METALS, raw, capital_intensive=True, capex_caveat=True)
    if token == "energy":
        return _operating(ENERGY_POWER, raw, capital_intensive=True, capex_caveat=True)
    if token == "infra":
        return _operating(INFRASTRUCTURE, raw, capital_intensive=True, capex_caveat=True)
    if token == "telecom":
        return _operating(TELECOM, raw, capital_intensive=True, capex_caveat=True)
    return _operating(OTHER, raw, capital_intensive=False, capex_caveat=False)


def quality_ratio_keys(profile: Optional[SectorProfile]) -> tuple:
    """Which balance-sheet/return ratios are meaningful for a quality score.

    Financials (banks / NBFCs / insurers / fin-services): ROE + P/B only —
    ROCE and debt/equity are not economically meaningful for lenders.
    Operating businesses: ROE, plus ROCE / D-E when the profile says they apply.
    """
    if profile is not None and profile.is_financial:
        return ("roe", "pb")
    keys = ["roe"]
    if profile is None or profile.roce_meaningful:
        keys.append("roce")
    if profile is None or profile.leverage_warning_applies:
        keys.append("debt_to_equity")
    return tuple(keys)


def classify_sector(raw: Optional[str], name: Optional[str] = None) -> SectorProfile:
    """Map a raw sector label (and optional company name) to a SectorProfile.

    Unknown / None → a neutral OTHER profile (industrial-style metrics apply) so behaviour
    is unchanged for anything we cannot classify — never a silent financial mis-suppression.
    """
    # Name hint can disambiguate insurers inside a generic "Finance" bucket.
    nm = (name or "").lower()
    if any(k in nm for k in ("life insurance", "general insurance", "gic ", "assurance",
                             "prudential", "lombard", " life ")):
        return _build("insur", raw)

    s = (raw or "").strip().lower()
    if not s:
        return _operating(OTHER, raw, capital_intensive=False, capex_caveat=False)

    if s in _EXACT:
        return _build(_EXACT[s], raw)

    for kw, token in _KEYWORDS:
        if kw in s:
            return _build(token, raw)

    return _operating(OTHER, raw, capital_intensive=False, capex_caveat=False)

"""
analysis/final_verdict.py — the ONE answer.

Problem this solves
───────────────────
The app currently produces up to seven partial verdicts for a single ticker:

  1. Composite score       (analysis/score.py)              — trade now?
  2. TQS                   (analysis/trend_quality_score)   — trend healthy?
  3. Qualitative flags     (analysis/qualitative_flags)     — governance?
  4. Thesis verdict        (analysis/thesis/thesis_engine)  — story holds?
  5. Valuation posture     (analysis/fundamentals/          — overpaying?
                             valuation_decision)
  6. Fundamental quality   (research/fundamental_quality)   — good business?
  7. Portfolio fit         (analysis/thesis/portfolio_fit)  — fits book?

Each answers a different question, and users have been asked to synthesise
seven partial answers themselves. That was the "seven-scores problem"
flagged in the assessment. This module produces ONE coherent verdict that
composes them, without hiding what each subsystem said.

Design principle
────────────────
Not a weighted sum. A DECISION TREE. Quality and governance are GATES
(they can veto a BUY no matter how good the technical setup looks).
Valuation is a DAMPER (a "STRONG BUY" on an overpriced name gets
downgraded, not vetoed). Technical setup is what turns a "watch-worthy"
name into an "act today" call — it's the LAST filter, not the first.

This is the Buffett/Munger circle-of-competence structure: rule out on
quality first, price second, timing third. Any single-number aggregator
that ignores this ordering will happily flash STRONG BUY on a fraud with
great momentum, which is the exact failure mode we want to prevent.

Inputs are all OPTIONAL. Callers pass whatever they've computed; missing
inputs are treated as unknown, not as bad. The verdict records confidence
based on how many gates were actually evaluable — a verdict with 5/5 gates
firm is HIGH confidence; with only 2/5 available is LOW.

Nothing here fetches data. Callers (dashboard pages / research scripts)
are the appropriate place to decide what to compute.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from analysis.action_labels import action_display_label

_log = logging.getLogger("analysis.final_verdict")


# ─────────────────────────────────────────────────────────────────────────────
# Verdict vocabulary
# ─────────────────────────────────────────────────────────────────────────────

# One label a user actually acts on. Ordered by strength.
VERDICTS = ["AVOID", "HOLD", "WATCH", "BUY", "STRONG BUY"]

CONFIDENCE = ("low", "medium", "high")

# ── Horizons (FIX FV-HORIZON) ─────────────────────────────────────────────────
# "Should I buy this?" is a different question depending on holding period.
#   * SHORT  (days–weeks)   — swing trade. Technical setup dominates; a bad
#                             valuation doesn't kill a 5-day trade.
#   * MEDIUM (weeks–months) — positional. Technical + trend + regime matter.
#     (Same behaviour as the original single-lens FinalVerdict — this is the
#     default so existing callers see no change.)
#   * LONG   (6 months +)   — investment. Quality + valuation + thesis
#                             dominate; a technical EXIT signal today is
#                             mostly noise vs the multi-year hold thesis.
#
# One aggregator, three lenses. Same subsystem inputs get read through
# horizon-specific gate weights. Every horizon still respects the quality /
# thesis / (short & medium) technical VETO — you don't buy a fraud on any
# horizon; you don't enter today into a technical EXIT signal for a trade
# your horizon says is a swing.
HORIZONS = ("short", "medium", "long")


# Gate-effect multipliers per horizon. A veto is always a veto (0 conviction);
# damp/amplify magnitudes flex.  Numbers chosen to be simple, defensible, and
# reversible — no fine-tuned coefficients, no over-fitting to a specific run.
_HORIZON_WEIGHTS: Dict[str, Dict[str, float]] = {
    "short": {
        "quality_damp":     0.5,   # amber flag hurts less on a 5-day trade
        "valuation_damp":   0.25,  # DEMANDING valuation barely matters
        "valuation_amp":    0.25,
        "thesis_damp":      0.75,  # Negative thesis still uncomfortable
        "thesis_amp":       0.75,
        "trend_damp":       1.5,   # weak trend really hurts a swing trade
        "trend_amp":        1.5,
        "technical_veto":   1.0,   # EXIT today = don't enter today
        "technical_amp":    1.2,   # BUY today = the whole point of a swing
    },
    "medium": {   # default — matches the original single-lens behaviour
        "quality_damp":     1.0,
        "valuation_damp":   1.0,
        "valuation_amp":    1.0,
        "thesis_damp":      1.0,
        "thesis_amp":       1.0,
        "trend_damp":       1.0,
        "trend_amp":        1.0,
        "technical_veto":   1.0,
        "technical_amp":    1.0,
    },
    "long": {
        "quality_damp":     1.5,   # amber flag matters more when holding years
        "valuation_damp":   1.75,  # DEMANDING valuation is a genuine problem
        "valuation_amp":    1.75,  # so is SUPPORTED (much more so long-term)
        "thesis_damp":      1.5,
        "thesis_amp":       1.5,
        "trend_damp":       0.25,  # weak trend today doesn't kill a 5y hold
        "trend_amp":        0.25,
        "technical_veto":   0.0,   # NOT a veto long-term — see docstring
        "technical_amp":    0.25,  # BUY today isn't why you'd buy for 5y
    },
}

# Base-conviction weighting per horizon. The composite score dominates on
# short horizons (it IS the trading signal); on longer horizons the base
# blends in TQS and fundamental quality with progressively higher weight.
_HORIZON_BASE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "short":  {"composite": 1.0,  "tqs": 0.0,  "quality": 0.0},
    "medium": {"composite": 0.7,  "tqs": 0.3,  "quality": 0.0},
    "long":   {"composite": 0.3,  "tqs": 0.3,  "quality": 0.4},
}


@dataclass
class GateEvaluation:
    """One gate's outcome. name is 'quality' | 'valuation' | ..."""
    name:    str
    passed:  Optional[bool]           # None = insufficient data
    message: str                      # one-line human-readable
    effect:  str                      # "veto" | "damp" | "amplify" | "none"


@dataclass
class FinalVerdict:
    """The single answer. Everything else is drilldown."""
    verdict:          str                                   # one of VERDICTS
    confidence:       str                                   # low | medium | high
    conviction:       int                                   # 0-100 aggregated conviction score
    primary_reason:   str                                   # single sentence
    horizon:          str = "medium"                        # short | medium | long
    gates:            List[GateEvaluation] = field(default_factory=list)
    limiting_gate:    Optional[str] = None                  # gate that most reduced conviction
    subsystem_labels: Dict[str, str] = field(default_factory=dict)  # {"composite": "BUY", ...}

    def as_dict(self) -> Dict:
        return {
            "verdict":          self.verdict,
            "confidence":       self.confidence,
            "conviction":       self.conviction,
            "primary_reason":   self.primary_reason,
            "horizon":          self.horizon,
            "limiting_gate":    self.limiting_gate,
            "gates": [
                {"name": g.name, "passed": g.passed,
                 "message": g.message, "effect": g.effect}
                for g in self.gates
            ],
            "subsystem_labels": self.subsystem_labels,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Individual gate evaluators
# ─────────────────────────────────────────────────────────────────────────────

def _gate_quality(quality_score: Optional[float],
                  quality_flags: Optional[Dict]) -> GateEvaluation:
    """
    Governance + fundamental-quality gate.

    A RED qualitative flag (pledge breach, regulatory action, audit qualification)
    is a HARD VETO — no BUY signal survives one. An AMBER flag caps conviction
    but doesn't veto. Fundamental-quality score < 30 is a soft veto.
    """
    if quality_flags and quality_flags.get("severity") == "red":
        top = quality_flags.get("top_flag", "governance red flag")
        return GateEvaluation(
            name="quality", passed=False, effect="veto",
            message=f"Red governance flag: {top}. BUY suppressed.")

    if quality_score is not None and quality_score < 30:
        return GateEvaluation(
            name="quality", passed=False, effect="veto",
            message=f"Fundamental quality score {quality_score:.0f}/100 below "
                    f"the 30-point floor. BUY suppressed.")

    if quality_flags and quality_flags.get("severity") == "amber":
        top = quality_flags.get("top_flag", "flag")
        return GateEvaluation(
            name="quality", passed=True, effect="damp",
            message=f"Amber flag ({top}) — proceed with reduced size.")

    if quality_score is None and not quality_flags:
        return GateEvaluation(
            name="quality", passed=None, effect="none",
            message="Quality data unavailable.")

    return GateEvaluation(
        name="quality", passed=True, effect="none",
        message=f"Quality passes"
                + (f" (score {quality_score:.0f}/100)" if quality_score else ""))


def _gate_valuation(posture: Optional[str],
                    guard: Optional[str] = None,
                    guard_reason: Optional[str] = None) -> GateEvaluation:
    """
    Valuation posture gate — DAMPER not veto.

    "DEMANDING_*" postures reduce conviction; they never suppress a signal
    outright, because a demanding valuation on a real trending winner can
    still be a valid trade (the DAMP is your position-sizing discipline).

    INSUFFICIENT_EVIDENCE is not a bug — the valuation engine deliberately
    abstains on cyclical peaks/troughs, negative earnings, and newly-listed
    stocks (see valuation_decision._refuse). When we know WHY it abstained
    (guard), surface that so the user reads "cyclical trough — multiples not
    meaningful" instead of the ambiguous "insufficient evidence".
    """
    if not posture:
        return GateEvaluation(
            name="valuation", passed=None, effect="none",
            message="Valuation data unavailable.")

    if posture == "INSUFFICIENT_EVIDENCE":
        # Human-readable label per known abstention reason. These match the
        # `guard` strings emitted by valuation_decision._refuse() at the
        # top of assess().
        _guard_labels = {
            "H1-negative-earnings": "Negative or zero earnings — no earnings multiple to assess.",
            "H2-negative-equity":   "Negative shareholders' equity — book multiples not meaningful.",
            "H3-missing-metric":    "Core valuation metric (P/E or P/B + ROE) unavailable.",
            "H4-insurance":         "Insurer — valued on embedded value (P/EV), not exposed here.",
            "H5-newly-listed":      "Under 2 yrs of earnings history — too new to assess.",
            "H6-implausible":       "P/E implausibly high — earnings likely distorted (one-off charge / trough).",
            "G1-cyclical-peak":     "Possible cyclical peak — multiples likely to compress; abstain.",
            "TR-cyclical-trough":   "Possible cyclical trough — depressed earnings distort P/E; abstain.",
            "growth-lens-off":      "Growth lens off — PEG unusable; multiples require judgement.",
        }
        pretty = _guard_labels.get(guard or "")
        if pretty:
            msg = f"Valuation intentionally skipped — {pretty}"
        elif guard_reason:
            msg = f"Valuation intentionally skipped — {guard_reason}"
        else:
            msg = ("Valuation intentionally skipped — profile not amenable to a "
                   "multiple-based read (cyclical / newly-listed / negative earnings).")
        return GateEvaluation(
            name="valuation", passed=None, effect="none", message=msg)

    if posture.startswith("DEMANDING"):
        return GateEvaluation(
            name="valuation", passed=True, effect="damp",
            message=f"Valuation posture: {posture.replace('_', ' ').lower()}. "
                    f"Stretched valuations have historically meant more downside on pullbacks.")

    if posture.startswith("SUPPORTED"):
        return GateEvaluation(
            name="valuation", passed=True, effect="amplify",
            message=f"Valuation posture: {posture.replace('_', ' ').lower()}.")

    # REASONABLE and everything else
    return GateEvaluation(
        name="valuation", passed=True, effect="none",
        message=f"Valuation posture: {posture.replace('_', ' ').lower()}.")


def _gate_thesis(verdict: Optional[str],
                 verdict_score: Optional[int]) -> GateEvaluation:
    """
    Thesis-engine gate.

    "Strong Negative" (score -2) is a veto — the story explicitly does not
    hold. "Negative" (-1) is a damper. "Positive" and "Strong Positive"
    are amplifiers. Neutral is a no-op.
    """
    if not verdict:
        return GateEvaluation(
            name="thesis", passed=None, effect="none",
            message="Thesis engine not run.")

    if verdict_score is not None and verdict_score <= -2:
        return GateEvaluation(
            name="thesis", passed=False, effect="veto",
            message=f"Thesis: {verdict}. BUY suppressed.")

    if verdict_score is not None and verdict_score == -1:
        return GateEvaluation(
            name="thesis", passed=True, effect="damp",
            message=f"Thesis: {verdict}. Reduce size.")

    if verdict_score is not None and verdict_score >= 1:
        return GateEvaluation(
            name="thesis", passed=True, effect="amplify",
            message=f"Thesis: {verdict}.")

    return GateEvaluation(
        name="thesis", passed=True, effect="none",
        message=f"Thesis: {verdict or 'Neutral'}.")


def _gate_trend_quality(tqs: Optional[float]) -> GateEvaluation:
    """
    Trend-quality (TQS) gate.

    TQS < 25 damps — the trend is weak enough that any BUY here is
    counter-trend by construction. Not a veto because oversold-bounce trades
    are legitimate; but conviction should reflect the trend backdrop.
    TQS > 60 amplifies (strong, persistent trend).
    """
    if tqs is None:
        return GateEvaluation(
            name="trend", passed=None, effect="none",
            message="TQS unavailable.")

    if tqs < 25:
        return GateEvaluation(
            name="trend", passed=True, effect="damp",
            message=f"TQS {tqs:.0f}/90 — trend weak; any long reading here runs against the trend.")

    if tqs > 60:
        return GateEvaluation(
            name="trend", passed=True, effect="amplify",
            message=f"TQS {tqs:.0f}/90 — trend strong and persistent.")

    return GateEvaluation(
        name="trend", passed=True, effect="none",
        message=f"TQS {tqs:.0f}/90 — trend health neutral.")


def _gate_technical(composite_score: Optional[float],
                    composite_action: Optional[str]) -> GateEvaluation:
    """
    Technical-setup gate — the "act now?" question.

    The composite score's action label maps here without translation. This
    is the DRIVER of the final verdict; every other gate modifies its
    conviction. When no composite is available the entire final-verdict
    exercise defaults to HOLD (we can't decide without SOMETHING numeric
    to anchor).
    """
    if composite_score is None:
        return GateEvaluation(
            name="technical", passed=None, effect="none",
            message="No composite technical read available.")

    act = (composite_action or "").upper()
    if act in ("STRONG BUY", "BUY"):
        return GateEvaluation(
            name="technical", passed=True, effect="amplify",
            message=f"Technical setup: {action_display_label(act)} (composite score "
                    f"{composite_score:.0f}/90).")

    if act in ("EXIT", "CAUTION"):
        return GateEvaluation(
            name="technical", passed=False, effect="veto",
            message=f"Technical setup: {action_display_label(act)} (composite score "
                    f"{composite_score:.0f}/90). The trend is not intact at this level.")

    return GateEvaluation(
        name="technical", passed=True, effect="none",
        message=f"Technical setup: {action_display_label(act) if act else 'Neutral'} "
                f"(composite score {composite_score:.0f}/90).")


# ─────────────────────────────────────────────────────────────────────────────
# The aggregator
# ─────────────────────────────────────────────────────────────────────────────

def _apply_effects(base_conviction: int,
                   gates: List[GateEvaluation],
                   horizon: str = "medium",
                   ) -> Tuple[int, Optional[str]]:
    """
    Turn gate effects into a conviction score adjustment, scaled by horizon.

    The BASE magnitudes are: damp = -20, amplify = +10. Horizon multipliers
    (see _HORIZON_WEIGHTS) can up- or down-scale each per gate. A veto is
    only a veto if the horizon's veto weight for that gate is > 0 (the LONG
    horizon has technical_veto=0, so a technical EXIT signal today does NOT
    veto a long-term buy verdict — it only lowers conviction).

    Bounded 0..100. `limiting_gate` is the gate that most reduced conviction.
    """
    w = _HORIZON_WEIGHTS.get(horizon, _HORIZON_WEIGHTS["medium"])
    _damp_key = {
        "quality":   "quality_damp",
        "valuation": "valuation_damp",
        "thesis":    "thesis_damp",
        "trend":     "trend_damp",
    }
    _amp_key = {
        "valuation": "valuation_amp",
        "thesis":    "thesis_amp",
        "trend":     "trend_amp",
        "technical": "technical_amp",
    }

    convict = base_conviction
    veto_gate: Optional[str] = None
    biggest_damp = 0.0
    biggest_damp_gate: Optional[str] = None

    for g in gates:
        if g.effect == "veto" and g.passed is False:
            # Technical veto is horizon-scaled — some horizons don't veto on it
            if g.name == "technical":
                veto_scale = w.get("technical_veto", 1.0)
                if veto_scale > 0:
                    veto_gate = g.name
                    convict = 0
                else:
                    # Long horizon: treat "don't enter today" as a big DAMP
                    # rather than a veto, since horizon >> today.
                    convict -= int(round(30 * (1 - veto_scale)))
                    if 30 > biggest_damp:
                        biggest_damp = 30
                        biggest_damp_gate = g.name
            else:
                veto_gate = g.name
                convict = 0
        elif g.effect == "damp":
            scale = w.get(_damp_key.get(g.name, ""), 1.0)
            hit = 20 * scale
            convict -= int(round(hit))
            if hit > biggest_damp:
                biggest_damp = hit
                biggest_damp_gate = g.name
        elif g.effect == "amplify":
            scale = w.get(_amp_key.get(g.name, ""), 1.0)
            convict += int(round(10 * scale))

    limiting = veto_gate or biggest_damp_gate
    return max(0, min(100, convict)), limiting


def _label_from_conviction(conviction: int,
                           tech_gate: GateEvaluation,
                           horizon: str = "medium") -> str:
    """
    Map final conviction number back to a verdict label.

    The technical-veto short-circuit is horizon-aware: on the LONG horizon
    a technical EXIT signal today does NOT force AVOID (it's already been
    damped in _apply_effects). On SHORT/MEDIUM it does.
    """
    w = _HORIZON_WEIGHTS.get(horizon, _HORIZON_WEIGHTS["medium"])
    if tech_gate.effect == "veto" and w.get("technical_veto", 1.0) > 0:
        return "AVOID"
    if conviction >= 85:  return "STRONG BUY"
    if conviction >= 65:  return "BUY"
    if conviction >= 45:  return "WATCH"
    if conviction >= 25:  return "HOLD"
    return "AVOID"


def _confidence_from_gate_coverage(gates: List[GateEvaluation]) -> str:
    """
    High = at least 4/5 gates yielded a firm passed/failed. Medium = 3/5.
    Low = fewer. Callers can't act with the same confidence on a partial
    read as on a full one.
    """
    firm = sum(1 for g in gates if g.passed is not None)
    if firm >= 4: return "high"
    if firm == 3: return "medium"
    return "low"


def _base_conviction(horizon: str,
                     composite_score: Optional[float],
                     tqs:             Optional[float],
                     quality_score:   Optional[float]) -> int:
    """
    Horizon-weighted base conviction (0-100).

    Composite score dominates on short horizons (it IS the trading signal).
    On long horizons the base blends in TQS and fundamental quality, so a
    stock with a bad short-term setup but excellent quality can still
    surface as a valid long-term BUY.
    """
    w = _HORIZON_BASE_WEIGHTS.get(horizon, _HORIZON_BASE_WEIGHTS["medium"])
    total_weight = 0.0
    total_score  = 0.0
    if composite_score is not None:
        total_weight += w["composite"]
        total_score  += w["composite"] * (composite_score * (100 / 90))
    if tqs is not None:
        total_weight += w["tqs"]
        total_score  += w["tqs"] * (tqs * (100 / 90))
    if quality_score is not None:
        total_weight += w["quality"]
        total_score  += w["quality"] * quality_score
    if total_weight <= 0:
        return 50   # neutral prior when nothing numeric available
    return int(round(min(100, total_score / total_weight)))


def combine(*,
            composite_score:      Optional[float] = None,
            composite_action:     Optional[str]   = None,
            tqs:                  Optional[float] = None,
            quality_score:        Optional[float] = None,
            quality_flags:        Optional[Dict]  = None,
            valuation_posture:    Optional[str]   = None,
            valuation_guard:      Optional[str]   = None,
            valuation_guard_reason: Optional[str] = None,
            thesis_verdict:       Optional[str]   = None,
            thesis_score:         Optional[int]   = None,
            horizon:              str             = "medium",
            ) -> FinalVerdict:
    """
    Combine every subsystem's output into ONE verdict.

    Every argument is optional; a caller that only has half the pieces
    still gets a defensible answer, just with lower confidence.

    `horizon` — one of "short" | "medium" | "long" (default "medium",
    which preserves the original single-lens behaviour). Different
    horizons re-weight the SAME subsystem outputs — a bad valuation is
    a big damp long-term but nearly zero short-term; a technical EXIT
    is a veto short-term but only a damp long-term. See _HORIZON_WEIGHTS
    and _HORIZON_BASE_WEIGHTS for the exact rules.
    """
    horizon = horizon if horizon in HORIZONS else "medium"

    tech = _gate_technical(composite_score, composite_action)
    qual = _gate_quality(quality_score, quality_flags)
    valu = _gate_valuation(valuation_posture,
                            guard=valuation_guard,
                            guard_reason=valuation_guard_reason)
    thes = _gate_thesis(thesis_verdict, thesis_score)
    trnd = _gate_trend_quality(tqs)
    gates = [tech, qual, valu, thes, trnd]

    base = _base_conviction(horizon, composite_score, tqs, quality_score)

    conviction, limiting = _apply_effects(base, gates, horizon=horizon)
    verdict = _label_from_conviction(conviction, tech, horizon=horizon)
    confidence = _confidence_from_gate_coverage(gates)

    # Primary reason: the strongest signal driving the verdict
    if tech.effect == "veto":
        primary = tech.message
    elif limiting:
        limiting_g = next((g for g in gates if g.name == limiting), None)
        if limiting_g:
            primary = limiting_g.message
        else:
            primary = tech.message
    else:
        primary = tech.message

    labels: Dict[str, str] = {}
    if composite_action:      labels["composite"] = composite_action
    if tqs is not None:       labels["tqs"] = f"{tqs:.0f}/90"
    if valuation_posture:     labels["valuation"] = valuation_posture
    if thesis_verdict:        labels["thesis"] = thesis_verdict
    if quality_score is not None:
        labels["quality"] = f"{quality_score:.0f}/100"
    if quality_flags and quality_flags.get("severity"):
        labels["flags"] = str(quality_flags.get("severity"))

    return FinalVerdict(
        verdict=verdict, confidence=confidence,
        conviction=conviction, primary_reason=primary,
        horizon=horizon,
        gates=gates, limiting_gate=limiting,
        subsystem_labels=labels,
    )

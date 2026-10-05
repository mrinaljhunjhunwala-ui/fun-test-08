"""
analysis/score.py
Composite Stock Score — 0 to 100 — with grade, action verdict,
entry/SL/TP levels, and a plain-English narrative for non-traders.

Score breakdown (90 pts max):
    Technical   40 pts  — RSI, MACD, SMA stack, ADX
    Momentum    25 pts  — 5d / 20d / 60d returns
    Volume      15 pts  — Volume ratio, OBV trend
    Sentiment   10 pts  — India VIX regime + sector rank

Candlestick patterns are detected and shown in the narrative but are NOT
scored. The 5-year variant study (docs/RESEARCH_SCORE_VARIANTS.md, 40,663
observations) found the 10-pt pattern component contributed zero-to-negative
ranking power in every market regime. See docs/PATTERN_REMOVAL_MIGRATION.md.

Grades:   A+ (≥88) | A (≥75) | B (≥62) | C (≥48) | D (≥32) | F (<32)
Actions:  STRONG BUY | BUY | WATCHLIST | HOLD | CAUTION | EXIT

Fixes applied vs previous version:
  - sys.path.insert moved to module level (was inside score_stock — ran on every call)
  - All heavy imports moved to module level with _DEPS_LOADED guard
  - _score_pattern renamed _detect_patterns: returns Dict only (no phantom numeric score)
  - CompositeScore.pattern_score removed; patterns_detected: List[str] added
  - resolve_ticker now catches Exception (was ValueError only — other errors crashed page)
  - Score cap corrected to 90 (was 100 — unreachable, misleading)
  - Sector rank thresholds now derived from n_sectors (were hardcoded 5/10)
  - Momentum fallback now sets is_fallback=True flag so narrative can note it
  - Windows UTF-8 fix moved out of module body into _setup_encoding() called once
"""

from __future__ import annotations

import os
import sys
import logging
import warnings
import math
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta

# FIX WARN1 — narrowed from a blanket `filterwarnings("ignore")` so numpy's
# RuntimeWarnings (invalid value / divide by zero / all-NaN slice) stay visible.
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)

_log = logging.getLogger("analysis.score")

# ── Ensure project root is on sys.path (one-time, at import — not per call) ──
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── Lazy-load heavy project deps once at module level ─────────────────────────
# Keeps score_stock() clean and avoids repeated import overhead on screener scans.
_DEPS_LOADED = False
_fetch_single          = None
_get_sector            = None
_resolve_ticker        = None
_list_sectors          = None
_add_all_indicators    = None
_get_india_vix_regime  = None

# FIX LAZY1 — score_stock() previously called add_all_indicators() with no
# argument, computing all 14 indicator groups (82 columns) even though this
# function only ever reads columns from 6 of them. Do NOT add groups here
# speculatively — if score_stock() is changed to read a new indicator column,
# add that column's group here at the same time, or it will KeyError at the
# dropna/read site.
#
# FIX SCORE-PAT — "patterns" and "divergence" were MISSING from this tuple even
# though score_dataframe() calls _detect_patterns(), which reads Pat_Doji,
# Pat_Hammer, Pat_ShootingStar, Pat_BullMarubozu, Pat_BearMarubozu,
# Pat_BullEngulfing, Pat_BearEngulfing, Pat_MorningStar, Pat_EveningStar,
# RSI_Bull_Div and RSI_Bear_Div. Because _detect_patterns() guards every read
# with `if col in recent.columns` / `cur.get(col, 0)`, the omission did not
# raise — it silently returned an EMPTY pattern list for every stock scored
# through score_stock(), which is the entry point behind Top Picks, the
# screener, Analyze Stock, Watchlist and Quality Watch. Downstream that meant:
#   * CompositeScore.patterns_detected was always []
#   * the headline never got its "(BullEngulfing pattern)" suffix
#   * _build_narrative()'s sentence 4 could never take the pattern branch, so
#     the "volume × pattern confirmation" line and the bearish-pattern warning
#     were dead code for the main path
# i.e. a documented, user-facing feature was switched off by an optimisation
# that only audited the *numeric* score's column reads and missed the narrative
# layer's. The tests didn't catch it because test_smoke_score_indicators.py only
# asserts `isinstance(cs.patterns_detected, list)` — [] satisfies that.
#
# Both groups are cheap enough to restore: "patterns" is fully vectorised, and
# "divergence" is now vectorised too (FIX IND5 in utils/indicators.py replaced
# its per-bar Python loop with sliding_window_view), so this no longer
# reintroduces the per-ticker cost that motivated FIX LAZY1.
_SCORE_INDICATOR_GROUPS = (
    "ma", "rsi", "macd", "adx", "volume", "atr", "patterns", "divergence",
)


def _num(row: "pd.Series", key: str, default: float) -> float:
    """
    Read a numeric field from an indicator row, falling back to `default` when
    the value is missing OR NaN.

    FIX SCORE-NAN — the scorers used `float(cur.get("SMA_200", price * 0.80))`.
    `Series.get(key, default)` only returns the default when the KEY IS ABSENT;
    when the column exists but the value is NaN — the normal case during an
    indicator's warm-up — it returns NaN, and `float(nan)` propagates. Every
    subsequent comparison against NaN is False under IEEE-754, so the intended
    fallback never applied and the value silently fell through to the LAST
    branch of each ladder:
      * _score_technical()'s SMA stack awarded 0.0 / 10 whenever SMA_200 was
        NaN (a stock with under 200 bars of history — recent listings, and any
        caller passing a shorter period to score_dataframe()), scoring it
        identically to a stock genuinely trading below its 200-day average.
      * the same pattern applied to RSI, ADX, MACD and Volume_Ratio.
    Reading through this helper makes the documented default actually take
    effect, so "unknown" no longer silently reads as "bearish".
    """
    val = row.get(key, default)
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) else f


# FIX RS-CACHE (2026-09-04) — process-level cache of the Nifty benchmark df
# used by the Rec 1 RS enrichment. Keyed on `period` string because callers
# pass different windows ("2y" default, "1y" from peer scans). Sentinel
# _MISS distinguishes "not tried yet" from "tried and failed" so a failed
# fetch on ticker 1 doesn't get retried for tickers 2..N (the actual bug
# 02_command_centre's 120s smoke timeout on 3.11 was pointing at).
#
# FIX CACHE-TTL (2026-09-24) — the cache used to live for the whole process
# (Streamlit Cloud processes run for days), so a Nifty series fetched on
# Monday was still used on Thursday, and a single failed fetch disabled RS
# for the life of the process. Entries are now (fetched_at, value):
#   * success: refreshed after _BENCH_TTL_S (1h), or earlier when the cached
#     series' last bar is older than the expected last NSE session (checked
#     at most every _BENCH_NEG_TTL_S so a holiday doesn't cause a refetch
#     storm);
#   * failure: negative-cached for only _BENCH_NEG_TTL_S (5 min) — still
#     enough to stop tickers 2..N of one scan re-walking every provider.
_BENCH_MISS = object()
_BENCH_CACHE: Dict[str, object] = {}
_BENCH_TTL_S = 60 * 60
_BENCH_NEG_TTL_S = 5 * 60


def _now() -> float:
    """Monotonic-ish wall clock; indirection so tests can freeze time."""
    import time
    return time.time()


def _expected_last_session() -> pd.Timestamp:
    """Most recent weekday strictly before today (a conservative 'the feed
    should have at least this bar' date; ignores NSE holidays, which the
    re-check throttle tolerates)."""
    return (pd.Timestamp.today().normalize() - pd.offsets.BDay(1)).normalize()


def _bench_is_stale(b) -> bool:
    try:
        last = pd.Timestamp(b.index.max())
        if last.tzinfo is not None:
            last = last.tz_localize(None)
        return last.normalize() < _expected_last_session()
    except Exception:
        return False


# FIX FLOWS-CACHE (2026-09-04) — universe-level flows cached per process.
# FIX CACHE-TTL (2026-09-24) — entries are (fetched_at, value) with a TTL;
# failures (None) use the short negative TTL.
_FLOWS_CACHE: Dict[str, object] = {}
_FLOWS_TTL_S = 60 * 60
_FLOWS_MAX_AGE_DAYS = 9          # 5 sessions + a weekend + a holiday or two


def _get_flows_cached() -> Optional[Dict]:
    ent = _FLOWS_CACHE.get("flows")
    if ent is not None:
        ts, val = ent  # type: ignore[misc]
        ttl = _FLOWS_TTL_S if val is not None else _BENCH_NEG_TTL_S
        if _now() - ts <= ttl:
            return val
    try:
        from analysis.fii_dii import load_history as _fd_load
        _fd = _fd_load(days=5)
        if _fd is not None and not _fd.empty:
            # FIX FLOWS-STALE (2026-10-02): the table has holes (NSE only serves
            # the latest day, missed nightly runs aren't backfilled), so "last 5
            # rows" could silently reach weeks back. Keep rows within
            # _FLOWS_MAX_AGE_DAYS calendar days of the newest market session.
            _cut = (pd.Timestamp(_expected_last_session())
                    - pd.Timedelta(days=_FLOWS_MAX_AGE_DAYS))
            _fd = _fd[pd.to_datetime(_fd["date"], errors="coerce") >= _cut]
        if _fd is not None and not _fd.empty and len(_fd) >= 3:
            v: Optional[Dict] = {
                "fii_5d": float(_fd["fii_net"].fillna(0).sum()),
                "dii_5d": float(_fd["dii_net"].fillna(0).sum()),
                "n_days": int(len(_fd)),
            }
        else:
            v = None
    except Exception as e:
        _log.debug("FII/DII flows cache miss: %s: %s", type(e).__name__, e)
        v = None
    _FLOWS_CACHE["flows"] = (_now(), v)
    return v


def _get_bench_cached(period: str):
    """Return a cached Nifty DataFrame for `period`, or None on failure.
    See FIX CACHE-TTL above for expiry rules (1h TTL, date staleness,
    5-min negative cache)."""
    ent = _BENCH_CACHE.get(period)
    if ent is not None:
        ts, v = ent  # type: ignore[misc]
        age = _now() - ts
        if v is _BENCH_MISS:
            if age <= _BENCH_NEG_TTL_S:
                return None
        elif age <= _BENCH_TTL_S and not (age > _BENCH_NEG_TTL_S and _bench_is_stale(v)):
            return v
    try:
        b = _fetch_single("^NSEI", period=period)  # type: ignore[misc]
        if b is None or (hasattr(b, "empty") and b.empty):
            _BENCH_CACHE[period] = (_now(), _BENCH_MISS)
            return None
        _BENCH_CACHE[period] = (_now(), b)
        return b
    except Exception as e:
        _log.debug("bench cache: ^NSEI %s fetch failed, negative-caching: %s: %s",
                   period, type(e).__name__, e)
        _BENCH_CACHE[period] = (_now(), _BENCH_MISS)
        return None


def _load_deps() -> bool:
    global _DEPS_LOADED, _fetch_single, _get_sector, _resolve_ticker
    global _list_sectors, _add_all_indicators, _get_india_vix_regime
    if _DEPS_LOADED:
        return True
    try:
        from data.fetcher   import fetch_single
        from data.universe  import get_sector, resolve_ticker, list_sectors
        from utils.indicators import add_all_indicators
        _fetch_single       = fetch_single
        _get_sector         = get_sector
        _resolve_ticker     = resolve_ticker
        _list_sectors       = list_sectors
        _add_all_indicators = add_all_indicators
        try:
            from utils.vix import get_india_vix_regime
            _get_india_vix_regime = get_india_vix_regime
        except Exception as e:
            # Optional dependency — VIX regime sentiment falls back to "normal"
            # when unavailable (see score_stock), but log so a real import
            # break in utils.vix isn't invisible.
            _log.debug("utils.vix unavailable, VIX regime sentiment disabled: %s: %s",
                       type(e).__name__, e)
        _DEPS_LOADED = True
        return True
    except Exception as e:
        _log.warning("analysis.score core dependencies failed to load: %s: %s",
                     type(e).__name__, e)
        return False


def _setup_encoding() -> None:
    """Set UTF-8 output on Windows — called once at startup, not at import."""
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except AttributeError as e:
            # Older Python / non-standard stream wrappers may lack reconfigure();
            # harmless, but log so it's visible during diagnosis.
            _log.debug("stdout/stderr reconfigure unavailable: %s", e)
        os.environ.setdefault("PYTHONUTF8", "1")


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CompositeScore:
    ticker:             str
    price:              float
    score:              float        # 0-90 baseline; 0-100 when Positioning pillar active (F&O + NSE_USE_POSITIONING_PILLAR flag + real positioning data). See design 6b in docs/COMPOSITE_SCORE_SHAPE_REVIEW.md.
    grade:              str          # A+ … F
    action:             str          # STRONG BUY … EXIT
    technical_score:    float        # /40
    momentum_score:     float        # /25
    volume_score:       float        # /15
    sentiment_score:    float        # /10
    entry:              float
    stop_loss:          float
    target:             float
    risk_reward:        float
    headline:           str
    narrative:          str
    sector:             str
    vix_regime:         str
    sector_rank:        Optional[int]  # None = sector not in the current ranking (scored neutral)
    patterns_detected:  List[str]    = field(default_factory=list)   # informational only
    momentum_fallback:  bool         = False   # True when < 25 bars of history
    horizon:            str          = ""      # FIX HZ1: e.g. "Swing (3–10 trading days)"
    valid_until:        str          = ""      # FIX HZ1: ISO date — pick considered stale after this
    rsi:                float        = 50.0    # FIX WL1: raw RSI(14), already computed — just unsurfaced
    return_1d:          float        = 0.0     # FIX WL1: 1-day % change, already available from df
    rs_score:           Optional[float] = None # FIX RS1: RS_Score vs Nifty (0-100 percentile), None when benchmark unavailable
    positioning_score:  Optional[float] = None # FIX POS1: Positioning pillar (0-10, F&O + flag ON); None otherwise
    is_fno:             bool            = False # FIX POS1: True when the ticker is F&O-eligible per data.fno_universe
    technical_raw:      Optional[float] = None  # FIX BEAR-NEUTRAL: pre-neutralisation technical pts (descriptive uses: thesis, ledger tags)
    overlay_score:      Optional[int]   = None  # FIX OVERLAY1 (Task 3.3): TQS x valuation sidecar, 0-100. NEVER blended into .score.
    timestamp:          str          = field(default_factory=lambda: datetime.now().isoformat())

    def as_dict(self) -> Dict:
        return {
            "ticker": self.ticker, "price": self.price,
            "score": self.score, "grade": self.grade, "action": self.action,
            "technical": self.technical_score, "momentum": self.momentum_score,
            "volume": self.volume_score, "sentiment": self.sentiment_score,
            "patterns": self.patterns_detected,
            "entry": self.entry, "sl": self.stop_loss, "tp": self.target,
            "rr": self.risk_reward, "headline": self.headline,
            "sector": self.sector, "vix_regime": self.vix_regime,
            "momentum_fallback": self.momentum_fallback,
            "horizon": self.horizon, "valid_until": self.valid_until,
            "rsi": self.rsi, "return_1d": self.return_1d,
            "rs_score": self.rs_score,
            "positioning": self.positioning_score,
            "is_fno": self.is_fno,
            "overlay_score": self.overlay_score,
            "technical_raw": self.technical_raw,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Task 3.3 - TQS x valuation sidecar overlay score
#
# Guardrail-safe home for the shape-review doc's "Quality & Valuation" idea:
# the overlay is a SIDECAR (0-100), rendered next to the composite score on
# the Verdict Card, NEVER blended into CompositeScore.score. Guardrail 5
# shape stays at 4 pillars + 90-pt cap (or 5 pillars + 90 with Positioning).
#
# Formula: normalise TQS to 0-100, then apply a valuation-posture modifier
# in [0.75, 1.15]. Rationale: TQS is the dominant signal (trend quality is
# what the composite already respects); valuation is a modifier that
# rewards paying for growth+quality and penalises paying for hype.
#
#   posture group       modifier   what it says
#   ------------------  --------   -------------------------------------
#   SUPPORTED_BY_       1.10       valuation supported by fundamentals
#     GROWTH_AND_QUALITY  1.15     both growth AND quality supported
#   REASONABLE          1.00       neither supported nor demanding
#   DEMANDING_VS_*      0.75       paying up vs growth/returns/ROE
#   INSUFFICIENT_       0.85       unknown - mild penalty, less than
#     EVIDENCE                     an outright demanding read
#
# Returns None when TQS or posture is missing, so consumers can render
# "not available" rather than a misleading zero.
# ─────────────────────────────────────────────────────────────────────────────

_OVERLAY_MODIFIERS: Dict[str, float] = {
    "SUPPORTED_BY_GROWTH_AND_QUALITY": 1.15,
    "SUPPORTED_BY_GROWTH":              1.10,
    "SUPPORTED_BY_QUALITY":             1.10,
    "SUPPORTED_BY_ROE":                 1.10,
    "REASONABLE":                       1.00,
    "DEMANDING_VS_GROWTH":              0.75,
    "DEMANDING_VS_RETURNS":             0.75,
    "DEMANDING_VS_ROE":                 0.75,
    "INSUFFICIENT_EVIDENCE":            0.85,
}


def compute_overlay_score(tqs: Optional[float],
                          valuation_posture: Optional[str]) -> Optional[int]:
    """TQS x valuation sidecar overlay, 0-100. Pure, deterministic.

    Args:
      tqs: TQSResult.tqs from analysis.trend_quality_score (0-90 native scale).
      valuation_posture: one of the ValuationAssessment.posture constants
        from analysis.fundamentals.valuation_decision (SUPPORTED_*, REASONABLE,
        DEMANDING_*, INSUFFICIENT_EVIDENCE).

    Returns:
      Integer in [0, 100], or None when either input is missing / NaN /
      not recognised. Never raises.
    """
    if tqs is None or valuation_posture is None:
        return None
    try:
        tqs_f = float(tqs)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(tqs_f):
        return None
    modifier = _OVERLAY_MODIFIERS.get(valuation_posture)
    if modifier is None:
        return None
    # Normalise TQS's 0-90 scale to 0-100, then apply the modifier.
    tqs_norm = (max(0.0, min(90.0, tqs_f)) / 90.0) * 100.0
    return int(round(max(0.0, min(100.0, tqs_norm * modifier))))


# ─────────────────────────────────────────────────────────────────────────────
# Sub-scorer: Technical  (40 pts)
# ─────────────────────────────────────────────────────────────────────────────

def _score_technical(df: pd.DataFrame) -> Tuple[float, Dict]:
    cur  = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else cur

    # FIX SCORE-NAN: read through _num() so an indicator still in its warm-up
    # (NaN, not missing) uses the documented default instead of poisoning every
    # comparison below into False. See _num()'s docstring.
    rsi    = _num(cur,  "RSI",         50)
    macd   = _num(cur,  "MACD",         0)
    sig    = _num(cur,  "MACD_Signal",  0)
    macd_p = _num(prev, "MACD",         0)
    sig_p  = _num(prev, "MACD_Signal",  0)
    hist   = _num(cur,  "MACD_Hist",    0)
    hist_p = _num(prev, "MACD_Hist",    0)
    adx    = _num(cur,  "ADX",         15)
    price  = float(cur["Close"])
    # FIX SMA-NEUTRAL (2026-09-24) — missing/warm-up SMAs used to default to
    # 0.95/0.90/0.80 x price, i.e. a perfect bullish stack, so a stock with
    # unknown moving averages earned up to 10/10 SMA points. Missing SMAs are
    # now NaN and every comparison involving them is False: a tier can only
    # be claimed from SMAs that actually exist (e.g. price > SMA_200 with
    # SMA_20/50 unknown still earns the 4-pt tier). Unknown data earns no
    # points from this sub-check — neutral, not rewarded.
    sma20  = _num(cur, "SMA_20",  float("nan"))
    sma50  = _num(cur, "SMA_50",  float("nan"))
    sma200 = _num(cur, "SMA_200", float("nan"))

    pts: Dict[str, float] = {}

    # RSI zone — 12 pts
    # NOTE: Intentionally non-monotonic — oversold (RSI<30) scores 10 pts as a
    # bounce candidate, which outranks a neutral (RSI 50-60 = 9 pts) but not a
    # trending sweet spot (RSI 60-70 = 12 pts). Documented here to avoid
    # confusion during code review.
    if 60 <= rsi <= 70:      pts["rsi"] = 12.0
    elif 50 <= rsi < 60:     pts["rsi"] = 9.0
    elif 70 < rsi <= 80:     pts["rsi"] = 7.0
    elif 40 <= rsi < 50:     pts["rsi"] = 6.0
    elif 30 <= rsi < 40:     pts["rsi"] = 8.0
    elif rsi < 30:           pts["rsi"] = 10.0   # deeply oversold — bounce candidate
    else:                    pts["rsi"] = 1.0    # RSI > 80: overbought

    # MACD — 10 pts
    macd_cross_up   = (macd > sig) and (macd_p <= sig_p)
    hist_expanding  = (abs(hist) > abs(hist_p)) and hist > 0
    if macd > sig:
        pts["macd"] = min(8.0 + (2.0 if macd_cross_up else 0) + (1.0 if hist_expanding else 0), 10.0)
    elif macd < sig and (macd_p >= sig_p):
        pts["macd"] = 2.0
    else:
        pts["macd"] = 4.0 if (sig - macd) < abs(sig * 0.1) else 1.0

    # SMA Stack — 10 pts
    if price > sma20 > sma50 > sma200:   pts["sma"] = 10.0
    elif price > sma50 > sma200:          pts["sma"] = 7.0
    elif price > sma200:                  pts["sma"] = 4.0
    else:                                 pts["sma"] = 0.0
    # Distinguish "unknown" from "bearish" in the breakdown (FIX SMA-NEUTRAL).
    pts["sma_available"] = not math.isnan(sma200)

    # ADX — 8 pts
    if adx > 40:    pts["adx"] = 8.0
    elif adx > 28:  pts["adx"] = 6.0
    elif adx > 20:  pts["adx"] = 3.0
    else:           pts["adx"] = 1.0

    # FIX TECH-BOOL (2026-09-26) — sum the named sub-scores only. A plain
    # sum(pts.values()) also added pts["sma_available"] (True == 1), so every
    # stock with a 200-day SMA got a free +1 and warm-up names did not.
    total = pts["rsi"] + pts["macd"] + pts["sma"] + pts["adx"]
    return round(min(total, 40.0), 2), pts


# ─────────────────────────────────────────────────────────────────────────────
# Sub-scorer: Momentum  (25 pts)
# ─────────────────────────────────────────────────────────────────────────────

# ── FIX REGIME1 (2026-09-03) — regime-conditional momentum dispatch flag ──
# Recommendation 5 of docs/COMPOSITE_SCORE_SHAPE_REVIEW.md. Guarded behind an
# env var so shipping the *mechanism* doesn't change production scoring: the
# study that would license flipping the default (research/score_variants_regime.py)
# has not yet been run end-to-end. When the flag is ON AND the regime is
# 'bear' (trend_down / risk_off) the absolute-returns half of the Momentum
# pillar (15 pts under Rec 1) is replaced by a self-normalised 5-day mean-
# reversion percentile — this is Var M from the variant study, the ONLY one
# of the three that preserves Guardrail 5 (4 pillars, 40+25+15+10, cap 90).
# RS component (10 pts) stays unchanged in either mode.
import os as _regime_os

def _regime_weights_enabled() -> bool:
    """Default ON since 2026-09-24 (docs/SCORE_EFFICACY_2026-09-24.md: the
    bear-regime variant improved bear ranking in both halves of the 5-year
    replay, bull/range untouched). Set NSE_USE_REGIME_WEIGHTS=0 to disable."""
    _v = _regime_os.environ.get("NSE_USE_REGIME_WEIGHTS", "").strip().lower()
    return _v not in {"0", "false", "no", "off"}


# Live regime label cache — snapshot_live() makes two fetches (^INDIAVIX,
# ^NSEI); calling it per scored ticker would multiply that by the universe.
# Pure module-level TTL cache (analysis/ stays Streamlit-free).
import time as _regime_time
_REGIME_LABEL_CACHE: Dict[str, object] = {"t": 0.0, "label": None, "ok": False}
_REGIME_TTL_OK   = 1800.0   # 30 min — regime inputs are daily
_REGIME_TTL_FAIL = 300.0    # retry a failed snapshot after 5 min


def _live_regime_label() -> Optional[str]:
    now = _regime_time.time()
    c = _REGIME_LABEL_CACHE
    ttl = _REGIME_TTL_OK if c["ok"] else _REGIME_TTL_FAIL
    if c["t"] and now - float(c["t"]) < ttl:
        return c["label"]  # type: ignore[return-value]
    label, ok = None, False
    try:
        from analysis.regime import snapshot_live as _reg_snap
        label = getattr(_reg_snap(), "label", None)
        ok = label is not None
    except Exception as _rg_e:
        _log.debug("regime snapshot unavailable: %s: %s", type(_rg_e).__name__, _rg_e)
    c.update(t=now, label=label, ok=ok)
    return label

# Regime labels analysis.regime.snapshot_live() emits, mapped to the
# three-way {bull, bear, sideways} the variant study used.
_BEAR_REGIMES  = {"trend_down", "risk_off", "bear"}
_BULL_REGIMES  = {"trend_up", "bull"}
_RANGE_REGIMES = {"range", "sideways"}


def _score_momentum_mean_reversion(df: pd.DataFrame) -> Tuple[float, Dict]:
    """
    Bear-regime alternative to _score_momentum_absolute. Returns (pts_0_15, detail).

    Score = inverted percentile rank of the stock's trailing-5-day return
    within its own trailing 252-day distribution of 5-day returns, scaled to
    0..15. Low recent 5d return -> high reversal score. Self-normalised so no
    cross-sectional lookahead. Direct implementation of Var M from
    research/score_variants_regime.py.
    """
    close = df["Close"]
    r5d = close.pct_change(5).dropna()
    if len(r5d) < 30:
        # Not enough history for a self-percentile — return neutral half.
        return 7.5, {"mr_note": "insufficient history for 5d reversal percentile",
                     "mr_available": False}
    window = r5d.tail(252)
    today  = float(window.iloc[-1])
    prior  = window.iloc[:-1]
    if len(prior) < 20:
        return 7.5, {"mr_note": "insufficient history",
                     "mr_available": False}
    # Percentile of today within prior: 0.0 = worst in window, 1.0 = best.
    # Invert so weakest 5d returns pay the most points (mean-reversion bet).
    pct = float((prior < today).sum()) / float(len(prior))
    inv = 1.0 - pct
    pts = round(inv * 15.0, 2)
    return pts, {
        "mr_available":   True,
        "mr_r5d_pct":     round(pct, 3),
        "mr_r5d_today":   round(today * 100, 2),
    }


def _score_momentum(df: pd.DataFrame,
                    regime_label: Optional[str] = None) -> Tuple[float, Dict]:
    """
    Momentum pillar (25 pts).

    Default computation (production):
      abs_returns (0-15, or 0-25 legacy when RS_Score absent) + rs_vs_nifty (0-10).

    When NSE_USE_REGIME_WEIGHTS env var is truthy AND regime_label is 'bear'
    (trend_down / risk_off), the abs_returns half is replaced by a 5-day
    mean-reversion percentile (Var M from score_variants_regime.py). RS
    component stays. Guardrail 5 shape unchanged (Momentum stays 25 pts).

    Flag defaults OFF — flipping requires the study run per Task 3.6 in
    tasks/plan.md.
    """
    if len(df) < 25:
        # Not enough history — return neutral 50% and flag it so the caller
        # can add a disclaimer to the narrative.
        return 12.5, {"note": "insufficient history", "is_fallback": True}

    close = df["Close"]
    r5d   = float(close.pct_change(5).iloc[-1])  * 100
    r20d  = float(close.pct_change(20).iloc[-1]) * 100

    # FIX SCORE-R60 — when fewer than 60 bars were available this used to set
    # r60d = 0.0 and score it like any other number. 0.0 does not land in a
    # neutral bucket: it falls past `r60d > 0` (0 is not > 0) into the
    # `r60d > -10` branch, which pays 1.0 of 10 points. So "we have no 3-month
    # history for this stock" was scored as "this stock is slightly DOWN over
    # 3 months" — a 9-point penalty out of a 25-point momentum budget, applied
    # to every recent listing and every caller passing a short frame to
    # score_dataframe(). Same defect family as FIX SCORE-NAN: absence of
    # evidence read as evidence of weakness. Unknown now scores the neutral
    # midpoint of that component and says so, rather than quietly convicting.
    has_r60 = len(df) >= 61
    r60d    = float(close.pct_change(60).iloc[-1]) * 100 if has_r60 else float("nan")

    pts: Dict[str, float] = {}

    # 5-day return — 5 pts
    if r5d >  3:    pts["r5d"] = 5.0
    elif r5d > 1:   pts["r5d"] = 4.0
    elif r5d > 0:   pts["r5d"] = 3.0
    elif r5d > -2:  pts["r5d"] = 2.0
    else:           pts["r5d"] = 0.0

    # 20-day return — 10 pts
    if r20d > 15:   pts["r20d"] = 10.0
    elif r20d > 8:  pts["r20d"] = 8.0
    elif r20d > 4:  pts["r20d"] = 7.0
    elif r20d > 2:  pts["r20d"] = 5.0
    elif r20d > 0:  pts["r20d"] = 4.0
    elif r20d > -5: pts["r20d"] = 2.0
    else:           pts["r20d"] = 0.0

    # 60-day return — 10 pts
    if not has_r60:
        # FIX SCORE-R60: unknown ≠ weak. Neutral midpoint of this component,
        # flagged so the narrative can disclose it instead of silently
        # presenting a penalised score as a considered one.
        pts["r60d"] = 5.0
    elif r60d > 25:  pts["r60d"] = 10.0
    elif r60d > 15:  pts["r60d"] = 8.0
    elif r60d > 8:   pts["r60d"] = 6.0
    elif r60d > 3:   pts["r60d"] = 5.0
    elif r60d > 0:   pts["r60d"] = 3.0
    elif r60d > -10: pts["r60d"] = 1.0
    else:            pts["r60d"] = 0.0

    pts["_r5d"]  = round(r5d,  2)
    pts["_r20d"] = round(r20d, 2)
    pts["_r60d"] = round(r60d, 2) if has_r60 else None
    pts["r60d_available"] = has_r60

    abs_total = pts["r5d"] + pts["r20d"] + pts["r60d"]

    # ── FIX RS1 (2026-09-03) — Relative Strength vs Nifty ───────────────────
    # Absolute returns reward beta in bull tapes and punish defensives in
    # bear tapes. The 5-year efficacy study (docs/SCORE_EFFICACY_REPORT.md)
    # tied the 62 → 46% BUY-hit-rate drop between train (trending) and
    # holdout (mean-reverting) partly to this. IBD-style RS_Score (0-100
    # percentile rank of the stock/Nifty ratio's 52-week distribution) is
    # already computed by utils.indicators.add_relative_strength() and
    # written to the df as RS_Score before this function is called (see
    # score_stock). When present, the Momentum pillar's 25 pts split as
    # abs_returns:15 + rs_vs_nifty:10 — same total, same shape (Guardrail
    # §5 unchanged). When absent (test frames with synthetic data, single-
    # ticker ad-hoc scoring without a benchmark fetch), the full 25 pts
    # stay on absolute momentum for backwards-compat.
    #
    # This lands Task 3.1 from tasks/plan.md and Recommendation 1 from
    # docs/COMPOSITE_SCORE_SHAPE_REVIEW.md.
    rs_score = _num(df.iloc[-1], "RS_Score", float("nan"))
    _rs_available = not math.isnan(rs_score)

    # ── FIX REGIME1 — Var M dispatch (bear + flag only) ───────────────────
    # In bear regime with the opt-in flag set, replace the abs_returns half
    # of the pillar with a 5d mean-reversion percentile. RS stays if
    # available. When RS is absent (test frames, ad-hoc scoring), Var M
    # still runs and gives the full 15 pts to mean-reversion; the remaining
    # 10 pts of the 25 stay unfilled (backwards-compat with the "abs alone
    # is 25 pts" legacy shape breaks here by design — this is bear-regime
    # opt-in, opting into it means opting into Var M).
    _regime_bear = (regime_label or "").lower() in _BEAR_REGIMES
    _use_var_m   = _regime_bear and _regime_weights_enabled()

    if _use_var_m:
        mr_pts, mr_detail = _score_momentum_mean_reversion(df)
        pts.update(mr_detail)
        pts["variant"]  = "M_bear_mean_reversion"
        pts["regime"]   = regime_label
        if _rs_available:
            # FIX BEAR-NEUTRAL (2026-09-28): RS ranks backwards in bear regimes
            # (10y per-week IC, trend_down 20d -0.036 / 60d -0.039), so it is
            # held at its neutral midpoint (5 of 10) instead of scored. Raw
            # value kept in the breakdown. docs/SCORE_EFFICACY_BEAR_2026-09-28.md
            pts["rs_available"]   = True
            pts["rs_score"]       = round(rs_score, 1)
            pts["rs_pts_raw"]     = round(float(np.clip(rs_score, 0.0, 100.0)) / 100.0 * 10.0, 2)
            pts["rs_pts"]         = 5.0
            pts["rs_neutralised"] = True
            total = mr_pts + 5.0
        else:
            pts["rs_available"] = False
            total = mr_pts   # 0-15 only; 10 pts unfilled in ad-hoc RS-less mode
    elif _rs_available:
        # Scale absolute component 25 → 15
        abs_scaled = abs_total * (15.0 / 25.0)
        # RS component: linear map 0-100 → 0-10, per _bonus_rs() convention
        # in research/score_variants_rs.py which studied exactly this shape.
        rs_pts = round(float(np.clip(rs_score, 0.0, 100.0)) / 100.0 * 10.0, 2)
        pts["rs_available"] = True
        pts["rs_score"]     = round(rs_score, 1)
        pts["rs_pts"]       = rs_pts
        pts["abs_scaled"]   = round(abs_scaled, 2)
        pts["variant"]      = "base_abs_plus_rs"
        total = abs_scaled + rs_pts
    else:
        pts["rs_available"] = False
        pts["variant"]      = "legacy_abs_only"
        total = abs_total

    return round(min(total, 25.0), 2), pts


# ─────────────────────────────────────────────────────────────────────────────
# Sub-scorer: Volume  (15 pts)
# ─────────────────────────────────────────────────────────────────────────────

_NSE_CLOSE_T = (15, 30)


def _now_ist_naive() -> "datetime.datetime":
    import datetime as _d
    return _d.datetime.now(_d.timezone(_d.timedelta(hours=5, minutes=30))).replace(tzinfo=None)


def _last_bar_in_progress(df: pd.DataFrame, now_ist=None) -> bool:
    """True when df's last DAILY bar is today's session and the market hasn't
    closed yet, so its volume is a partial day's (FIX VOL-PARTIAL)."""
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return False
    last = df.index[-1]
    if (last.hour, last.minute, last.second) != (0, 0, 0):
        return False                       # intraday bars — not a daily frame
    now = now_ist or _now_ist_naive()
    if last.date() != now.date():
        return False
    try:
        from utils.nse_holidays import is_trading_day
        if not is_trading_day(now.date()):
            return False
    except Exception:                      # calendar unavailable — weekday check only
        if now.weekday() >= 5:
            return False
    return (now.hour, now.minute) < _NSE_CLOSE_T


def _score_volume(df: pd.DataFrame,
                  delivery_info: Optional[Dict] = None,
                  now_ist=None) -> Tuple[float, Dict]:
    """Volume pillar (15 pts).

    Two modes, gated on `delivery_info` availability:

    Legacy (backwards-compat, delivery_info=None) — 15 pts split as
        vol_ratio:10 + obv:5

    With delivery — 15 pts split as
        vol_ratio:8 + delivery_pct:4 + obv:3
    Rationale: NSE bhavcopy delivery % is the closest thing retail India has
    to a Level-2 institutional print. High delivery on rising price is
    institutional accumulation; high delivery on falling price is
    distribution; low delivery on a sharp up-move is intraday froth that
    tends to unwind. Guardrail 5 shape unchanged (Volume stays 15 pts of 90).

    Ships Recommendation 4 from docs/COMPOSITE_SCORE_SHAPE_REVIEW.md.
    """
    # FIX VOL-PARTIAL (audit 2026-10-03): during market hours today's daily
    # bar carries a partial session's volume, so every stock read "low
    # volume" in the morning and drifted up through the day. Score the last
    # COMPLETED session instead (which is also the session the delivery %
    # bhavcopy describes).
    _in_progress = _last_bar_in_progress(df, now_ist) and len(df) >= 2
    _vdf = df.iloc[:-1] if _in_progress else df
    cur        = _vdf.iloc[-1]
    vol_ratio  = _num(cur, "Volume_Ratio", 1.0)   # FIX SCORE-NAN
    close      = float(cur["Close"])
    # FIX UPDAY-PREVCLOSE (audit 2026-10-03): direction is the day's change vs
    # the previous close. close >= open called a gap-up that faded but still
    # closed +3% a DOWN day and cut its volume points to 40%.
    if len(_vdf) >= 2 and np.isfinite(float(_vdf["Close"].iloc[-2])):
        up_day = close >= float(_vdf["Close"].iloc[-2])
    else:
        up_day = close >= _num(cur, "Open", close)

    pts: Dict[str, float] = {}
    pts["up_day"] = bool(up_day)
    pts["vol_ratio_raw"] = round(vol_ratio, 3) if np.isfinite(vol_ratio) else None
    pts["volume_session"] = "previous (today in progress)" if _in_progress else "latest"
    _use_delivery = (
        isinstance(delivery_info, dict)
        and delivery_info.get("today") is not None
        and delivery_info.get("mean")  is not None
    )

    # ── vol_ratio: same bucketing shape, top rescaled from 10 to 8 when
    # delivery is available (Guardrail 5: pillar stays 15 pts).
    if vol_ratio > 2.5:   raw_vol = 10.0
    elif vol_ratio > 1.8: raw_vol = 8.0
    elif vol_ratio > 1.2: raw_vol = 6.0
    elif vol_ratio > 0.8: raw_vol = 4.0
    else:                 raw_vol = 1.0
    _vol_after_dir = raw_vol if up_day else max(1.0, raw_vol * 0.4)
    if _use_delivery:
        # Scale 10-pt shape down to 8: multiply by 0.8, cap at 8.
        pts["vol_ratio"] = round(min(_vol_after_dir * 0.8, 8.0), 2)
    else:
        pts["vol_ratio"] = _vol_after_dir

    # ── OBV slope: same shape, top rescaled from 5 to 3 when delivery is
    # available.
    if "OBV" in _vdf.columns and len(_vdf) >= 10:
        obv_recent = _vdf["OBV"].iloc[-10:].values.astype(float)
        slope      = float(np.polyfit(range(10), obv_recent, 1)[0])
        _obv_raw = 5.0 if slope > 0 else (2.0 if abs(slope) < abs(obv_recent.mean()) * 0.001 else 0.0)
    else:
        _obv_raw = 2.0
    if _use_delivery:
        pts["obv"] = round(min(_obv_raw * 0.6, 3.0), 2)   # 5 -> 3
    else:
        pts["obv"] = _obv_raw

    # ── Delivery % sub-score (FIX DELIV1, 2026-09-03) — 4 pts when
    # delivery snapshot is present.
    if _use_delivery:
        today = float(delivery_info["today"])
        mean  = float(delivery_info["mean"])
        z     = delivery_info.get("zscore")
        # Absolute level: what fraction of trades are actually taken to demat.
        # 40% is roughly the market-cap-weighted large-cap norm; above 60% is
        # institutional footprint, below 20% is pure intraday churn.
        if   today >= 60: abs_pts = 2.5
        elif today >= 45: abs_pts = 2.0
        elif today >= 30: abs_pts = 1.0
        elif today >= 20: abs_pts = 0.5
        else:              abs_pts = 0.0
        # Direction vs the stock's own 60-day mean — separates
        # accumulation (today > mean) from distribution / intraday froth.
        if z is None:      dir_pts = 0.75           # std=0 pathological: neutral
        elif z >  1.0:     dir_pts = 1.5           # sharply above own mean
        elif z >  0.25:    dir_pts = 1.0           # above mean
        elif z > -0.25:    dir_pts = 0.75          # near mean
        elif z > -1.0:     dir_pts = 0.25          # below mean
        else:              dir_pts = 0.0           # sharp under-delivery
        _deliv_pts = round(min(abs_pts + dir_pts, 4.0), 2)
        pts["delivery"]           = _deliv_pts
        pts["delivery_today"]     = round(today, 2)
        pts["delivery_mean"]      = round(mean, 2)
        pts["delivery_z"]         = z
        pts["delivery_n"]         = int(delivery_info.get("n", 0))
        pts["delivery_available"] = True
        # Divergence flag — surfaced in narrative by _build_narrative()
        pts["delivery_divergence"] = bool(up_day and vol_ratio > 1.5 and z is not None and z < -1.0)
    else:
        pts["delivery_available"] = False

    total = pts["vol_ratio"] + pts["obv"] + (pts.get("delivery", 0.0) if _use_delivery else 0.0)
    return round(min(total, 15.0), 2), pts


# ─────────────────────────────────────────────────────────────────────────────
# Pattern detection — INFORMATIONAL ONLY (feeds narrative, not the composite)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_patterns(df: pd.DataFrame) -> Dict:
    """
    Detect candlestick patterns in the last 3 bars.
    Returns {"patterns": List[str]} — no numeric score.
    Patterns excluded from composite (variant study: zero-to-negative ranking power).
    """
    recent = df.iloc[-3:] if len(df) >= 3 else df
    found: List[str] = []

    bull_pat_cols = [
        ("Pat_MorningStar",   "MorningStar"),
        ("Pat_BullEngulfing", "BullEngulfing"),
        ("Pat_Hammer",        "Hammer"),
        ("Pat_BullMarubozu",  "BullMarubozu"),
        ("Pat_Doji",          "Doji"),
    ]
    bear_pat_cols = [
        ("Pat_EveningStar",   "EveningStar⚠️"),
        ("Pat_BearEngulfing", "BearEngulfing⚠️"),
        ("Pat_ShootingStar",  "ShootingStar⚠️"),
    ]

    for col, label in bull_pat_cols:
        if col in recent.columns and recent[col].any():
            found.append(label)
            break  # strongest bullish only

    for col, label in bear_pat_cols:
        if col in recent.columns and recent[col].any():
            found.append(label)
            break  # strongest bearish only

    cur = df.iloc[-1]
    if cur.get("RSI_Bull_Div", 0): found.append("RSI_BullDiv")
    if cur.get("RSI_Bear_Div", 0): found.append("RSI_BearDiv⚠️")

    return {"patterns": found}


# ─────────────────────────────────────────────────────────────────────────────
# Sub-scorer: Sentiment  (10 pts)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_sector_rank(sector: str, sector_scores_df) -> Tuple[Optional[int], int]:
    """(rank, number of ranked sectors) for score_stock.

    FIX SECTOR-RANK-DEFAULT (audit 2026-10-03): this used to default to rank 7
    measured against len(list_sectors()) == 22, and 7 <= 22 // 3 put every
    unranked stock in the top third. The thirds are now measured against the
    sectors actually ranked, and a sector missing from the ranking (or no
    ranking at all) is None, which _score_sentiment scores as neutral.
    """
    if sector_scores_df is None or getattr(sector_scores_df, "empty", True) \
            or "Rank" not in sector_scores_df.columns:
        return None, 0
    n = int(len(sector_scores_df))
    if sector not in sector_scores_df.index:
        return None, n
    return int(sector_scores_df.loc[sector, "Rank"]), n


def _sector_points(sector_rank: Optional[int], n_sectors: int, *, top: float, mid: float) -> float:
    """Top / middle / bottom third of the sector ranking; unranked = middle (neutral)."""
    if sector_rank is None or n_sectors <= 0:
        return mid
    if sector_rank <= n_sectors // 3:
        return top
    if sector_rank <= 2 * n_sectors // 3:
        return mid
    return 0.0


def _score_sentiment(vix_info: Dict, sector_rank: Optional[int], n_sectors: int = 15,
                     flows_info: Optional[Dict] = None) -> Tuple[float, Dict]:
    """Sentiment pillar (10 pts).

    Two modes, gated on `flows_info` availability:

    Legacy (backwards-compat, flows_info is None) — the 10 pts split as
        vix:6 + sector_rank:4
    which is how every caller before FIX FLOWS1 (2026-09-03) worked.

    With flows (flows_info dict from analysis.fii_dii.load_history) — the
    10 pts split as
        vix:5 + sector_rank:3 + fii_dii_flows:2
    Rationale: FII/DII cash-market net-buy/sell is the single largest
    driver of same-week Nifty direction on Indian equities — the app
    already computes and displays it on Analyze Stock's market-context
    strip, but the composite score was blind to it. Guardrail 5 shape
    unchanged (Sentiment stays 10 pts of the 90).

    Ships Recommendation 2 from docs/COMPOSITE_SCORE_SHAPE_REVIEW.md.
    """
    regime = (vix_info or {}).get("regime", "normal")
    pts: Dict[str, float] = {}
    pts["sector_ranked"] = sector_rank is not None

    _flows_available = (
        isinstance(flows_info, dict)
        and flows_info.get("fii_5d") is not None
        and flows_info.get("dii_5d") is not None
    )

    if _flows_available:
        # Split 5/3/2 — see docstring. VIX map preserves the old 6-pt map's
        # relative shape then rescales so the max is 5 (normal) instead of 6.
        # FIX VIX-NEUTRAL (2026-09-24): VIX is identical for every stock on a
        # date, so it can't rank anything; replayed over 5y its points ran
        # opposite to forward returns (fear had the best 20d returns but the
        # fewest points). Held at the "normal" value so the pillar max and
        # calm-market scores are unchanged. VIX still drives stop width and
        # horizon, and is shown as context. See SCORE_EFFICACY_2026-09-24.md.
        pts["vix"] = 5.0
        pts["vix_regime"] = regime

        pts["sector"] = _sector_points(sector_rank, n_sectors, top=3.0, mid=1.5)

        # Flows: sign of 5-day (FII + DII) net cash-market
        _fii = float(flows_info.get("fii_5d") or 0.0)
        _dii = float(flows_info.get("dii_5d") or 0.0)
        if _fii > 0 and _dii > 0:
            pts["flows"] = 2.0   # broad participation — persistent rallies
        elif _fii < 0 and _dii < 0:
            pts["flows"] = 0.0   # distribution — usually precedes weakness
        elif _fii < 0 and _dii > 0:
            pts["flows"] = 1.5   # domestic-supported dip — tradeable pullback
        elif _fii > 0 and _dii < 0:
            pts["flows"] = 1.0   # DII profit-taking rally — shallower legs
        else:
            pts["flows"] = 1.0   # mixed / one leg is zero
        pts["flows_available"] = True
        pts["fii_5d"] = round(_fii, 1)
        pts["dii_5d"] = round(_dii, 1)

        total = pts["vix"] + pts["sector"] + pts["flows"]
    else:
        # Legacy mode — unchanged from pre-2026-09-03 behavior
        # FIX VIX-NEUTRAL (2026-09-24) — see the flows branch above.
        pts["vix"] = 6.0
        pts["vix_regime"] = regime

        pts["sector"] = _sector_points(sector_rank, n_sectors, top=4.0, mid=2.0)

        pts["flows_available"] = False
        total = pts["vix"] + pts["sector"]

    return round(min(total, 10.0), 2), pts


# ─────────────────────────────────────────────────────────────────────────────
# Sub-scorer: Positioning  (10 pts, F&O + flag ON only)
# Recommendation 6 design 6a of docs/COMPOSITE_SCORE_SHAPE_REVIEW.md.
# ─────────────────────────────────────────────────────────────────────────────

def _positioning_pillar_enabled() -> bool:
    """Env flag gating the whole pillar. Same opt-in discipline as Rec 5.

    Default OFF. Setting NSE_USE_POSITIONING_PILLAR to any truthy value
    activates the 5-pillar 35+20+15+10+10=90 shape on F&O-eligible tickers.
    Non-F&O tickers keep the legacy 4-pillar 40+25+15+10=90 shape in either
    state; the flag is not enough on its own — F&O eligibility is required
    because the pillar's inputs (OI regime, PCR, max pain, FII index-futs)
    literally do not exist for non-F&O names.
    """
    _v = _regime_os.environ.get("NSE_USE_POSITIONING_PILLAR", "").strip().lower()
    return _v in {"1", "true", "yes", "on"}


# Neutral midpoints used when a data pipeline for a sub-input is not yet
# online. Rationale: shipping the pillar with all-zero defaults would
# systematically demote F&O names once the flag is on, before the data
# even arrives. Neutrals mean a "no positioning read" is treated the same
# as "everything is average" — matches the "unknown" bucket convention
# used for VIX regime, sector rank and (in Rec 3) SL bounds.
_POS_OI_REGIME_MAP = {
    "long_buildup":    3.0,   # fresh longs on rising price
    "short_covering":  2.0,   # weak bears exiting on rising price
    "long_unwinding":  1.0,   # weak bulls exiting on falling price
    "short_buildup":   0.0,   # fresh shorts on falling price
}


def _score_positioning(
    oi_regime:              Optional[str]   = None,
    pcr:                    Optional[float] = None,
    max_pain_distance_pct:  Optional[float] = None,
    fii_deriv_net:          Optional[float] = None,
    fii_deriv_pct:          Optional[float] = None,
) -> Tuple[float, Dict]:
    """Positioning pillar (10 pts) — F&O eligibility + opt-in flag only.

    Sub-scores (all graceful when data absent, defaulting to their neutral
    midpoint so unknown reads never systematically penalise a name):

      OI regime         (3 pts)  long_buildup / short_covering /
                                 long_unwinding / short_buildup
      PCR               (2 pts)  extreme reads are contrarian
      Max pain distance (2 pts)  price near max pain = pinning risk
      FII deriv (idx)   (3 pts)  where today's FII net index-futures position
                                 sits in its trailing ~1y range (percentile)

    Ships in this landing with all four sub-inputs default-None (data
    pipelines are queued as follow-ups per docs/POSITIONING_INTEGRATION_2026-09.md).
    Each pipeline lands as its own commit and lights up its sub-score.
    """
    pts: Dict[str, float] = {}

    # OI regime
    pts["oi_regime"] = _POS_OI_REGIME_MAP.get((oi_regime or "").lower(), 1.5)

    # PCR: <0.6 extreme complacency (bearish); 0.6-0.9 mildly bullish;
    # 0.9-1.2 healthy; 1.2-1.5 mild fear; >1.5 extreme fear (contrarian
    # bullish). Neutral default 1.0 of 2.
    if pcr is None:
        pts["pcr"] = 1.0
    elif pcr < 0.6:  pts["pcr"] = 0.5
    elif pcr < 0.9:  pts["pcr"] = 1.5
    elif pcr <= 1.2: pts["pcr"] = 1.5
    elif pcr <= 1.5: pts["pcr"] = 1.0
    else:            pts["pcr"] = 2.0

    # Max pain distance %: near expiry, price tends to pin toward max pain.
    # Far from pin gives options-writers room; on-pin has more chop risk.
    if max_pain_distance_pct is None:
        pts["max_pain"] = 1.0
    elif abs(max_pain_distance_pct) < 1.0:  pts["max_pain"] = 0.5
    elif abs(max_pain_distance_pct) < 3.0:  pts["max_pain"] = 1.0
    else:                                    pts["max_pain"] = 1.5

    # FII net index-futures position. FIX FII-PCTL (2026-09-27): scored on
    # its PERCENTILE within the trailing ~250 sessions (0-1), not on the raw
    # contract count. The old fixed +/-30k bands assumed a -100k..+100k
    # range; live 2026-09 values sit near -300k (lot-size changes shift
    # contract counts), so the sub-score was pinned at 0. A raw net with no
    # percentile (too little history) scores the neutral midpoint.
    if fii_deriv_pct is None:
        pts["fii_deriv"] = 1.5
    elif fii_deriv_pct >= 0.80:     pts["fii_deriv"] = 3.0
    elif fii_deriv_pct >= 0.50:     pts["fii_deriv"] = 2.0
    elif fii_deriv_pct >= 0.20:     pts["fii_deriv"] = 1.0
    else:                            pts["fii_deriv"] = 0.0

    pts["_oi_regime_input"]        = oi_regime
    pts["_pcr_input"]              = pcr
    pts["_max_pain_pct_input"]     = max_pain_distance_pct
    pts["_fii_deriv_net_input"]    = fii_deriv_net
    pts["_fii_deriv_pct_input"]    = fii_deriv_pct

    total = pts["oi_regime"] + pts["pcr"] + pts["max_pain"] + pts["fii_deriv"]
    return round(min(total, 10.0), 2), pts


# ─────────────────────────────────────────────────────────────────────────────
# Entry / SL / TP
# ─────────────────────────────────────────────────────────────────────────────

_SL_BOUNDS_BY_REGIME = {
    # FIX SL-REGIME (2026-09-03) — Ships Recommendation 3 from
    # docs/COMPOSITE_SCORE_SHAPE_REVIEW.md. ATR is a rolling 14-bar volatility
    # measure; when VIX shifts regime, the same ATR multiple represents a
    # very different amount of realized noise. Fixed [1.2, 3.0] bounds
    # whipsaw longs out in panic and give lazy room in complacency. Bounds
    # widen with regime volatility; mid_mult (fallback when swing_low is
    # too far out) tracks the midpoint. Not a scoring change (Guardrail 5
    # unaffected) — only SL / target-risk / R:R move.
    "complacency": (1.0, 1.75, 2.5),   # low-VIX: tighten
    "normal":      (1.2, 2.0,  3.0),   # unchanged baseline
    "elevated":    (1.3, 2.2,  3.2),
    "fear":        (1.5, 2.5,  3.5),
    "panic":       (1.7, 2.75, 3.8),   # high-VIX: give room
    "unknown":     (1.2, 2.0,  3.0),   # match baseline
}


def _compute_entry_levels(
    df: pd.DataFrame, score: float,
    vix_regime: Optional[str] = None,
) -> Tuple[float, float, float, float]:
    """
    Structure-aware stop + volatility-calibrated target.
    Entry = current close (live price injected later in score_stock if Angel One connected).

    FIX RR1: the target used to come from a fixed 4-bucket score ladder
    (score>=72 -> 3.0x risk, >=60 -> 2.5x, >=48 -> 2.0x, else 1.5x) — the
    same multiplier for every stock in a band regardless of how that stock
    actually moves. The stop was already stock-specific (ATR + swing-low);
    the target wasn't, which is the "fixed, not genuine" feeling flagged
    against this page. The target multiplier is now anchored to THIS
    stock's own realized volatility — a ~10-trading-day expected move,
    scaled from its trailing daily-return std-dev via sqrt-time scaling —
    expressed in ATR units. Score still nudges the target (higher
    conviction reaches a bit further), but it no longer overrides what's
    realistic for that specific stock: a calm large-cap and a choppy
    small-cap in the same score band no longer get an identical target.
    """
    cur   = df.iloc[-1]
    price = float(cur["Close"])
    atr   = float(cur.get("ATR", price * 0.02))
    if atr <= 0 or pd.isna(atr):
        atr = price * 0.02

    # FIX SL-REGIME: bounds now scale with VIX regime — see _SL_BOUNDS_BY_REGIME
    # docstring above for rationale. Unknown / no-regime-passed keeps the
    # historical (1.2, 2.0, 3.0) baseline for backwards-compat with callers
    # that never wired regime through (tests with synthetic frames included).
    _min_m, _mid_m, _max_m = _SL_BOUNDS_BY_REGIME.get(
        (vix_regime or "unknown").lower(), _SL_BOUNDS_BY_REGIME["unknown"]
    )

    lows      = df["Low"].tail(10).dropna()
    swing_low = float(lows.min()) if len(lows) else price - _mid_m * atr
    sl        = swing_low - 0.25 * atr

    max_risk = _max_m * atr
    min_risk = _min_m * atr
    if price - sl > max_risk:
        sl = price - _mid_m * atr
    if price - sl < min_risk:
        sl = price - min_risk

    risk = max(price - sl, 0.01)

    # ── FIX RR1: stock-specific expected move, in ATR units ──
    # 10 trading days ≈ 2 calendar weeks — matches the "Swing" horizon
    # bucket in _pick_horizon() below, so the target and the holding-period
    # label describe the same window rather than two unrelated numbers.
    _HOLD_DAYS = 10
    closes = df["Close"].tail(120).dropna()
    expected_move_atr = None
    if len(closes) >= 20:
        daily_ret = closes.pct_change().dropna()
        daily_vol = float(daily_ret.std())
        if daily_vol > 0 and not pd.isna(daily_vol):
            expected_move_price = price * daily_vol * math.sqrt(_HOLD_DAYS)
            expected_move_atr = expected_move_price / atr

    if   score >= 72: conviction = 1.25
    elif score >= 60: conviction = 1.10
    elif score >= 48: conviction = 0.95
    else:             conviction = 0.80

    if expected_move_atr is not None and expected_move_atr > 0:
        rr_mult = expected_move_atr * conviction
        # Bounds keep targets sane at either extreme — under 1.2x risk isn't
        # a real trade idea, and past 4x risk the volatility estimate is
        # more noise than signal (short history, thin trading, a recent gap).
        rr_mult = max(1.2, min(rr_mult, 4.0))
    else:
        # FIX RR1 fallback: not enough price history for a volatility
        # estimate (e.g. a recent listing) — use the old fixed ladder
        # rather than guessing off too little data.
        if   score >= 72: rr_mult = 3.0
        elif score >= 60: rr_mult = 2.5
        elif score >= 48: rr_mult = 2.0
        else:             rr_mult = 1.5

    tp = price + rr_mult * risk
    rr = round((tp - price) / risk, 2)
    return round(price, 2), round(sl, 2), round(tp, 2), rr


def _pick_horizon(tech_pts: float, mom_pts: float,
                  vix_regime: Optional[str] = None,
                  score: Optional[float] = None) -> Tuple[str, int]:
    """FIX HZ2 (2026-09-04) — granular horizon based on momentum share,
    VIX regime and score strength.

    Previously this was a binary flip between "Swing (3–10 trading days)"
    and "Positional (2–6 weeks)" — every stock in the app read one of
    exactly two labels forever, which is what the user reported as "the
    horizon is the same for all stocks". Not a bug in the sense of a
    crash, but a bug in that the label carried no per-stock information.

    New shape:
      * mom/(tech+mom) ratio picks one of four base windows
      * VIX regime shortens (fear / panic) or extends (complacency) the
        window — high vol tape resolves faster, low vol tape gives room
      * Very low score (<40) forces a "Watch only" label — the composite
        isn't strong enough to justify a directional horizon at all

    Backwards-compat: vix_regime + score default to None; when both are
    None the function returns one of the four base labels only (no VIX /
    score modifiers), still more varied than the old binary switch.
    """
    # Very low conviction — no meaningful trade horizon.
    if score is not None and score < 40:
        return "Watch only – conviction too low for a horizon", 7

    total = float(tech_pts) + float(mom_pts)
    if total <= 0:
        return "Watch – no clear timeframe", 7
    mom_ratio = float(mom_pts) / total

    # Base window from momentum-to-total ratio.
    if mom_ratio >= 0.65:
        label, days = "Fast swing (2–5 trading days)", 7
    elif mom_ratio >= 0.55:
        label, days = "Swing (5–10 trading days)", 14
    elif mom_ratio >= 0.45:
        label, days = "Positional (2–4 weeks)", 28
    else:
        label, days = "Trend hold (4–8 weeks)", 56

    # VIX regime modifier — high vol -> shorter window; complacency -> longer.
    reg = (vix_regime or "").lower()
    if reg in {"fear", "panic"}:
        days = max(3, int(round(days * 0.7)))
        label = f"{label} (high-VIX: window shortened)"
    elif reg == "complacency":
        days = int(round(days * 1.25))
        label = f"{label} (low-VIX: window extended)"

    return label, days


# ─────────────────────────────────────────────────────────────────────────────
# Grade + Action
# ─────────────────────────────────────────────────────────────────────────────

def _grade(score: float) -> str:
    if score >= 88: return "A+"
    if score >= 75: return "A"
    if score >= 62: return "B"
    if score >= 48: return "C"
    if score >= 32: return "D"
    return "F"

def _action(score: float) -> str:
    if score >= 80: return "STRONG BUY"
    if score >= 65: return "BUY"
    if score >= 52: return "WATCHLIST"
    if score >= 40: return "HOLD"
    if score >= 25: return "CAUTION"
    return "EXIT"


# ─────────────────────────────────────────────────────────────────────────────
# Plain-English Narrative
# ─────────────────────────────────────────────────────────────────────────────

def _build_narrative(
    ticker: str, df: pd.DataFrame, score: float, grade: str, action: str,
    tech_pts: Dict, mom_pts: Dict, vol_pts: Dict, patterns: List[str],
    entry: float, sl: float, tp: float, rr: float,
    sector: str, vix_regime: str, sector_rank: Optional[int], n_sectors: int,
    momentum_fallback: bool,
    sent_pts: Optional[Dict] = None,   # FIX FLOWS1: flow sentence gated on this
) -> Tuple[str, str]:
    if sent_pts is None:
        sent_pts = {}

    cur        = df.iloc[-1]
    price      = float(cur["Close"])
    rsi        = _num(cur, "RSI",          50)          # FIX SCORE-NAN
    sma20      = _num(cur, "SMA_20",       price)
    # Missing SMA_200 (<200 bars) must read as "not yet available", never as
    # a synthetic level the price happens to sit above (matches the neutral
    # SMA scoring in _score_technical).
    _sma200_raw = cur.get("SMA_200") if hasattr(cur, "get") else None
    sma200_ok  = _sma200_raw is not None and not pd.isna(_sma200_raw)
    sma200     = float(_sma200_raw) if sma200_ok else float("nan")
    # Same session the volume pillar scored (FIX VOL-PARTIAL): during market
    # hours that's the last completed day, not today's partial bar.
    _vr = vol_pts.get("vol_ratio_raw")
    vol        = float(_vr) if _vr is not None else _num(cur, "Volume_Ratio", 1.0)
    r20d       = mom_pts.get("_r20d", 0.0)
    # FIX SCORE-R60: _r60d is None when there is under 60 bars of history — the
    # sentence below must say so rather than format None or print a fake 0.0%.
    r60d       = mom_pts.get("_r60d")
    has_r60    = r60d is not None
    bull_pats  = [p for p in patterns if "⚠️" not in p]
    bear_pats  = [p for p in patterns if "⚠️" in p]
    short_name = ticker.replace(".NS", "")

    # ── Headline ─────────────────────────────────────────────────────────────
    if score >= 80:
        headline = "Strong uptrend — all indicators aligned"
    elif score >= 65:
        headline = "Healthy trend — most indicators aligned"
    elif score >= 52:
        headline = "Mixed signals — trend not yet confirmed"
    elif score >= 40:
        headline = "Consolidating — no clear edge right now"
    elif score >= 25:
        headline = "Weakening — momentum fading"
    else:
        headline = "Weak — downtrend with no reversal signal"

    if rsi < 30 and score > 40:
        headline = "Deeply oversold — stretched to the downside"
    if bull_pats:
        headline += f" ({bull_pats[0]} pattern)"

    # ── Narrative ─────────────────────────────────────────────────────────────
    parts = []

    # Sentence 1: Trend / SMA context
    if not sma200_ok:
        parts.append(
            f"{short_name} is trading at Rs.{price:,.2f}, "
            + ("above" if price > sma20 else "below")
            + f" its 20-day average (Rs.{sma20:,.2f}). The 200-day average is not yet "
            f"available (under 200 trading days of history), so the long-term trend "
            f"check scores neutral rather than being measured."
        )
    elif price > sma20 and price > sma200:
        parts.append(
            f"{short_name} is trading at Rs.{price:,.2f}, above both its "
            f"20-day average (Rs.{sma20:,.2f}) and 200-day average — the uptrend is intact."
        )
    elif price > sma200:
        pct_below_20 = (sma20 / price - 1) * 100
        parts.append(
            f"{short_name} at Rs.{price:,.2f} is in an overall uptrend (above 200-day average) "
            f"but has pulled back {abs(pct_below_20):.1f}% below its 20-day average."
        )
    else:
        parts.append(
            f"{short_name} at Rs.{price:,.2f} is below its long-term 200-day average "
            f"— the broader trend is currently down."
        )

    # Sentence 2: RSI
    if rsi < 30:
        parts.append(
            f"The stock has become deeply oversold (momentum indicator at {rsi:.0f}/100), "
            f"a stretched reading that has often preceded rebounds, though it can persist in downtrends."
        )
    elif rsi < 45:
        parts.append(
            f"The stock is in oversold territory (momentum at {rsi:.0f}/100), "
            f"suggesting selling pressure may be easing."
        )
    elif rsi <= 65:
        parts.append(
            f"Momentum is healthy at {rsi:.0f}/100 — neither overbought nor oversold, "
            f"suggesting the move has room to continue."
        )
    elif rsi <= 75:
        parts.append(
            f"Momentum is elevated ({rsi:.0f}/100) — the stock is running hot, "
            f"and extended readings like this often see a pause or pullback."
        )
    else:
        parts.append(
            f"Momentum is very high ({rsi:.0f}/100 — overbought zone). "
            f"Overbought readings can persist in strong trends but raise pullback risk."
        )

    # Sentence 3: Recent performance (or fallback note)
    if momentum_fallback:
        parts.append(
            "Momentum data is limited (fewer than 25 trading days of history available) "
            "— treat the momentum score as indicative only."
        )
    elif r20d > 10:
        if has_r60:
            parts.append(
                f"The stock has gained {r20d:.1f}% in the last month and {r60d:.1f}% "
                f"over 3 months — strong institutional interest."
            )
        else:
            parts.append(
                f"The stock has gained {r20d:.1f}% in the last month — strong buying "
                f"interest. Under 3 months of price history is available, so the "
                f"3-month momentum component is scored neutral rather than measured."
            )
    elif r20d > 3:
        parts.append(f"Returns over the last month: +{r20d:.1f}%. Steady accumulation phase.")
    elif r20d >= -3:
        parts.append(
            f"The stock has been largely flat over the past month ({r20d:+.1f}%), "
            f"consolidating after a prior move."
        )
    else:
        parts.append(
            f"The stock is down {abs(r20d):.1f}% over the past month — "
            f"sellers are currently in control."
        )

    # Sentence 3b: Relative Strength vs Nifty (FIX RS1) — only rendered when
    # RS_Score is present on the df (i.e. score_stock fetched a benchmark).
    _rs_val = mom_pts.get("rs_score")
    if _rs_val is not None:
        if _rs_val >= 80:
            parts.append(
                f"Relative strength vs Nifty is exceptional ({_rs_val:.0f}/100 percentile) "
                f"— outperforming the broad market decisively over the trailing year."
            )
        elif _rs_val >= 60:
            parts.append(
                f"Relative strength vs Nifty is strong ({_rs_val:.0f}/100 percentile) "
                f"— leading the broader market."
            )
        elif _rs_val >= 40:
            parts.append(
                f"Relative strength vs Nifty is neutral ({_rs_val:.0f}/100 percentile) "
                f"— tracking the broad market rather than leading it."
            )
        else:
            parts.append(
                f"Relative strength vs Nifty is weak ({_rs_val:.0f}/100 percentile) "
                f"— lagging the broad market; any absolute-return uptick here is beta, not skill."
            )

    # Sentence 4: Volume + pattern
    if vol > 1.5 and bull_pats:
        parts.append(
            f"Notably, today's volume is {vol:.1f}× the 20-day average with a "
            f"{bull_pats[0]} candlestick pattern — a positive confirmation signal."
        )
    elif vol > 1.5:
        parts.append(
            f"Trading volume is {vol:.1f}× the normal level, indicating above-average buyer activity."
        )
    elif bear_pats:
        parts.append(
            f"A {bear_pats[0].replace('⚠️','')} pattern appeared recently — "
            f"a warning sign that some selling may follow."
        )

    # Sentence 4b: Delivery % (FIX DELIV1) — only when the delivery snapshot
    # is present in vol_pts. Two branches: normal read vs divergence flag.
    if vol_pts.get("delivery_available"):
        _dt_today = vol_pts.get("delivery_today")
        _dt_mean  = vol_pts.get("delivery_mean")
        _dt_z     = vol_pts.get("delivery_z")
        _dt_n     = vol_pts.get("delivery_n", 0)
        if vol_pts.get("delivery_divergence"):
            parts.append(
                f"Delivery % is only {_dt_today:.0f}% today vs the {_dt_n}-day mean "
                f"of {_dt_mean:.0f}% ({_dt_z:+.1f}sigma) — the volume surge is "
                f"largely intraday, not institutional accumulation. Frothy moves "
                f"on low delivery tend to unwind."
            )
        elif _dt_today is not None and _dt_z is not None:
            if _dt_z > 1.0:
                parts.append(
                    f"Delivery % is {_dt_today:.0f}% today, sharply above the "
                    f"{_dt_n}-day mean of {_dt_mean:.0f}% ({_dt_z:+.1f}sigma) — "
                    f"institutional accumulation footprint."
                )
            elif _dt_today >= 45 and _dt_z > 0.25:
                parts.append(
                    f"Delivery % is {_dt_today:.0f}% today vs the {_dt_n}-day mean "
                    f"of {_dt_mean:.0f}% — above-normal institutional participation."
                )
            elif _dt_today < 25 and _dt_z < -0.5:
                parts.append(
                    f"Delivery % is {_dt_today:.0f}% today ({_dt_z:+.1f}sigma below the "
                    f"{_dt_n}-day mean) — dominated by intraday traders, thin real conviction."
                )

    # Sentence 5: Entry / SL / TP
    sl_pct = (sl / price - 1) * 100
    tp_pct = (tp / price - 1) * 100
    parts.append(
        f"Model reference levels: entry Rs.{entry:,.2f}, invalidation "
        f"Rs.{sl:,.2f} ({sl_pct:.1f}%), target Rs.{tp:,.2f} ({tp_pct:+.1f}%) "
        f"— a {rr:.1f}:1 risk-reward structure."
    )

    # Sentence 6: VIX + sector (thresholds derived from n_sectors)
    vix_sentence_map = {
        "complacency": "Market is calm (low VIX) — historically supportive, though complacency can precede volatility spikes.",
        "normal":      "Overall market conditions are normal (VIX in healthy range).",
        "elevated":    "Market fear is elevated (VIX rising) — price swings are wider than usual.",
        "fear":        "Market fear is high (VIX above 22) — volatility is well above normal.",
        "panic":       "Market is in panic mode (VIX above 28) — extreme volatility regime.",
    }
    vix_txt    = vix_sentence_map.get(vix_regime, "Market conditions are uncertain.")
    top_third  = n_sectors // 3
    mid_third  = 2 * n_sectors // 3
    if sector_rank is None:
        sector_txt = (f"The {sector} sector isn't in the current sector-strength ranking, "
                      "so it is treated as a neutral backdrop.")
    else:
        sector_txt = (
            f"The {sector} sector is currently ranked #{sector_rank} of {n_sectors} — "
            + ("a tailwind for this stock." if sector_rank <= top_third else
               "neutral sector backdrop."   if sector_rank <= mid_third else
               "a headwind — sector is underperforming.")
        )
    parts.append(f"{vix_txt} {sector_txt}")

    # Sentence 7: FII/DII 5-day flows (FIX FLOWS1) — only when available.
    # The sub-scorer records the two sums on `pts["fii_5d"] / dii_5d`; we
    # read them off the sentiment detail dict passed via `sent_pts` in
    # score_dataframe. Keep this to one sentence so the narrative stays
    # readable; the full flow analysis lives on the FII/DII Flows page.
    if sent_pts.get("flows_available"):
        _f = sent_pts.get("fii_5d", 0.0)
        _d = sent_pts.get("dii_5d", 0.0)
        if _f > 0 and _d > 0:
            _flow_txt = ("FII and DII are both net buyers over the last 5 sessions "
                         "— broad institutional participation, a tailwind.")
        elif _f < 0 and _d < 0:
            _flow_txt = ("FII and DII are both net sellers over the last 5 sessions "
                         "— institutional distribution, a headwind.")
        elif _f < 0 and _d > 0:
            _flow_txt = ("FII selling absorbed by DII buying (5-day) — a domestic-supported "
                         "dip, historically more often a pullback than a trend break.")
        elif _f > 0 and _d < 0:
            _flow_txt = ("FII buying with DII taking profits (5-day) — rallies in this regime "
                         "have tended to be shallower.")
        else:
            _flow_txt = None
        if _flow_txt:
            parts.append(_flow_txt)

    return headline, " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Main scoring functions
# ─────────────────────────────────────────────────────────────────────────────

def score_dataframe(
    df:           pd.DataFrame,
    ticker:       str,
    vix_info:     Optional[Dict] = None,
    sector_rank:  Optional[int] = 7,   # 7 of the default 15 = middle third; None = unranked
    sector:       str = "Other",
    n_sectors:    int = 15,
    dispersion:   Optional[float] = None,
    flows_info:   Optional[Dict] = None,
    delivery_info: Optional[Dict] = None,
    regime_label: Optional[str] = None,
    positioning_info: Optional[Dict] = None,
    tqs:                Optional[float] = None,   # FIX OVERLAY1: Task 3.3 sidecar input
    valuation_posture:  Optional[str]   = None,   # FIX OVERLAY1: Task 3.3 sidecar input
) -> "CompositeScore":
    """
    Score from a pre-fetched, indicator-enriched DataFrame.
    Call this when you already have the df to avoid redundant fetches.

    `dispersion` is the current cross-sectional 20-day return dispersion
    across the universe, as produced by
    analysis.regime.cross_sectional_dispersion_20d. When passed and below
    the low-dispersion threshold, the narrative for BUY/STRONG BUY signals
    gets a warning suffix — see FIX REGIME-DISP-SCORE below. Optional;
    callers that don't have it (e.g. single-stock ad-hoc scoring) can
    still pass None and get the previous behavior unchanged.
    """
    if vix_info is None:
        vix_info = {"regime": "normal", "vix": None, "allow_buy": True}

    tech_pts,  tech_detail = _score_technical(df)
    tech_raw = tech_pts
    # FIX BEAR-NEUTRAL (2026-09-28): in bear regimes (trend_down / risk_off,
    # same opt-out flag as the bear momentum variant) the Technical pillar is
    # held at its neutral midpoint (20 of 40). It rewards RSI 60-70, a bullish
    # SMA stack and strong trends, which rank backwards there. 10y study,
    # tech+RS neutral vs current, bear weeks: 20d IC 2017-20 -0.024 -> +0.026,
    # 2021-26 -0.005 -> +0.061; 60d also better in both halves.
    if (regime_label or "").lower() in _BEAR_REGIMES and _regime_weights_enabled():
        tech_detail["tech_pts_raw"]     = tech_pts
        tech_detail["tech_neutralised"] = True
        tech_pts = 20.0
    mom_pts,   mom_detail  = _score_momentum(df, regime_label=regime_label)
    vol_pts,   vol_detail  = _score_volume(df, delivery_info=delivery_info)
    pat_detail             = _detect_patterns(df)
    sent_pts,  sent_detail = _score_sentiment(vix_info, sector_rank, n_sectors,
                                              flows_info=flows_info)

    momentum_fallback = mom_detail.get("is_fallback", False)
    patterns          = pat_detail.get("patterns", [])

    # ── FIX POS2 (2026-09-06) — Positioning pillar aggregation, design 6b ──
    # Recommendation 6 design 6b: F&O-eligible tickers with the opt-in flag
    # set add positioning as a pure 10-pt overlay on top of the legacy
    # 40+25+15+10=90 shape - cap becomes 100 for those tickers. Everything
    # else (non-F&O tickers OR flag off) runs the legacy 40+25+15+10=90
    # shape unchanged. Tech and momentum sub-scores are NOT rescaled - they
    # keep their 0-40 and 0-25 outputs, so every existing calibration on
    # sub-scores stays valid. Grade / action thresholds stay on their 0-90
    # calibration too, so an F&O ticker with positioning data has a mild
    # additive upgrade path (up to +10) but a non-F&O ticker behaves
    # identically to today. Guardrail 5 shape change 6a -> 6b ratified by
    # user 2026-09-06 (see docs/COMPOSITE_SCORE_SHAPE_REVIEW.md D2);
    # nse-app-guardrails SKILL.md §5 updated in this landing.
    from data.fno_universe import is_fno_eligible as _is_fno
    _fno_flag  = _is_fno(ticker)
    _pi        = positioning_info or {}
    # Pillar activates only when the flag is ON, ticker is F&O-eligible,
    # AND at least one positioning input has real data. This prevents an
    # under-scored bias that would otherwise hit every F&O name the moment
    # the flag flips but before the data pipelines are online.
    _has_pos_data  = any(_pi.get(k) is not None for k in
                         ("oi_regime", "pcr", "max_pain_distance_pct",
                          "fii_deriv_net", "fii_deriv_pct"))
    _positioning_on  = _positioning_pillar_enabled() and _fno_flag and _has_pos_data
    positioning_pts: float = 0.0
    positioning_detail: Dict = {"pillar_active": False, "is_fno": _fno_flag}
    _base_total = tech_pts + mom_pts + vol_pts + sent_pts
    if _positioning_on:
        positioning_pts, positioning_detail = _score_positioning(
            oi_regime             = _pi.get("oi_regime"),
            pcr                   = _pi.get("pcr"),
            max_pain_distance_pct = _pi.get("max_pain_distance_pct"),
            fii_deriv_net         = _pi.get("fii_deriv_net"),
            fii_deriv_pct         = _pi.get("fii_deriv_pct"),
        )
        positioning_detail["pillar_active"] = True
        positioning_detail["is_fno"]        = True
        # Additive overlay - cap 100. Sub-scores unchanged.
        total = round(min(max(_base_total + positioning_pts, 0), 100.0), 1)
    else:
        total  = round(min(max(_base_total, 0), 90.0), 1)

    grade  = _grade(total)
    action = _action(total)

    entry, sl, tp, rr = _compute_entry_levels(
        df, total, vix_regime=vix_info.get("regime", "normal")
    )
    # FIX HZ2 (2026-09-04): pass regime + score so the horizon is per-stock
    # varied instead of a binary label.
    horizon_label, horizon_days = _pick_horizon(
        tech_pts, mom_pts,
        vix_regime=vix_info.get("regime"),
        score=total,
    )
    valid_until = (datetime.now() + timedelta(days=horizon_days)).date().isoformat()

    # FIX WL1: RSI/1-day-return were never exposed on CompositeScore, even
    # though RSI is already computed inside _score_technical() and both are
    # cheap to derive from the df already in scope here — no extra fetch.
    _cur = df.iloc[-1]
    rsi_val = float(_cur.get("RSI", 50))
    # FIX RS1: surface RS_Score on CompositeScore when present. None keeps
    # backwards-compat for callers that never enriched with a benchmark.
    _rs_raw = _cur.get("RS_Score")
    try:
        _rs_val = float(_rs_raw) if _rs_raw is not None else None
        if _rs_val is not None and math.isnan(_rs_val):
            _rs_val = None
    except (TypeError, ValueError):
        _rs_val = None
    _closes_1d = df["Close"].tail(2)
    return_1d_val = (
        float(_closes_1d.pct_change().iloc[-1]) * 100
        if len(_closes_1d) == 2 else 0.0
    )

    headline, narrative = _build_narrative(
        ticker=ticker, df=df, score=total, grade=grade, action=action,
        tech_pts=tech_detail, mom_pts=mom_detail, vol_pts=vol_detail,
        patterns=patterns,
        entry=entry, sl=sl, tp=tp, rr=rr,
        sector=sector, vix_regime=vix_info.get("regime", "normal"),
        sector_rank=sector_rank, n_sectors=n_sectors,
        momentum_fallback=momentum_fallback,
        sent_pts=sent_detail,   # FIX FLOWS1: flow sentence needs the detail dict
    )

    # FIX REGIME-DISP-SCORE — append the low-dispersion caution to the
    # narrative when the market is in a correlated (low-dispersion) regime
    # AND this is a BUY/STRONG BUY signal. That is the ONE stable filter
    # the Path B axis search produced (see analysis/regime.py). Kept as a
    # narrative append rather than a score deduction on purpose: the finding
    # is a hit-rate CAUTION, not a scoring re-weight (train/holdout show
    # different directions on the axis magnitude — see FIX REGIME-DISP for
    # the deliberate design choice not to overfit the effect direction).
    if dispersion is not None and action in ("BUY", "STRONG BUY"):
        try:
            from analysis.regime import dispersion_verdict as _dv
            _v = _dv(dispersion)
            if _v.get("zone") == "low" and _v.get("note"):
                narrative = narrative + " " + _v["note"]
        except Exception as _dispersion_e:
            _log.debug("dispersion narrative append skipped: %s", _dispersion_e)

    return CompositeScore(
        ticker            = ticker,
        price             = entry,
        score             = total,
        grade             = grade,
        action            = action,
        technical_score   = tech_pts,
        technical_raw     = tech_raw,
        momentum_score    = mom_pts,
        volume_score      = vol_pts,
        sentiment_score   = sent_pts,
        patterns_detected = patterns,
        momentum_fallback = momentum_fallback,
        horizon           = horizon_label,
        valid_until       = valid_until,
        rsi               = rsi_val,
        return_1d         = return_1d_val,
        rs_score          = _rs_val,
        positioning_score = positioning_pts if _positioning_on else None,
        is_fno            = _fno_flag,
        overlay_score     = compute_overlay_score(tqs, valuation_posture),
        entry             = entry,
        stop_loss         = sl,
        target            = tp,
        risk_reward       = rr,
        headline          = headline,
        narrative         = narrative,
        sector            = sector,
        vix_regime        = vix_info.get("regime", "normal"),
        sector_rank       = sector_rank,
    )


def score_stock(
    ticker:           str,
    period:           str = "2y",
    vix_info:         Optional[Dict] = None,
    sector_scores_df: Optional["pd.DataFrame"] = None,
) -> "CompositeScore":
    """
    Full end-to-end scoring for any NSE stock.
    Fetches data, adds indicators, computes composite score, returns CompositeScore.
    Pass vix_info and sector_scores_df when scoring many stocks to reuse shared data.
    """
    _setup_encoding()

    if not _load_deps():
        return CompositeScore(
            ticker=ticker, price=0.0, score=0.0, grade="F",
            action="DATA_UNAVAILABLE",
            technical_score=0, momentum_score=0, volume_score=0, sentiment_score=0,
            entry=0, stop_loss=0, target=0, risk_reward=0,
            headline="Engine dependencies unavailable",
            narrative="Could not load required modules. Check your Python path setup.",
            sector="Unknown", vix_regime="unknown", sector_rank=None,
        )

    # Resolve ticker — catches all exception types, not just ValueError
    try:
        canonical = _resolve_ticker(ticker)
    except Exception as e:
        canonical = ticker if ticker.endswith(".NS") else ticker + ".NS"
        _log.debug("ticker resolution failed for %r, falling back to %r: %s",
                   ticker, canonical, e)

    # VIX
    if vix_info is None:
        try:
            vix_info = _get_india_vix_regime() if _get_india_vix_regime else None
        except Exception as e:
            vix_info = None
            _log.debug("VIX regime lookup failed, defaulting to 'normal': %s", e)
        if vix_info is None:
            vix_info = {"regime": "normal", "vix": None, "allow_buy": True}

    # Sector + rank
    sector     = _get_sector(canonical)
    sector_rank, n_sectors = _resolve_sector_rank(sector, sector_scores_df)

    # Fetch + indicators
    try:
        df = _fetch_single(canonical, period=period)
        df = _add_all_indicators(df, groups=_SCORE_INDICATOR_GROUPS)

        # FIX RS1 (2026-09-03) — enrich with RS vs Nifty so _score_momentum
        # can use the abs:15 + RS:10 split. Best-effort: a benchmark-fetch
        # failure leaves the df without RS_Score, and momentum falls back
        # to the 25-pt absolute mode (documented in _score_momentum).
        #
        # FIX RS-CACHE (2026-09-04): the benchmark was being fetched INSIDE
        # every score_stock() call. Command Centre scans N tickers -> N
        # ^NSEI fetches. On network-blocked runs (CI page-smoke) each fetch
        # walks Angel/Stooq/Yahoo with per-provider timeouts -> the smoke
        # test for 02_command_centre hit its 120s ceiling. Cache the
        # per-(period) result at module level so we pay at most one fetch
        # per process per period; a failed fetch is remembered as None so
        # subsequent tickers short-circuit instead of retrying.
        try:
            _bench = _get_bench_cached(period)
            if _bench is not None and not _bench.empty:
                from utils.indicators import add_relative_strength
                df = add_relative_strength(df, _bench)
        except Exception as _rs_e:
            _log.debug("RS enrichment skipped for %s: %s: %s",
                       canonical, type(_rs_e).__name__, _rs_e)

        df.dropna(subset=["RSI", "ATR"], inplace=True)
        if len(df) < 30:
            raise ValueError(f"Insufficient data for {canonical}")
    except Exception as e:
        return CompositeScore(
            ticker=canonical, price=0.0, score=0.0, grade="F",
            action="DATA_UNAVAILABLE",
            technical_score=0, momentum_score=0, volume_score=0, sentiment_score=0,
            entry=0, stop_loss=0, target=0, risk_reward=0,
            headline=f"Data unavailable: {e}",
            narrative=f"Could not fetch data for {canonical}. Please check the ticker symbol.",
            sector=sector, vix_regime="unknown", sector_rank=sector_rank,
        )

    # FIX POS-OI1 (2026-09-03) — feed OI-regime sub-input to the Positioning
    # pillar (Rec 6 sub-score #1 of 4). Only useful when the ticker is
    # F&O-eligible AND the daily fno bhavcopy cron has populated at least
    # two rows for it AND we have today's equity price change. All three
    # are best-effort; a miss returns positioning_info=None which the
    # three-way gate in score_dataframe handles as "pillar stays inactive
    # for this ticker" (no bias).
    #
    # FIX POS-GATE (2026-09-04): also skip the DB reads entirely when the
    # opt-in flag is off (the default). Command Centre scans many tickers
    # per render; without this gate we did 3-4 SQL SELECTs per ticker even
    # though _score_dataframe would then throw the results away. That was
    # part of what pushed 02_command_centre's smoke past the 120s ceiling
    # on the slower CI runner.
    positioning_info: Optional[Dict] = None
    try:
        from data.fno_universe import is_fno_eligible as _fno_check
        if _fno_check(canonical) and _positioning_pillar_enabled():
            from data.nse_fno_bhavcopy import get_oi_regime_for_ticker as _oi_reg
            # Today's price change from the df we already have.
            _closes = df["Close"].tail(2)
            _px_pct = (
                float(_closes.pct_change().iloc[-1]) * 100
                if len(_closes) == 2 else None
            )
            _oi_regime = _oi_reg(canonical, _px_pct)
            # Positioning dict gets populated even with a single input;
            # the three-way gate in score_dataframe then activates the
            # pillar for this ticker.
            _pi_acc: Dict = {}
            if _oi_regime is not None:
                _pi_acc["oi_regime"] = _oi_regime
            # FII deriv net is UNIVERSE-LEVEL — one value for all F&O
            # tickers on a given day. Cheap to load per ticker (single
            # DB read + LRU-style cache implicit in the trade_store).
            try:
                from data.nse_fii_deriv import get_latest_fut_idx_net, get_latest_fut_idx_pct
                _fii_net = get_latest_fut_idx_net()
                if _fii_net is not None:
                    _pi_acc["fii_deriv_net"] = _fii_net
                _fii_pct = get_latest_fut_idx_pct()
                if _fii_pct is not None:
                    _pi_acc["fii_deriv_pct"] = _fii_pct
            except Exception as _fii_e:
                _log.debug("FII deriv net unavailable: %s: %s",
                           type(_fii_e).__name__, _fii_e)
            # PCR + max-pain % come from the same options-chain row (last
            # DB read per ticker). Both stay None gracefully when the
            # options-chain cron hasn't fetched this symbol yet.
            try:
                from data.nse_option_chain import get_pcr, get_max_pain_pct
                _pcr_v = get_pcr(canonical)
                _mp_v  = get_max_pain_pct(canonical)
                if _pcr_v is not None:
                    _pi_acc["pcr"] = _pcr_v
                if _mp_v is not None:
                    _pi_acc["max_pain_distance_pct"] = _mp_v
            except Exception as _oc_e:
                _log.debug("option chain unavailable for %s: %s: %s",
                           canonical, type(_oc_e).__name__, _oc_e)
            if _pi_acc:
                positioning_info = _pi_acc
    except Exception as _pos_e:
        _log.debug("positioning inputs unavailable for %s: %s: %s",
                   canonical, type(_pos_e).__name__, _pos_e)

    # FIX REGIME1 (2026-09-03) — best-effort regime snapshot for the
    # opt-in bear-regime momentum dispatch (Rec 5). regime_label stays None
    # when the snapshot fails or the flag is off; _score_momentum keeps
    # its default behavior in either case. Env-var opt-in prevents this
    # from changing production output until the 5-year variant study is
    # run (see docs/REGIME_WEIGHTS_2026-09.md).
    regime_label: Optional[str] = None
    if _regime_weights_enabled():
        regime_label = _live_regime_label()

    # FIX DELIV1 (2026-09-03) — load NSE bhavcopy delivery snapshot so the
    # Volume pillar can consume delivery % as a 4-pt sub-score inside its
    # 15-pt total. Best-effort: an empty local cache (fresh DB, bhavcopy
    # cron hasn't run yet) returns None, and _score_volume falls back to
    # its legacy vol_ratio:10 + obv:5 split. See docs/DELIVERY_INTEGRATION_2026-09.md.
    delivery_info: Optional[Dict] = None
    try:
        from data.nse_delivery import get_snapshot as _delv_snap
        delivery_info = _delv_snap(canonical, lookback=60)
    except Exception as _dv_e:
        _log.debug("NSE delivery snapshot unavailable for %s: %s: %s",
                   canonical, type(_dv_e).__name__, _dv_e)

    # FIX FLOWS1 (2026-09-03) — load FII/DII 5-day cash-market flows so the
    # Sentiment pillar can consume the sign. load_history() reads from the
    # trade_store cache the fii_dii cron populates; a miss here (no data yet,
    # DB unreachable) is best-effort: flows_info stays None and _score_sentiment
    # falls back to its legacy 6/4 split (documented in that function).
    #
    # FIX FLOWS-CACHE (2026-09-04): flows are universe-level (one value
    # applies to every ticker on the same day). Cache at the module level
    # so Command Centre's ticker sweep does a single DB read, not N.
    flows_info = _get_flows_cached()

    result = score_dataframe(
        df=df, ticker=canonical,
        vix_info=vix_info,
        sector_rank=sector_rank,
        sector=sector,
        n_sectors=n_sectors,
        flows_info=flows_info,
        delivery_info=delivery_info,
        regime_label=regime_label,
        positioning_info=positioning_info,
    )

    # Update entry to live price if Angel One is configured
    try:
        from data.angel_fetcher import get_live_quote, is_configured
        if is_configured():
            live = get_live_quote(canonical)
            if live and live["price"] > 0:
                live_price = live["price"]
                # FIX (entry/SL/TP staleness): stop_loss/target were computed
                # by _compute_entry_levels() against the previous CLOSE price.
                # Overwriting only entry/price with the live quote and leaving
                # SL/TP untouched could leave the displayed entry below its
                # own stop-loss, or a risk:reward ratio that no longer matches
                # the displayed numbers, any time the live price has moved
                # from the close (the normal case during market hours).
                # Preserve the ATR-based stop distance and conviction-scaled
                # target distance already computed, and re-anchor both to
                # the live price rather than recomputing from scratch (which
                # would need a fresh fetch + indicator pass).
                close_entry = result.entry
                risk_dist   = close_entry - result.stop_loss
                reward_dist = result.target - close_entry
                result.entry     = live_price
                result.price     = live_price
                result.stop_loss = round(live_price - risk_dist, 2)
                result.target    = round(live_price + reward_dist, 2)
                # risk_reward is a ratio of the two distances above, which
                # are unchanged — only re-anchored — so it stays valid as-is.
    except Exception as e:
        # live price update is best-effort — never fail the score, but log
        # so a broken Angel One connection isn't invisible during diagnosis.
        _log.debug("live price update skipped for %s: %s: %s",
                   canonical, type(e).__name__, e)

    return result

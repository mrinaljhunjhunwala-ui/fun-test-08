"""
dashboard/shared/data_health.py - unified provider health snapshot for the
Command Centre `data_health` panel. Ships Task 2.3 from tasks/plan.md.

Design decision: read the state each provider already exposes (is_configured,
get_last_diagnostic, circuit-breaker registers) rather than launching live
probes on every render. Rationale:

  * Command Centre re-runs on every Streamlit interaction. Live probes would
    burn 8-12 network calls per interaction and would themselves become the
    slow page. Users care about "did fetches THAT HAVE RUN succeed", not
    "is every provider alive right now".
  * The moment a page actually needs a provider, it calls the fetcher, the
    fetcher populates its diagnostic, and this panel picks it up on the next
    render. That's the honest signal.
  * Providers with no captured activity render as "no recent activity"
    rather than fake-healthy.

Streamlit-free (Guardrail 11): the collector and render helper are both pure.
The Command Centre call site wraps the returned HTML with st.markdown.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Callable, Dict, List, Optional


# Status vocabulary (kept tiny on purpose - grows only when a check needs it):
#   healthy       - last diagnostic ok, or provider is configured/ready
#   stale         - last diagnostic present but old (> 30 min)
#   degraded      - non-fatal issue (e.g., Stooq circuit breaker tripped)
#   unavailable   - provider not configured / dependency missing
#   idle          - no captured activity this session (not necessarily bad)
STATUS_HEALTHY     = "healthy"
STATUS_STALE       = "stale"
STATUS_DEGRADED    = "degraded"
STATUS_UNAVAILABLE = "unavailable"
STATUS_IDLE        = "idle"

_HEALTHY_STATUSES  = {STATUS_HEALTHY}
# Trouble = real, actionable issue (breaker open, request failing, snapshot too
# stale to trust). These earn the loud red section.
_TROUBLE_STATUSES  = {STATUS_STALE, STATUS_DEGRADED}
# Inactive = no signal to worry about: provider isn't wired up (missing creds,
# optional dependency not installed) or simply hasn't been exercised this
# session. Bucketing these with real failures was making a working dashboard
# look 7/9 broken. Kept visible so an operator can still see the roster, but
# rendered in the muted "neutral" tone rather than bear-red.
_INACTIVE_STATUSES = {STATUS_UNAVAILABLE, STATUS_IDLE}

_STALE_MINUTES = 30


@dataclass
class ProviderCheck:
    """One provider's current health snapshot."""
    name:            str                    # display name
    group:           str                    # "market" / "corp_info" / "news" / "options"
    status:          str                    # one of STATUS_*
    last_success_at: Optional[str] = None   # ISO string or None
    warnings:        int = 0
    note:            str = ""


# ─── individual probes (each returns a ProviderCheck; never raises) ──────────

def _parse_iso(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def _status_from_diagnostic(diag: Optional[dict],
                            *, unavailable_reason: str = "") -> tuple[str, Optional[str], int, str]:
    """Fold a get_last_diagnostic()-style dict into (status, iso_at, warnings, note)."""
    if not diag:
        if unavailable_reason:
            return STATUS_UNAVAILABLE, None, 0, unavailable_reason
        return STATUS_IDLE, None, 0, "no recent activity"

    at = str(diag.get("at") or "") or None
    ok = bool(diag.get("ok"))
    warnings = int(diag.get("warnings", 0) or 0)
    note = str(diag.get("reason") or diag.get("note") or "")

    if not ok:
        # any recorded non-ok call, however recent, reads as degraded
        return STATUS_DEGRADED, at, warnings, note

    ts = _parse_iso(at)
    if ts is not None:
        ref = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
        if abs((ref - ts).total_seconds()) > _STALE_MINUTES * 60:
            return STATUS_STALE, at, warnings, note
    return STATUS_HEALTHY, at, warnings, note


def _try(fn: Callable, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def probe_angel() -> ProviderCheck:
    """Angel One: configured plus last-fetch state from data.fetcher tier 0."""
    try:
        from data.angel_fetcher import is_configured
        from data.fetcher import get_last_diagnostic as _fetch_diag
    except ImportError:
        return ProviderCheck("Angel One SmartAPI", "market", STATUS_UNAVAILABLE,
                             note="diagnostic getter unavailable")
    if not _try(is_configured):
        return ProviderCheck("Angel One SmartAPI", "market", STATUS_UNAVAILABLE,
                             note="no ANGEL_* env / secret")
    diag = _try(_fetch_diag, "AngelOne") or {}
    if not diag:
        return ProviderCheck("Angel One SmartAPI", "market", STATUS_HEALTHY,
                             note="credentials present, no fetches yet")
    status, at, warnings, note = _status_from_diagnostic(diag)
    return ProviderCheck("Angel One SmartAPI", "market", status, at, warnings, note)


def probe_stooq() -> ProviderCheck:
    """Stooq: circuit-breaker state from fetcher module."""
    try:
        from data.fetcher import _STOOQ_BREAKER, _stooq_enabled
    except ImportError:
        return ProviderCheck("Stooq CSV", "market", STATUS_UNAVAILABLE,
                             note="diagnostic getter unavailable")
    if not _stooq_enabled():
        return ProviderCheck("Stooq CSV", "market", STATUS_UNAVAILABLE,
                             note="disabled: behind a JS browser check (set NSE_ENABLE_STOOQ=1 to retry)")
    fails = int(_STOOQ_BREAKER.get("consecutive_failures", 0))
    tripped_until = float(_STOOQ_BREAKER.get("tripped_until", 0.0))
    now = time.time()
    if tripped_until and now < tripped_until:
        remain = int(tripped_until - now)
        return ProviderCheck("Stooq CSV", "market", STATUS_DEGRADED,
                             warnings=fails,
                             note=f"circuit breaker open, retries in {remain}s")
    if fails > 0:
        return ProviderCheck("Stooq CSV", "market", STATUS_HEALTHY,
                             warnings=fails,
                             note=f"{fails} recent failure(s), breaker not tripped")
    return ProviderCheck("Stooq CSV", "market", STATUS_IDLE,
                         note="no recent activity")


def _probe_diagnostic_provider(display: str, group: str,
                               getter: Callable, canary_key: str,
                               unavailable_reason: str = "") -> ProviderCheck:
    diag = _try(getter, canary_key)
    status, at, warnings, note = _status_from_diagnostic(diag, unavailable_reason=unavailable_reason)
    return ProviderCheck(display, group, status, at, warnings, note)


def probe_nse_security_info() -> ProviderCheck:
    """NSE sec_list / EQUITY_L (Analyze Stock trading-status flags). A failed
    fetch only hides the flags, so surface it here instead of failing silent."""
    name = "NSE security status (series / bands)"
    try:
        from data.nse_security_info import status
        st_ = status()
    except Exception:
        return ProviderCheck(name, "corp_info", STATUS_UNAVAILABLE, note="status getter unavailable")
    if not st_["fetched_at"]:
        return ProviderCheck(name, "corp_info", STATUS_IDLE, note="not fetched yet in this process")
    at = datetime.fromtimestamp(st_["fetched_at"]).isoformat(timespec="seconds")
    if st_["ok"]:
        return ProviderCheck(name, "corp_info", STATUS_HEALTHY, at, note=f"{st_['symbols']:,} symbols")
    return ProviderCheck(name, "corp_info", STATUS_DEGRADED, None, warnings=1,
                         note="last fetch failed; trading-status flags hidden until it recovers")


def probe_nse_corp_info() -> ProviderCheck:
    try:
        from data.nse_corp_info import get_last_diagnostic
    except ImportError:
        return ProviderCheck("NSE corp-info", "corp_info", STATUS_UNAVAILABLE,
                             note="diagnostic getter unavailable")
    return _probe_diagnostic_provider("NSE corp-info", "corp_info",
                                      get_last_diagnostic, "RELIANCE.NS")


def probe_bse_corp_info() -> ProviderCheck:
    try:
        from data.bse_corp_info import get_last_diagnostic
    except ImportError:
        return ProviderCheck("BSE corp-info", "corp_info", STATUS_UNAVAILABLE,
                             note="diagnostic getter unavailable")
    diag = _try(get_last_diagnostic, "500325")
    if diag is None:
        # Package may not be installed; that's the dependency-gated deferral
        # documented in the audit (finding #5).
        return ProviderCheck("BSE corp-info", "corp_info", STATUS_UNAVAILABLE,
                             note="bse package not installed (GPLv3 pending)")
    status, at, warnings, note = _status_from_diagnostic(diag)
    return ProviderCheck("BSE corp-info", "corp_info", status, at, warnings, note)


def probe_news_feed() -> ProviderCheck:
    try:
        from data.news_feed import get_last_diagnostic
    except ImportError:
        return ProviderCheck("Google News RSS", "news", STATUS_UNAVAILABLE,
                             note="diagnostic getter unavailable")
    return _probe_diagnostic_provider("Google News RSS", "news",
                                      get_last_diagnostic, "RELIANCE")


def probe_nse_rss() -> ProviderCheck:
    try:
        from data.nse_rss_feeds import get_last_diagnostic
    except ImportError:
        return ProviderCheck("NSE RSS feeds", "news", STATUS_UNAVAILABLE,
                             note="diagnostic getter unavailable")
    # Poll all documented categories; if any populated, pick most-recent.
    categories = ("related_party_transactions", "reason_for_encumbrance",
                  "sast_regulation_29", "sast_regulation_31",
                  "corporate_governance", "insider_trading")
    latest_at: Optional[str] = None
    best_diag: Optional[dict] = None
    for cat in categories:
        diag = _try(get_last_diagnostic, cat)
        if not diag:
            continue
        at = str(diag.get("at") or "")
        if latest_at is None or at > latest_at:
            latest_at = at
            best_diag = diag
    status, at, warnings, note = _status_from_diagnostic(best_diag)
    return ProviderCheck("NSE RSS feeds", "news", status, at, warnings, note)


def probe_yahoo() -> ProviderCheck:
    """Yahoo v8 chart: tier-2 fetcher state per _record_provider_diagnostic."""
    try:
        from data.fetcher import get_last_diagnostic as _fetch_diag
    except ImportError:
        return ProviderCheck("Yahoo v8 chart", "market", STATUS_UNAVAILABLE,
                             note="diagnostic getter unavailable")
    diag = _try(_fetch_diag, "Yahoo") or {}
    if not diag:
        return ProviderCheck("Yahoo v8 chart", "market", STATUS_IDLE,
                             note="no recent activity")
    status, at, warnings, note = _status_from_diagnostic(diag)
    return ProviderCheck("Yahoo v8 chart", "market", status, at, warnings, note)


def probe_vix() -> ProviderCheck:
    """India VIX: last fetch diagnostic from utils.vix."""
    try:
        from utils.vix import get_last_diagnostic
    except ImportError:
        return ProviderCheck("India VIX", "market", STATUS_UNAVAILABLE,
                             note="diagnostic getter unavailable")
    diag = _try(get_last_diagnostic) or {}
    if not diag:
        return ProviderCheck("India VIX", "market", STATUS_IDLE,
                             note="no recent activity")
    status, at, warnings, note = _status_from_diagnostic(diag)
    return ProviderCheck("India VIX", "market", status, at, warnings, note)


def probe_nse_delivery() -> ProviderCheck:
    """NSE bhavcopy delivery: last fetch_and_persist_today diagnostic."""
    try:
        from data.nse_delivery import get_last_diagnostic
    except ImportError:
        return ProviderCheck("NSE bhavcopy delivery", "market", STATUS_UNAVAILABLE,
                             note="diagnostic getter unavailable")
    diag = _try(get_last_diagnostic) or {}
    if not diag:
        return ProviderCheck("NSE bhavcopy delivery", "market", STATUS_IDLE,
                             note="no recent activity")
    status, at, warnings, note = _status_from_diagnostic(diag)
    return ProviderCheck("NSE bhavcopy delivery", "market", status, at, warnings, note)


def _probe_warmer_snapshot(name: str, kv_key: str, kv_user: str,
                           max_age_seconds: int) -> ProviderCheck:
    """Task 2.4 F1: read a trade_store snapshot's generated_at and report
    whether the scheduled warmer is keeping it fresh.

    This is NOT a provider fetch - it inspects the persisted snapshot the
    GitHub Actions warmer writes. Purpose: surface a silent warmer failure
    (cron missed, workflow error, DATABASE_URL misconfigured) BEFORE the
    user hits a cold-scan latency spike on the next visit.

    healthy   - snapshot exists and generated_at is within max_age_seconds.
    stale     - snapshot exists but older than max_age_seconds.
    idle      - snapshot missing entirely (fresh deploy, or warmer never ran).
    unavailable - trade_store import/read raised.
    """
    group = "warmer"
    try:
        import trade_store as _store   # local import: keeps module Streamlit-free
    except ImportError as _e:
        return ProviderCheck(name, group, STATUS_UNAVAILABLE,
                             note=f"trade_store import failed: {type(_e).__name__}")
    try:
        snap = _store.kv_get(kv_key, user_id=kv_user)
    except Exception as _e:
        return ProviderCheck(name, group, STATUS_UNAVAILABLE,
                             note=f"kv_get failed: {type(_e).__name__}")
    if not snap or not isinstance(snap, dict):
        return ProviderCheck(name, group, STATUS_IDLE,
                             note="no snapshot yet (warmer has not run)")
    gen_at = snap.get("generated_at")
    if not gen_at:
        return ProviderCheck(name, group, STATUS_IDLE, note="snapshot present but no generated_at")
    from utils.snapshot_time import snapshot_age_seconds  # not via trade_store — see cache.py
    age = snapshot_age_seconds(gen_at)
    if age is None:
        return ProviderCheck(name, group, STATUS_IDLE, gen_at, 0,
                             note=f"unparseable generated_at: {gen_at!r}")
    if age <= max_age_seconds:
        return ProviderCheck(name, group, STATUS_HEALTHY, gen_at, 0,
                             note="warmer on schedule")
    # Older than the app's max-age tolerance: on the next visit the app
    # falls back to a live scan, which is the cold-latency situation.
    hours_late = int(age // 3600)
    return ProviderCheck(name, group, STATUS_STALE, gen_at, 1,
                         note=f"snapshot {hours_late}h+ old, cold scan risk")


def probe_warmer_top_picks() -> ProviderCheck:
    """Task 2.4 F1: freshness of the Top Picks warmer snapshot."""
    # KV constants intentionally hard-coded here (not imported from
    # dashboard/shared/cache.py) to keep this module Streamlit-free.
    # If either constant changes in cache.py, mirror it here. Two-line
    # duplication is acceptable in return for module-purity guardrail.
    return _probe_warmer_snapshot(
        "Warmer: Top Picks", "top_picks_snapshot", "_system",
        max_age_seconds=1800,   # 30 min: covers the 15-min cron cadence + one miss
    )


def probe_warmer_tomorrow_watchlist() -> ProviderCheck:
    """Task 2.4 F1: freshness of the Tomorrow's Watchlist warmer snapshot."""
    return _probe_warmer_snapshot(
        "Warmer: Tomorrow's Watchlist", "tomorrow_watchlist_snapshot", "_system",
        max_age_seconds=24 * 3600,   # 24h: post-close daily, pre-open retry, and pre-open scan
    )


# ─── stored end-of-day datasets (tables populated by out-of-app jobs) ─────────
# FIX HEALTH-EOD (2026-09-26) — the probes above only see fetches made in THIS
# process, so a refresh job that stopped running was invisible: the scoring
# pillars kept silently reading week-old FII / OI / PCR / delivery rows. These
# probes read MAX(date) per table and count NSE sessions behind the latest
# completed session. (display, table, how to refresh it)
_STORED_DATASETS = (
    ("FII/DII cash flows",       "fii_dii_daily",          "page 22 refresh"),
    ("FII index futures",        "nse_fii_deriv_daily",    "py -m scripts.fetch_nse_fii_deriv"),
    ("F&O open interest",        "nse_fno_oi_daily",       "py -m scripts.fetch_nse_fno_bhavcopy"),
    ("Option chain PCR / pain",  "nse_option_chain_daily", "py -m scripts.fetch_nse_option_chain --fno-all"),
    ("Delivery % (bhavcopy)",    "nse_delivery_daily",     "py -m scripts.fetch_nse_delivery"),
)
_EOD_READY_IST = dtime(20, 0)       # the refresh jobs are scheduled 19:15-19:30 IST
_STORED_MAX_LAG_SESSIONS = 1        # one session of grace for a late or retried job
_STORED_TTL_S = 600                 # one DB round-trip per 10 min, not per render
_STORED_CACHE: Dict[str, object] = {}
_TABLE_MISSING = "__missing__"


def _latest_stored_dates() -> Optional[Dict[str, Optional[str]]]:
    """{table: latest date string | None (empty) | _TABLE_MISSING}, or None when
    the database itself is unreachable. Cached for _STORED_TTL_S."""
    now = time.time()
    hit = _STORED_CACHE.get("v")
    if hit is not None and now - float(_STORED_CACHE.get("t", 0.0)) < _STORED_TTL_S:
        return hit[0]  # type: ignore[index]
    try:
        import trade_store as _store   # local import: keeps module Streamlit-free
        out: Dict[str, Optional[str]] = {}
        with _store._get_conn() as conn:
            for _name, table, _how in _STORED_DATASETS:
                cur = conn.cursor()
                try:
                    cur.execute(f"SELECT MAX(date) FROM {table}")
                    row = cur.fetchone()
                    out[table] = str(row[0])[:10] if row and row[0] else None
                except Exception:
                    # Table not created yet. Roll back so a pooled Postgres
                    # connection is not handed back in an aborted transaction.
                    out[table] = _TABLE_MISSING
                    _try(conn.rollback)
            _try(conn.rollback)        # read-only: leave the pooled conn clean
        result: Optional[Dict[str, Optional[str]]] = out
    except Exception:
        result = None
    _STORED_CACHE["v"] = (result,)
    _STORED_CACHE["t"] = now
    return result


def _reference_session(now_ist: Optional[datetime] = None) -> date:
    """Latest NSE session whose end-of-day data should already be stored."""
    from dashboard.shared.market_hours import is_trading_day, prev_trading_day, _now_ist
    now_ist = now_ist or _now_ist()
    d = now_ist.date()
    if is_trading_day(d) and now_ist.time() >= _EOD_READY_IST:
        return d
    return prev_trading_day(d)


def _sessions_behind(latest: date, ref: date) -> int:
    """Trading sessions in (latest, ref] - 0 when the latest row covers ref."""
    from dashboard.shared.market_hours import is_trading_day
    n, d = 0, latest + timedelta(days=1)
    while d <= ref:
        n += is_trading_day(d)
        d += timedelta(days=1)
    return n


def probe_stored_datasets(now_ist: Optional[datetime] = None) -> List[ProviderCheck]:
    """One check per stored EOD dataset: healthy / stale / idle / unavailable."""
    group = "eod store"
    dates = _latest_stored_dates()
    if dates is None:
        return [ProviderCheck(name, group, STATUS_UNAVAILABLE, note="database unreachable")
                for name, _t, _h in _STORED_DATASETS]
    ref = _reference_session(now_ist)
    checks: List[ProviderCheck] = []
    for name, table, how in _STORED_DATASETS:
        latest = dates.get(table)
        if latest == _TABLE_MISSING or not latest:
            checks.append(ProviderCheck(name, group, STATUS_IDLE,
                                        note=f"no rows yet - refresh: {how}"))
            continue
        try:
            latest_d = date.fromisoformat(latest)
        except ValueError:
            checks.append(ProviderCheck(name, group, STATUS_IDLE, latest,
                                        note=f"unparseable date {latest!r}"))
            continue
        lag = _sessions_behind(latest_d, ref)
        if lag <= _STORED_MAX_LAG_SESSIONS:
            checks.append(ProviderCheck(name, group, STATUS_HEALTHY, latest,
                                        note=f"latest {latest}"))
        else:
            checks.append(ProviderCheck(name, group, STATUS_STALE, latest, lag,
                                        note=f"latest {latest}, {lag} sessions behind - refresh: {how}"))
    return checks


def collect_all_health() -> List[ProviderCheck]:
    """Every probe. Pure: no network, no Streamlit."""
    return [
        probe_angel(),
        probe_stooq(),
        probe_yahoo(),
        probe_vix(),
        probe_nse_delivery(),
        probe_nse_corp_info(),
        probe_nse_security_info(),
        probe_bse_corp_info(),
        probe_news_feed(),
        probe_nse_rss(),
        # Task 2.4 F1: warmer freshness surfaces silent cron failures before
        # they become user-visible cold-scan latency spikes.
        probe_warmer_top_picks(),
        probe_warmer_tomorrow_watchlist(),
        # FIX HEALTH-EOD: stored FII/DII, FII-deriv, OI, option-chain, delivery.
        *probe_stored_datasets(),
    ]


# ─── rendering (pure HTML string; caller stamps via st.markdown) ─────────────

def _pill(status: str) -> str:
    """Small colored pill matching the status."""
    tone = {
        STATUS_HEALTHY:     ("--bull",   "✓ healthy"),
        STATUS_STALE:       ("--amber",  "○ stale"),
        STATUS_DEGRADED:    ("--bear",   "▲ degraded"),
        STATUS_UNAVAILABLE: ("--dim",    "✗ offline"),
        STATUS_IDLE:        ("--dim",    "· idle"),
    }.get(status, ("--dim", status))
    color, label = tone
    return (
        f'<span style="display:inline-flex;align-items:center;'
        f'padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;'
        f'letter-spacing:.3px;background:color-mix(in srgb, var({color}) 14%, transparent);'
        f'color:var({color});border:1px solid var({color})">{label}</span>'
    )


def _relative_time(iso: Optional[str]) -> str:
    ts = _parse_iso(iso)
    if ts is None:
        return "-"
    ref = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
    delta = ref - ts
    secs = int(abs(delta.total_seconds()))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def render_data_health_html(checks: Optional[List[ProviderCheck]] = None) -> str:
    """Compose the two-row `data_health` block: providers up / providers stale.

    Uses panel() from ui_components. Returns raw HTML; Streamlit caller
    should st.markdown(..., unsafe_allow_html=True).
    """
    from dashboard.shared.ui_components import panel

    if checks is None:
        checks = collect_all_health()

    up       = [c for c in checks if c.status in _HEALTHY_STATUSES]
    trouble  = [c for c in checks if c.status in _TROUBLE_STATUSES]
    inactive = [c for c in checks if c.status in _INACTIVE_STATUSES]

    def _row(c: ProviderCheck) -> str:
        rel = _relative_time(c.last_success_at)
        w_str = f' &middot; <span style="color:var(--amber)">{c.warnings} warn</span>' if c.warnings else ""
        note_html = f' &middot; <span style="color:var(--dim);font-size:11px">{c.note}</span>' if c.note else ""
        return (
            f'<div style="display:flex;align-items:center;justify-content:space-between;'
            f'gap:12px;padding:6px 0;border-bottom:1px solid var(--hairline-soft)">'
            f'<div style="display:flex;align-items:center;gap:10px;min-width:0">'
            f'  <span style="color:var(--ink);font-weight:600;font-size:13px">{c.name}</span>'
            f'  <span style="color:var(--dim);font-size:11px">[{c.group}]</span>'
            f'</div>'
            f'<div style="display:flex;align-items:center;gap:10px;font-size:12px;color:var(--ink-mid)">'
            f'  {_pill(c.status)}'
            f'  <span style="font-family:var(--font-mono);color:var(--dim)">{rel}</span>'
            f'  {w_str}{note_html}'
            f'</div>'
            f'</div>'
        )

    def _section(title: str, tone: str, rows: List[ProviderCheck]) -> str:
        if not rows:
            return ""
        body = "".join(_row(c) for c in rows)
        return panel(
            f'<div style="font-family:var(--font-mono);font-size:10px;'
            f'letter-spacing:1.2px;text-transform:uppercase;color:var(--dim);'
            f'font-weight:600;margin-bottom:6px">{title} &middot; {len(rows)}</div>'
            f'{body}',
            kind="flat", tone=tone, margin="6px 0",
        )

    parts = [
        _section("providers live",   "bull",    up),
        _section("needs attention",  "bear",    trouble),
        _section("inactive",         "neutral", inactive),
    ]
    if not any(parts):
        # Nothing to show; render an empty-state panel for symmetry.
        return panel(
            '<div style="color:var(--dim);font-size:12px">No provider health signal captured yet '
            '(nothing has been fetched this session).</div>',
            kind="flat", tone="neutral", margin="6px 0",
        )
    return "".join(p for p in parts if p)

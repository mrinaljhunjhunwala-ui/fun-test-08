"""
scripts/warm_top_picks.py — Scheduled Top Picks pre-warm job.

FIX SPEED1. Runs on a schedule (GitHub Actions — see
.github/workflows/warm-top-picks.yml) and writes a fresh Top Picks scan
result to trade_store (shared Postgres, set via DATABASE_URL) so the
deployed app can read it instantly instead of paying the ~2-minute
cold-scan cost on whichever visit happens to hit an expired in-app cache.

Why this exists: dashboard/shared/cache.py's _home_top_picks() scans the
full niftytotalmarket universe (~745 tickers) and can take up to ~2 minutes
cold. It's cached with @st.cache_data(ttl=300), but that cache is
process-local to the running Streamlit Cloud container — it doesn't survive
a redeploy/restart, and on a low-traffic app it routinely expires between
visits. Whoever's browser session happens to hit the expired cache pays the
full cold scan.

This script runs the same scan headlessly and writes the result to
trade_store, which GitHub Actions and the deployed app both have real
network access to. The app's get_top_picks() reads this snapshot first and
only falls back to its own live scan if the snapshot is missing or older
than _TOP_PICKS_MAX_AGE_SECONDS — see dashboard/shared/cache.py.

NOTE: this deliberately imports dashboard.shared.cache and calls
_home_top_picks() directly (with the same vix_regime/sector_ranks inputs
get_top_picks() itself would use), plus reuses the module's own KV key
constants, rather than re-implementing the scan/persist logic here — so
there is exactly one place that logic lives and this can never drift from
what the app itself would compute. Same discipline as
warm_tomorrow_watchlist.py.

Exit codes: always 0 — a failed warm-up should not fail the workflow or
block the next scheduled attempt; the app already degrades gracefully to a
live scan if no fresh snapshot is available.
"""
from __future__ import annotations

import datetime
import logging
import os
import sys
import threading

_log = logging.getLogger("scripts.warm_top_picks")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception as _enc_e:
    print(f"[startup] stdout reconfigure skipped: {_enc_e}")

# Make project root importable (script lives in <root>/scripts/)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> int:
    try:
        from dashboard.shared.cache import (
            _home_top_picks,
            _TOP_PICKS_KV_KEY,
            _TOP_PICKS_KV_USER,
            _ALL_SCORES_KV_KEY,   # FIX WL-SNAP1
            _MOMENTUM_KV_KEY,
            get_vix_info,
            _sector_ranks_tuple,
        )
        from dashboard.shared.market_hours import is_trading_day
        import trade_store as store
    except Exception as e:
        _log.error("warm_top_picks: import failed, aborting this run: %s", e)
        return 0

    # FIX HOL1: the workflow's cron is weekday-only (Mon-Fri), which does NOT
    # account for NSE holidays that fall on a weekday (Republic Day, Holi,
    # Diwali, etc. — see dashboard/shared/market_hours.py's NSE_HOLIDAYS,
    # the single canonical calendar every other market-hours check in this
    # repo already delegates to). Without this guard, a holiday still burns
    # a full ~745-ticker scan every 15 minutes for ~8 hours, producing an
    # unchanging snapshot (nothing traded) while writing a timestamp that
    # misleadingly looks freshly updated. Checks the DAY only (not
    # time-of-day) since this job deliberately also runs pre-open/post-close.
    if not is_trading_day():
        _log.info("warm_top_picks: not a trading day (weekend or NSE holiday) — skipping scan.")
        return 0

    _log.info("warm_top_picks: starting scan...")
    _t0 = datetime.datetime.now()
    # FIX TP-HEARTBEAT (2026-09-28): the scan logs nothing until it finishes,
    # so a run killed by the job timeout looked like a silent hang. Log the
    # elapsed time every minute so the Actions log shows it was still working.
    _hb_stop = threading.Event()

    def _heartbeat() -> None:
        while not _hb_stop.wait(60):
            _log.info("warm_top_picks: scan still running (%.0fs elapsed)",
                      (datetime.datetime.now() - _t0).total_seconds())

    threading.Thread(target=_heartbeat, name="warm-heartbeat", daemon=True).start()
    try:
        _vix = get_vix_info()
        _sectors = _sector_ranks_tuple()
        result = _home_top_picks(
            vix_regime=_vix.get("regime", "normal"),
            n=20,
            sector_ranks=_sectors,
        )
    except Exception as e:
        _log.error("warm_top_picks: scan failed, nothing persisted this run: %s", e)
        return 0
    finally:
        _hb_stop.set()
    _elapsed = (datetime.datetime.now() - _t0).total_seconds()
    # FIX TP-HEALTH1: include scan-health counts (n_scanned / n_scored_ok /
    # n_unavailable) and the vix_regime the scan ran under, so a degraded run
    # is visible in the Actions log without needing to open the app. e.g.
    # "buys=3 sells=1 scanned=745 ok=612 unavailable=133 (17.9%) regime=fear"
    # would immediately flag a Stooq/Yahoo/Angel outage even though the
    # snapshot itself still gets written (empty-ish, but written).
    _meta = result.get("meta", {}) if isinstance(result, dict) else {}
    _n_scan = int(_meta.get("n_scanned", 0) or 0)
    _n_ok   = int(_meta.get("n_scored_ok", 0) or 0)
    _n_un   = int(_meta.get("n_unavailable", 0) or 0)
    _un_pct = (100.0 * _n_un / _n_scan) if _n_scan else 0.0
    _log.info(
        "warm_top_picks: scan done in %.1fs — buys=%d sells=%d scanned=%d "
        "ok=%d unavailable=%d (%.1f%%) regime=%s",
        _elapsed,
        len(result.get("buys", [])),
        len(result.get("sells", [])),
        _n_scan, _n_ok, _n_un, _un_pct,
        _vix.get("regime", "?"),
    )

    _gen_at = store.snapshot_timestamp()

    # FIX WL-SNAP1: pull the full-universe scored map out of the result and
    # persist it to a separate KV entry. This is what powers the watchlist
    # fast-path (dashboard/shared/cache._persisted_all_scores_snapshot) so
    # a My Watchlist page load whose tickers are all in the niftytotalmarket
    # universe pays zero live-scoring cost. Kept in a separate KV entry so
    # top-picks readers (called every render on Command Centre) don't drag
    # this larger payload around.
    _all_scored = result.pop("all_scored", {}) if isinstance(result, dict) else {}
    _momentum = result.pop("momentum_leaders", None) if isinstance(result, dict) else None

    snapshot = {
        "data": result,
        "generated_at": _gen_at,
        "scan_seconds": round(_elapsed, 1),
    }
    ok = store.kv_set(_TOP_PICKS_KV_KEY, snapshot, user_id=_TOP_PICKS_KV_USER)
    if ok:
        _log.info("warm_top_picks: persisted top-picks snapshot to trade_store (backend=%s)",
                  store.backend_name())
    else:
        _log.error("warm_top_picks: top-picks kv_set FAILED — snapshot not persisted "
                   "this run (app will fall back to a live scan)")

    # Second write: the full scored map. Failure here is non-fatal — the app
    # already degrades gracefully by falling back to live per-ticker scoring
    # for any watchlist entry not covered by this snapshot.
    all_scored_snapshot = {
        "data": _all_scored,
        "generated_at": _gen_at,
        "n_scored": len(_all_scored),
    }
    ok2 = store.kv_set(_ALL_SCORES_KV_KEY, all_scored_snapshot, user_id=_TOP_PICKS_KV_USER)
    if ok2:
        _log.info("warm_top_picks: persisted all-scored map (%d tickers) to trade_store",
                  len(_all_scored))
    else:
        _log.error("warm_top_picks: all-scored kv_set FAILED — watchlist page will fall "
                   "back to live per-ticker scoring this cycle")

    # Third write: momentum leaders (regime-independent ranking, same frames).
    if isinstance(_momentum, dict):
        ok3 = store.kv_set(_MOMENTUM_KV_KEY, {"data": _momentum, "generated_at": _gen_at},
                           user_id=_TOP_PICKS_KV_USER)
        _log.info("warm_top_picks: momentum leaders (%d) persisted=%s, ledger rows sent=%d",
                  len(_momentum.get("leaders", [])), ok3,
                  log_momentum_leaders(_momentum.get("leaders", [])))

    _n_sent = log_shadow_picks(result.get("buys", []) if isinstance(result, dict) else [])
    _log.info("warm_top_picks: shadow ledger — %d pick(s) sent (repeats in a session are no-ops)",
              _n_sent)
    return 0


def log_shadow_picks(buys) -> int:
    """Log each Top Picks card to verdict_log (source "shadow_auto"), exactly
    as Command Centre does when it renders the card. That page call was the
    only writer, so a day nobody opened Command Centre (6 Oct 2026: 28 scans,
    0 rows) left a hole in the live track record. log_verdict dedupes per
    (session, ticker, horizon, source), so page views and repeat scans add
    nothing twice; the first scan of the session sets the entry price."""
    from dashboard.shared.pick_freshness import compose_finalverdict_for_card

    n = 0
    for b in buys or []:
        try:
            compose_finalverdict_for_card(b, tqs=None)   # logs as "shadow_auto"
            n += 1
        except Exception as e:
            _log.debug("shadow log skipped %s: %s", b.get("ticker"), e)
    return n



def log_momentum_leaders(leaders) -> int:
    """Log each momentum leader to verdict_log under its OWN source,
    "momentum_leaders" (never "shadow_auto"), so the list's follow-through can
    be measured separately without touching the composite track record. The
    posture logged is the composite's; the momentum rank rides as a tag.
    Deduped per (session, ticker, horizon, source) by log_verdict."""
    from analysis.final_verdict import combine
    from analysis.verdict_ledger import log_verdict

    n = 0
    for r in leaders or []:
        try:
            fv = combine(composite_score=r.get("composite_score"),
                         composite_action=r.get("composite_action"))
            log_verdict(ticker=r["ticker"], final_verdict=fv,
                        entry_price=r.get("price"),
                        composite_score=r.get("composite_score"),
                        signal_tags=[f"momentum:rank{r.get('rank')}",
                                     f"momentum:score{int(r.get('momentum_score') or 0)}"],
                        source="momentum_leaders")
            n += 1
        except Exception as e:
            _log.debug("momentum ledger skipped %s: %s", r.get("ticker"), e)
    return n


if __name__ == "__main__":
    sys.exit(main())

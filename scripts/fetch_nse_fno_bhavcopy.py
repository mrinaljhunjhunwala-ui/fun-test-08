"""
scripts/fetch_nse_fno_bhavcopy.py — NSE F&O bhavcopy fetcher.

Feeds the OI-regime sub-score inside Recommendation 6's Positioning pillar.
See docs/POSITIONING_INTEGRATION_2026-09.md.

MUST BE RUN FROM A RESIDENTIAL IP — not GitHub Actions, not Streamlit
Cloud. Same NSE-WAF constraint as scripts/fetch_nse_delivery.py and
scripts/refresh_qualitative_flags.py.

USAGE

    py -m scripts.fetch_nse_fno_bhavcopy                # today
    py -m scripts.fetch_nse_fno_bhavcopy --date 2026-09-02
    py -m scripts.fetch_nse_fno_bhavcopy --days 30      # backfill
    py -m scripts.fetch_nse_fno_bhavcopy --days 30 --end 2026-09-01

Same rate-limit and retry discipline as scripts/fetch_nse_delivery.py:
randomised 1.5-3.5s between requests, 2 retries per date with backoff.
Weekends are skipped; holidays are handled by 404 -> graceful log-and-skip.

EXIT CODES

  0  All requested days written OK (or already present as no-ops).
  1  At least one requested day failed after the retry budget.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import random
import sys
import time
from typing import List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data.nse_fno_bhavcopy import (   # noqa: E402
    _fetch_bhavcopy, _parse_bhavcopy, _persist, ensure_schema,
)
from data.fno_universe import check_universe_drift   # noqa: E402

_log = logging.getLogger("scripts.fetch_nse_fno_bhavcopy")


def _is_weekend(d: _dt.date) -> bool:
    return d.weekday() >= 5


def _daterange(end: _dt.date, days: int) -> List[_dt.date]:
    out: List[_dt.date] = []
    cursor = end
    while len(out) < days:
        if not _is_weekend(cursor):
            out.append(cursor)
        cursor -= _dt.timedelta(days=1)
    return list(reversed(out))


def _fetch_one_day(date: _dt.date, max_retries: int = 2) -> int:
    for attempt in range(max_retries + 1):
        try:
            csv_text = _fetch_bhavcopy(date)
            rows = _parse_bhavcopy(csv_text, date)
            written = _persist(rows)
            _log.info("%s: %d symbols written", date.isoformat(), written)
            return written
        except ValueError as e:
            msg = str(e)
            if "not available" in msg or "404" in msg:
                _log.info("%s: not published (holiday or too early) — skipped",
                          date.isoformat())
                return 0
            _log.warning("%s: parse/fetch error on attempt %d/%d: %s",
                         date.isoformat(), attempt + 1, max_retries + 1, e)
        except Exception as e:
            _log.warning("%s: unexpected error on attempt %d/%d: %s",
                         date.isoformat(), attempt + 1, max_retries + 1, e)
        if attempt < max_retries:
            time.sleep(random.uniform(3.0, 6.0))
    _log.error("%s: gave up after %d retries", date.isoformat(), max_retries + 1)
    return -1


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch NSE F&O bhavcopy Open-Interest data into the shared trade_store."
    )
    p.add_argument("--date", type=str, default=None,
                   help="Single YYYY-MM-DD to fetch (mutually exclusive with --days)")
    p.add_argument("--days", type=int, default=1,
                   help="Fetch the N most-recent weekdays ending at --end (default 1)")
    p.add_argument("--end", type=str, default=None,
                   help="YYYY-MM-DD end date for --days (default today)")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )
    ensure_schema()

    if args.date:
        dates = [_dt.date.fromisoformat(args.date)]
    else:
        end = _dt.date.fromisoformat(args.end) if args.end else _dt.date.today()
        dates = _daterange(end, args.days)

    _log.info("fetching %d date(s): %s .. %s",
              len(dates), dates[0].isoformat(), dates[-1].isoformat())

    failed = 0
    for i, d in enumerate(dates):
        rc = _fetch_one_day(d)
        if rc < 0:
            failed += 1
        if i < len(dates) - 1:
            time.sleep(random.uniform(1.5, 3.5))

    _log.info("done. requested=%d, failed=%d", len(dates), failed)

    # Drift check — warn when data/fno_universe.py has fallen out of sync
    # with the actual NSE F&O universe. Prevents the "WAF-blocked" red
    # herring where the fetcher iterates symbols NSE has already delisted.
    try:
        drift = check_universe_drift()
        if drift["db_date"]:
            if drift["stale"]:
                _log.warning(
                    "fno_universe drift: %d symbol(s) in _FNO_TICKERS but NOT in "
                    "bhavcopy %s — re-run tools/refresh_fno_universe.py: %s",
                    len(drift["stale"]), drift["db_date"],
                    ", ".join(sorted(drift["stale"])),
                )
            if drift["missing"]:
                # _FNO_TICKERS is NSE's official list (data/nse_fno_underlyings.csv);
                # a gap means the CSV is older than the bhavcopy.
                _log.warning(
                    "fno_universe: %d F&O symbol(s) not in data/nse_fno_underlyings.csv "
                    "— re-run tools/refresh_fno_universe.py: %s",
                    len(drift["missing"]),
                    ", ".join(sorted(drift["missing"])),
                )
    except Exception as e:
        _log.debug("drift check skipped: %s", e)

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())

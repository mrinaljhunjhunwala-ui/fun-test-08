"""
alerts/check_alerts.py — Headless market alert checker.

Runs on a schedule (GitHub Actions) during NSE market hours and sends a
Telegram message when:
    1. A custom price alert from data/alerts.csv triggers
       (price crosses above/below a level you defined)
    2. India VIX enters a fear/panic regime
    3. Nifty 50 breaks into a confirmed downtrend

NO Streamlit dependency — pure Python stdlib + the project's headless
data helpers (utils.vix, utils.live_price, data.fetcher). Safe to run in CI.

Credentials (set as GitHub Actions secrets, NEVER committed):
    TELEGRAM_BOT_TOKEN   — from @BotFather
    TELEGRAM_CHAT_ID     — your numeric chat id (from @userinfobot)

De-duplication:
    A small data/alert_state.json records which alerts already fired today,
    so the same alert is not re-sent every run. The GitHub Actions cache
    persists this file between runs.

Exit codes: always 0 (a failed alert check should not fail the workflow).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import urllib.parse
import urllib.request

_log = logging.getLogger("alerts.check_alerts")

# Ensure emoji/Unicode in log output never crash on a non-UTF-8 console (Windows)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception as _enc_e:
    print(f"[startup] stdout reconfigure skipped: {_enc_e}")

# Make project root importable (script lives in <root>/alerts/)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis.action_labels import action_display_label  # noqa: E402

_ALERTS_CSV  = os.path.join(_ROOT, "data", "alerts.csv")
_STATE_JSON  = os.path.join(_ROOT, "data", "alert_state.json")
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


# ─────────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    """Send an HTML message to the configured Telegram chat."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing instead:")
        print(text)
        return False
    try:
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = r.status == 200
            print(f"[telegram] sent ({r.status})")
            return ok
    except Exception as e:
        print(f"[telegram] FAILED: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Gmail (Task 4.1 Q1 answer, added 2026-09-08)
# ─────────────────────────────────────────────────────────────────────────────

def send_gmail(subject: str, body_html: str) -> bool:
    """Send the alert as email via Gmail SMTP. body_html is stripped of
    the HTML tags used for Telegram, since email clients render plain
    text more reliably across mobile / web / desktop."""
    try:
        from utils.alerts_gmail import GmailAlerter
    except ImportError as _ie:
        print(f"[gmail] alerts_gmail import failed: {_ie}")
        return False
    import re as _re
    # Cheap HTML-to-text: strip <b>/<i>/<br>/etc, keep whitespace.
    _plain = _re.sub(r"<[^>]+>", "", body_html)
    _plain = (_plain
              .replace("&amp;", "&")
              .replace("&lt;",  "<")
              .replace("&gt;",  ">")
              .replace("&nbsp;", " "))
    return GmailAlerter().send(subject, _plain)


def _subject_from_message(msg: str, fallback: str) -> str:
    """Derive a subject line from the first non-empty line of the alert
    text, with HTML tags stripped and length-capped. Falls back to the
    channel-level label if the message is empty."""
    import re as _re
    first = ""
    for _ln in msg.splitlines():
        _s = _re.sub(r"<[^>]+>", "", _ln).strip()
        if _s:
            first = _s
            break
    if not first:
        return fallback
    return (first[:120] + "...") if len(first) > 120 else first


def dispatch(msg: str, *, subject: str = "NSE Smart Investor alert") -> bool:
    """Fan one alert out to every configured channel. Returns True if at
    least one channel accepted the message (so the de-dup marker gets
    set), False only if BOTH channels failed AND neither was in console-
    fallback mode. Channels that are unconfigured print to console and
    count as delivered locally (matching prior behaviour so a local dev
    run still marks the state file)."""
    _subj = _subject_from_message(msg, fallback=subject)
    _tg_ok    = send_telegram(msg)
    _gmail_ok = send_gmail(_subj, msg)
    # Name the alert in the run log — "1 alert(s) sent" alone didn't say which.
    print(f"[sent] {_subj} (telegram={'ok' if _tg_ok else 'fail'}, "
          f"gmail={'ok' if _gmail_ok else 'fail'})")
    return bool(_tg_ok or _gmail_ok)


# ─────────────────────────────────────────────────────────────────────────────
# De-dup state (per-day)
# ─────────────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        with open(_STATE_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}   # normal on first run — no state file yet
    except Exception as e:
        _log.warning(
            "_load_state: %s exists but is unreadable/corrupt, resetting to empty "
            "state (previously-fired alerts may re-fire today): %s", _STATE_JSON, e
        )
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(_STATE_JSON, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[state] could not save: {e}")


# _FORCE_SEND is set to True by main() when --force is on argv. It makes
# _already_fired() always return False AND _mark_fired() a no-op, so a
# manual test run (workflow_dispatch) does not pollute the shared state
# file and thereby starve subsequent scheduled runs of the same bucket.
# Root cause of "scheduled cron produced no email" bug diagnosed via the
# GitHub Actions runs API 2026-09-09: workflow_dispatch runs at 12:58 and
# 13:04 IST stamped momentum_pm=today, then every scheduled fire that
# afternoon saw dedup-set and exited in <1 second silently.
_FORCE_SEND = False


def _already_fired(state: dict, key: str, today: str) -> bool:
    """Dedup check. Prints when it's returning True so scheduled runs that
    silently skip because of dedup show up in the workflow log — before
    this diagnostic print, the "successful 1-second run that sent nothing"
    was indistinguishable from a healthy send in the Actions UI."""
    if _FORCE_SEND:
        # --force bypasses dedup for manual testing so the tester actually
        # sees the alert land in Gmail/Telegram right now.
        return False
    already = state.get(key) == today
    if already:
        print(f"[dedup] '{key}' already fired today ({today}) — skipping this check")
    return already


def _mark_fired(state: dict, key: str, today: str) -> None:
    """Set dedup state. --force is a no-op so manual tests don't leave a
    stamp that later starves the real scheduled fire."""
    if _FORCE_SEND:
        print(f"[dedup] --force is set; NOT marking '{key}' fired "
              "(manual test won't block the real scheduled fire)")
        return
    state[key] = today


def _prune_state(state: dict, today: str) -> dict:
    """Keep only today's entries so the file doesn't grow forever."""
    return {k: v for k, v in state.items() if v == today}


# ─────────────────────────────────────────────────────────────────────────────
# Market hours guard
#
# FIX HOL1: this used to be a hand-rolled weekday+time-of-day check with no
# NSE holiday awareness at all — a real gap given FIX MH2 (see
# dashboard/shared/market_hours.py) already consolidated every OTHER
# market-hours check in the repo onto one canonical, holiday-aware calendar
# specifically because a second, independent implementation like this one
# drifts out of date. Without this fix, check_price_alerts() would run and
# attempt to fetch "live" prices on every NSE holiday that falls on a
# weekday, on a schedule that fires every 15 minutes for ~8 hours.
# ─────────────────────────────────────────────────────────────────────────────

def _is_market_hours() -> bool:
    from dashboard.shared.market_hours import is_market_open
    return is_market_open()


# Morning messages bypass the market-hours guard, so they need their own
# clock: on 2026-09-30 GitHub ran the 09:20 cron at 15:31 and the "morning"
# watchlist went out after the close. A trigger outside the window is dropped.
_SEND_WINDOWS = {
    "morning-digest":     (datetime.time(7, 0),  datetime.time(10, 0)),
    "intraday-watchlist": (datetime.time(9, 15), datetime.time(10, 30)),
}


def _in_send_window(mode: str, now: datetime.datetime | None = None) -> bool:
    window = _SEND_WINDOWS.get(mode)
    if window is None:
        return True
    t = (now or datetime.datetime.now(_IST)).astimezone(_IST).time()
    return window[0] <= t < window[1]


# ─────────────────────────────────────────────────────────────────────────────
# Checks
# ─────────────────────────────────────────────────────────────────────────────

def check_price_alerts(state: dict, today: str) -> int:
    """Read data/alerts.csv, fetch live prices, fire on threshold crossings."""
    sent = 0
    try:
        import csv
        from utils.live_price import get_live_quote
    except Exception as e:
        print(f"[price] import failed: {e}")
        return 0

    rules = []
    try:
        with open(_ALERTS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if str(row.get("enabled", "1")).strip() in ("0", "false", "False", ""):
                    continue
                rules.append(row)
    except FileNotFoundError:
        print(f"[price] no alerts.csv at {_ALERTS_CSV}")
        return 0
    except Exception as e:
        print(f"[price] could not read alerts.csv: {e}")
        return 0

    for r in rules:
        ticker    = str(r.get("ticker", "")).strip().upper()
        condition = str(r.get("condition", "")).strip().lower()
        try:
            level = float(r.get("level", 0))
        except Exception as _lvl_e:
            print(f"[price] {r.get('ticker','?')}: invalid level '{r.get('level')}' — {_lvl_e}")
            continue
        note = str(r.get("note", "")).strip()
        if not ticker or condition not in ("above", "below") or level <= 0:
            continue

        key = f"price_{ticker}_{condition}_{level:.2f}"
        if _already_fired(state, key, today):
            continue

        q = get_live_quote(ticker)
        if not q or not q.get("price"):
            print(f"[price] {ticker}: no quote")
            continue
        price = float(q["price"])

        hit = (condition == "above" and price >= level) or \
              (condition == "below" and price <= level)
        if not hit:
            continue

        arrow = "🔼" if condition == "above" else "🔽"
        chg   = q.get("chg_pct", 0.0)
        msg = (
            f"{arrow} <b>{ticker}</b> price alert\n"
            f"Now <b>₹{price:,.2f}</b> ({chg:+.2f}% today)\n"
            f"Crossed <b>{condition} ₹{level:,.2f}</b>"
            + (f"\n📝 {note}" if note else "")
        )
        if dispatch(msg):
            _mark_fired(state, key, today)
            sent += 1
    return sent


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio alerts — derived from the My Portfolio holdings on every run
#
# No list to maintain: holdings are read from the same trade_store KV key the
# My Portfolio page writes, so adds / sells / avg-price edits take effect on
# the next run. Two alerts per holding:
#   * Step move: ±_PF_STEP_PCT from a per-holding reference price. The first
#     sighting only records the reference; each alert re-anchors it to the
#     alert price, so the next alert is the NEXT 10% move, not a repeat.
#   * Cost cross: price crosses the current average buy price, either way.
# References + last cost side persist in KV (_PF_ANCHORS_KEY, user "_system");
# sold holdings are pruned from it.
# ─────────────────────────────────────────────────────────────────────────────

def _holding_alerts_on() -> bool:
    """Holdings alerts (avg-cost, ±10%, levels on holdings) are opt-in since
    2026-10-01 — the user wants alerts on the digest picks, not their holdings.
    Set the repo variable ALERT_HOLDINGS=1 to bring them back."""
    return os.getenv("ALERT_HOLDINGS", "").strip().lower() in ("1", "true", "yes")


_PF_HOLDINGS_KEY = "manual_portfolio_holdings"
_PF_ANCHORS_KEY  = "portfolio_alert_anchors"
_PF_STEP_PCT     = 10.0
_PF_COST_MARGIN_PCT = 1.0


def check_portfolio_alerts(state: dict, today: str) -> int:
    try:
        import trade_store as store
        from utils.live_price import get_live_quote
    except Exception as e:
        print(f"[portfolio] import failed: {e}")
        return 0
    try:
        holdings = store.kv_get(_PF_HOLDINGS_KEY, None) or []
        anchors  = store.kv_get(_PF_ANCHORS_KEY, None, user_id="_system") or {}
    except Exception as e:
        print(f"[portfolio] kv read failed: {e}")
        return 0
    if not holdings:
        print("[portfolio] no holdings on the My Portfolio page — nothing to check")
        return 0

    sent, held = 0, set()
    for h in holdings:
        ticker = str(h.get("ticker", "")).upper().replace(".NS", "").replace(".BO", "").strip()
        try:
            avg = float(h.get("avg_buy_price") or 0)
        except (TypeError, ValueError):
            avg = 0.0
        if not ticker:
            continue
        held.add(ticker)
        q = get_live_quote(ticker)
        if not q or not q.get("price"):
            print(f"[portfolio] {ticker}: no quote")
            continue
        price = float(q["price"])
        chg = q.get("chg_pct", 0.0) or 0.0
        a = anchors.get(ticker)
        if not a or not a.get("ref"):
            anchors[ticker] = {"ref": price, "ref_date": today,
                               "side": ("above" if price >= avg else "below") if avg else None}
            print(f"[portfolio] {ticker}: reference set at {price:.2f}")
            continue

        # 1) Step move from the reference price
        ref = float(a["ref"])
        move = (price / ref - 1) * 100
        if abs(move) >= _PF_STEP_PCT:
            key = f"pf_step_{ticker}_{ref:.2f}"
            if not _already_fired(state, key, today):
                arrow = "🔼" if move > 0 else "🔽"
                msg = (f"{arrow} <b>{ticker}</b> moved <b>{move:+.1f}%</b> "
                       f"since ₹{ref:,.2f} ({a.get('ref_date', '?')})\n"
                       f"Now <b>₹{price:,.2f}</b> ({chg:+.2f}% today)\n"
                       f"📝 Portfolio holding · next alert at ±{_PF_STEP_PCT:.0f}% from here")
                if dispatch(msg):
                    _mark_fired(state, key, today)
                    sent += 1
                    a["ref"], a["ref_date"] = price, today

        # 2) Average-cost cross. A cross only counts once the price is
        # _PF_COST_MARGIN_PCT past the average, and at most one alert per
        # holding per day: on 2026-10-01 ONGC wobbled 21 paise around its
        # average and sent "above" then "below" 15 min apart.
        if avg:
            band = avg * _PF_COST_MARGIN_PCT / 100
            side = ("above" if price >= avg + band else
                    "below" if price <= avg - band else None)
            if not a.get("side"):
                a["side"] = side or ("above" if price >= avg else "below")
            elif side and side != a["side"]:
                a["side"] = side
                key = f"pf_cost_{ticker}"
                if not _already_fired(state, key, today):
                    arrow = "🔼" if side == "above" else "🔽"
                    msg = (f"{arrow} <b>{ticker}</b> crossed {side} your average cost "
                           f"<b>₹{avg:,.2f}</b>\n"
                           f"Now <b>₹{price:,.2f}</b> ({chg:+.2f}% today)\n"
                           f"📝 Portfolio holding · counted once {_PF_COST_MARGIN_PCT:g}% past the average")
                    if dispatch(msg):
                        _mark_fired(state, key, today)
                        sent += 1

    for t in [t for t in anchors if t not in held]:
        print(f"[portfolio] {t}: no longer held — dropping its reference")
        anchors.pop(t)
    try:
        store.kv_set(_PF_ANCHORS_KEY, anchors, user_id="_system")
    except Exception as e:
        print(f"[portfolio] kv write failed: {e}")
    return sent


# ─────────────────────────────────────────────────────────────────────────────
# Digest picks through the day — the user follows the suggested stocks, not
# their holdings (2026-10-02). Three messages, all about today's Morning
# Digest names:
#   * Opening update  — first run from 09:30: open, gap, first-15-min range
#   * ±2% moves       — day change from yesterday's close crosses ±2%, once
#                       per direction per stock per day (a stock that falls
#                       2% and then rallies 2% sends both)
#   * Picks recap     — first run from 15:15: move since the scan + day range
# Levels come from analysis/intraday_levels.py and are saved by the 09:20
# watchlist (KV intraday_levels) so later runs reuse the same numbers.
# ─────────────────────────────────────────────────────────────────────────────

_MOVE_PCT = 2.0
_OPENING_FROM = datetime.time(9, 30)
_OPENING_UNTIL = datetime.time(10, 30)
_RECAP_FROM = datetime.time(15, 15)
_LEVELS_KEY = "intraday_levels"


def _base_symbol(t) -> str:
    return str(t or "").upper().replace(".NS", "").replace(".BO", "").strip()


def _today_picks(store, today: str) -> dict | None:
    """Today's saved Morning Digest picks, or None."""
    try:
        morning = store.kv_get(_MORNING_PICKS_KEY, None, user_id="_system")
    except Exception:
        return None
    if isinstance(morning, dict) and morning.get("date") == today and morning.get("buys"):
        return morning
    return None


def _pick_levels(store, base: str, today: str) -> dict | None:
    """The watchlist's saved levels for `base`, else computed from daily bars."""
    try:
        saved = store.kv_get(_LEVELS_KEY, None, user_id="_system")
    except Exception:
        saved = None
    if isinstance(saved, dict) and saved.get("date") == today and base in (saved.get("levels") or {}):
        return saved["levels"][base]
    try:
        import pandas as pd
        from analysis.intraday_levels import daily_levels
        from data.fetcher import fetch_single
        df = fetch_single(f"{base}.NS", period="3mo")
        df = df[pd.to_datetime(df.index).strftime("%Y-%m-%d") < today]
        return daily_levels(df)
    except Exception as e:
        _log.debug("levels for %s failed: %s", base, e)
        return None


def _pick_session(base: str, today: str) -> dict | None:
    """Today's open / high / low / last / opening range from 5-min bars."""
    try:
        from analysis.intraday_levels import session_stats
        from data.fetcher import fetch_intraday
        bars = fetch_intraday(f"{base}.NS", interval="5m", days=1)
        return session_stats(bars, datetime.date.fromisoformat(today))
    except Exception as e:
        _log.debug("session for %s failed: %s", base, e)
        return None


def _quote(base: str) -> dict | None:
    try:
        from utils.live_price import get_live_quote
        q = get_live_quote(base)
    except Exception:
        return None
    return q if q and q.get("price") else None


def check_opening_update(state: dict, today: str, now: datetime.datetime | None = None) -> int:
    key = "opening_update"
    if _already_fired(state, key, today):
        return 0
    now = (now or datetime.datetime.now(_IST)).astimezone(_IST)
    if not (_OPENING_FROM <= now.time() < _OPENING_UNTIL):
        return 0
    try:
        import trade_store as store
        from analysis.intraday_levels import position_vs_levels
    except Exception as e:
        print(f"[opening] import failed: {e}")
        return 0
    morning = _today_picks(store, today)
    if not morning:
        print("[opening] no Morning Digest picks for today — nothing to update")
        return 0

    rows = []
    for i, b in enumerate(morning["buys"], start=1):
        base = _base_symbol(b.get("ticker"))
        lv = _pick_levels(store, base, today)
        ss = _pick_session(base, today)
        q = _quote(base)
        price = float(q["price"]) if q else (ss or {}).get("last")
        if not price:
            rows.append(f"{i}. <b>{base}</b> — no price yet")
            continue
        prev = float((q or {}).get("prev_close") or (lv or {}).get("y_close") or 0)
        line = f"{i}. <b>{base}</b>"
        if ss and prev:
            line += f" opened ₹{ss['open']:,.2f} ({(ss['open'] / prev - 1) * 100:+.1f}% vs yesterday)"
        if ss and ss.get("or_high"):
            line += f" · first 15 min ₹{ss['or_low']:,.2f}–₹{ss['or_high']:,.2f}"
        day = f" ({(price / prev - 1) * 100:+.1f}%)" if prev else ""
        rows.append(line + f"\n   Now ₹{price:,.2f}{day}"
                    + (f" · {position_vs_levels(price, lv)}" if lv else ""))

    lines = [f"🔔 <b>Opening update — today's picks</b> ({now:%H:%M} IST)", ""] + rows + [
        "",
        f"<i>The first 15 minutes set the opening range. Moves of ±{_MOVE_PCT:g}% from "
        f"yesterday's close are flagged during the day. Descriptive, not advice.</i>"]
    if dispatch("\n".join(lines), subject="Opening update — NSE Smart Investor"):
        _mark_fired(state, key, today)
        return 1
    return 0


def check_pick_moves(state: dict, today: str) -> int:
    try:
        import trade_store as store
        from analysis.intraday_levels import position_vs_levels
    except Exception as e:
        print(f"[moves] import failed: {e}")
        return 0
    morning = _today_picks(store, today)
    if not morning:
        return 0

    sent = 0
    for b in morning["buys"]:
        base = _base_symbol(b.get("ticker"))
        q = _quote(base)
        if not q:
            print(f"[moves] {base}: no quote")
            continue
        chg = float(q.get("chg_pct") or 0.0)
        if abs(chg) < _MOVE_PCT:
            continue
        direction = "up" if chg > 0 else "down"
        key = f"mv_{direction}_{base}"
        if _already_fired(state, key, today):
            continue
        price = float(q["price"])
        prev = q.get("prev_close")
        ss = _pick_session(base, today)
        lv = _pick_levels(store, base, today)
        icon = "🟢" if direction == "up" else "🔴"
        msg = f"{icon} <b>{base} {direction} {abs(chg):.1f}% today</b> · ₹{price:,.2f}"
        if prev:
            msg += f" (yesterday ₹{float(prev):,.2f})"
        detail = []
        if ss:
            detail.append(f"day range so far ₹{ss['low']:,.2f}–₹{ss['high']:,.2f}")
        if lv:
            detail.append(position_vs_levels(price, lv))
        if detail:
            msg += "\n   " + " · ".join(detail)
        msg += "\n📝 Morning Digest pick"
        if dispatch(msg):
            _mark_fired(state, key, today)
            sent += 1
    return sent


_VERDICT_ALERT_TIERS = ("STRONG BUY", "BUY")
_VERDICT_MAX_SNAPSHOT_AGE_H = 2      # the warmer refreshes every 15 min in market hours


def _regime_note() -> str:
    try:
        from analysis.score import _live_regime_label
        lbl = _live_regime_label()
    except Exception:
        return ""
    return f"NIFTY regime: {str(lbl).replace('_', ' ')}" if lbl else ""


def _format_verdict_alert(verdict: str, b: dict, fv, regime: str, gen_at) -> list:
    base = _base_symbol(b.get("ticker"))
    icon = "🟢🟢" if verdict == "STRONG BUY" else "🟢"
    price = b.get("price")
    price_str = f"₹{float(price):,.2f}" if isinstance(price, (int, float)) else "n/a"
    lines = [f"{icon} <b>{base}</b> — model verdict <b>{verdict}</b> · conviction "
             f"{getattr(fv, 'conviction', '?')} · score {b.get('score')}/90 · {price_str}"]
    meta = [x for x in (b.get("sector", "").strip(), b.get("horizon", "")) if x]
    if meta:
        lines.append("   " + " · ".join(meta))
    if b.get("sl") and b.get("tp"):
        lines.append(f"   Model reference levels: invalidation ₹{float(b['sl']):,.2f} · "
                     f"target ₹{float(b['tp']):,.2f}")
    if regime:
        lines.append(f"   {regime}")
    lines.append(f"<i>Scan at {_snapshot_ist_label(gen_at)}. Model output from past price data — "
                 "not investment advice; live track record on the Verdict Calibration page.</i>")
    return lines


def check_verdict_alerts(state: dict, today: str) -> int:
    """Telegram/Gmail when a stock in the latest Top Picks scan carries a BUY or
    STRONG BUY verdict — the same verdict the dashboard pick cards show
    (dashboard.shared.pick_freshness.compose_finalverdict_for_card, composite
    score + action + horizon). Once per stock per verdict tier per day; an
    upgrade BUY → STRONG BUY fires again. Descriptive, not advice."""
    try:
        import trade_store as store
        from dashboard.shared.pick_freshness import compose_finalverdict_for_card
        from utils.snapshot_time import snapshot_age_seconds
    except Exception as e:
        print(f"[verdicts] import failed: {e}")
        return 0
    try:
        snap = store.kv_get("top_picks_snapshot", user_id="_system")
    except Exception as e:
        print(f"[verdicts] kv_get failed: {e}")
        return 0
    if not (snap and isinstance(snap, dict) and isinstance(snap.get("data"), dict)):
        print("[verdicts] no top-picks snapshot")
        return 0
    gen_at = snap.get("generated_at", "unknown")
    _age = snapshot_age_seconds(gen_at)
    if _age is None or _age / 3600 > _VERDICT_MAX_SNAPSHOT_AGE_H:
        print(f"[verdicts] snapshot is stale (generated_at={gen_at}) — skipping")
        return 0

    hits = []
    for b in snap["data"].get("buys") or []:
        try:
            fv = compose_finalverdict_for_card(b, tqs=None, log_source=None)
        except Exception as e:
            print(f"[verdicts] {b.get('ticker')}: verdict failed: {e}")
            continue
        verdict = str(getattr(fv, "verdict", "") or "").upper()
        if verdict in _VERDICT_ALERT_TIERS:
            hits.append((verdict, b, fv))
    print(f"[verdicts] {len(hits)} BUY/STRONG BUY in scan at {_snapshot_ist_label(gen_at)}")

    sent = 0
    regime = _regime_note()
    for verdict, b, fv in sorted(hits, key=lambda h: (h[0] != "STRONG BUY", -float(h[1].get("score") or 0))):
        base = _base_symbol(b.get("ticker"))
        key = f"verdict_{verdict.replace(' ', '_').lower()}_{base}"
        if _already_fired(state, key, today):
            continue
        lines = _format_verdict_alert(verdict, b, fv, regime, gen_at)
        if dispatch("\n".join(lines)):
            _mark_fired(state, key, today)
            sent += 1
    return sent


def check_picks_recap(state: dict, today: str, now: datetime.datetime | None = None) -> int:
    key = "picks_recap"
    if _already_fired(state, key, today):
        return 0
    now = (now or datetime.datetime.now(_IST)).astimezone(_IST)
    if now.time() < _RECAP_FROM:
        return 0
    try:
        import trade_store as store
    except Exception as e:
        print(f"[recap] import failed: {e}")
        return 0
    morning = _today_picks(store, today)
    if not morning:
        print("[recap] no Morning Digest picks for today — nothing to recap")
        return 0

    rows, moves = [], []
    for i, b in enumerate(morning["buys"], start=1):
        base = _base_symbol(b.get("ticker"))
        try:
            ref = float(b.get("price") or b.get("last_price") or 0)
        except (TypeError, ValueError):
            ref = 0.0
        q = _quote(base)
        if not q:
            rows.append(f"{i}. <b>{base}</b> — no live price")
            continue
        price = float(q["price"])
        day = q.get("chg_pct", 0.0) or 0.0
        if ref > 0:
            since = (price / ref - 1) * 100
            moves.append(since)
            rows.append(f"{i}. <b>{base}</b> ₹{ref:,.2f} → <b>₹{price:,.2f}</b> "
                        f"({since:+.1f}% since the scan · {day:+.2f}% today)")
        else:
            rows.append(f"{i}. <b>{base}</b> now <b>₹{price:,.2f}</b> ({day:+.2f}% today)")
        extra = []
        ss = _pick_session(base, today)
        if ss:
            extra.append(f"day range ₹{ss['low']:,.2f}–₹{ss['high']:,.2f}")
        extra += [f"{d} {_MOVE_PCT:g}%+ at one point"
                  for d in ("up", "down") if state.get(f"mv_{d}_{base}") == today]
        if extra:
            rows.append("   " + " · ".join(extra))

    lines = [f"📊 <b>Digest picks — how they did today</b> ({now:%H:%M} IST)",
             f"<i>This morning's Delivery Digest names "
             f"(scan at {_snapshot_ist_label(morning.get('generated_at'))}).</i>", ""]
    lines += rows
    if moves:
        up = sum(m > 0 for m in moves)
        avg_move = round(sum(moves) / len(moves), 1) + 0.0     # + 0.0 turns -0.0 into 0.0
        lines += ["", f"Average {avg_move:+.1f}% · {up} up / {len(moves) - up} down or flat"]
    lines += ["", "<i>Price moves since the scan — descriptive, not advice.</i>"]
    if dispatch("\n".join(lines), subject="Digest picks recap — NSE Smart Investor"):
        _mark_fired(state, key, today)
        return 1
    return 0


def check_vix_regime(state: dict, today: str) -> int:
    """Alert when India VIX is in a fear/panic regime."""
    try:
        from utils.vix import get_india_vix_regime
        info = get_india_vix_regime()
    except Exception as e:
        print(f"[vix] failed: {e}")
        return 0

    regime = str(info.get("regime", "unknown")).lower()
    vix    = info.get("vix")
    if regime not in ("fear", "panic"):
        return 0

    key = f"vix_{regime}"
    if _already_fired(state, key, today):
        return 0

    icon = "🚨" if regime == "panic" else "🔴"
    msg = (
        f"{icon} <b>Market volatility alert</b>\n"
        f"India VIX is in <b>{regime.upper()}</b> territory"
        + (f" at <b>{vix:.1f}</b>" if vix else "")
        + "\nVolatility is elevated — daily swings in this regime run wider "
          "than usual, in both directions."
    )
    if dispatch(msg):
        _mark_fired(state, key, today)
        return 1
    return 0


def check_nifty_trend(state: dict, today: str) -> int:
    """Alert when Nifty 50 is in a confirmed downtrend (price < SMA20 < SMA50)."""
    try:
        from data.fetcher import fetch_single
        df = fetch_single("^NSEI", period="3mo")
    except Exception as e:
        print(f"[nifty] fetch failed: {e}")
        return 0
    if df is None or df.empty or len(df) < 50:
        return 0

    close = df["Close"]
    price = float(close.iloc[-1])
    sma20 = float(close.rolling(20).mean().iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    if not (price < sma20 < sma50):
        return 0

    key = "nifty_downtrend"
    if _already_fired(state, key, today):
        return 0

    chg5 = (price / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else 0.0
    msg = (
        f"📉 <b>Nifty 50 downtrend</b>\n"
        f"Nifty at <b>{price:,.0f}</b> ({chg5:+.1f}% 5d), now below both "
        f"SMA20 ({sma20:,.0f}) and SMA50 ({sma50:,.0f}).\n"
        f"Price &lt; SMA20 &lt; SMA50 — a confirmed downtrend on this rule."
    )
    if dispatch(msg):
        _mark_fired(state, key, today)
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Delivery digest — top positional picks from the pre-warmed snapshot
#
# Reads the ~745-ticker top-picks scan that scripts/warm_top_picks.py already
# writes every 15 min to trade_store KV, so this check does NOT rescan the
# universe. It only formats and sends. Fires ONCE per day, so the state key
# is date-only. Primary sender: warm-preopen-scan.yml, right after it writes
# a fresh snapshot. The 09:00 IST market-digest cron is a fallback only.
# ─────────────────────────────────────────────────────────────────────────────

_DIGEST_MAX_SNAPSHOT_AGE_H = 16

# The names the Morning Digest actually sent, so the 09:20 intraday watchlist
# reframes the same list instead of whatever the 15-min warmer wrote since.
_MORNING_PICKS_KEY = "morning_digest_picks"


def _snapshot_ist_label(gen_at) -> str:
    """generated_at (UTC ISO) → 'HH:MM IST'; the raw stamp is UTC, not IST."""
    from utils.snapshot_time import parse_snapshot_timestamp
    ts = parse_snapshot_timestamp(gen_at)
    return ts.astimezone(_IST).strftime("%H:%M IST") if ts else "unknown time"


def check_delivery_digest(state: dict, today: str, *, max_picks: int = 5) -> int:
    """Read persisted Top Picks snapshot and email/Telegram the top delivery
    candidates for the day. One fire per day (state-deduplicated)."""
    key = "delivery_digest"
    if _already_fired(state, key, today):
        return 0

    try:
        import trade_store as store
    except Exception as _e:
        print(f"[digest] trade_store import failed: {_e}")
        return 0

    try:
        snap = store.kv_get("top_picks_snapshot", user_id="_system")
    except Exception as _e:
        print(f"[digest] kv_get failed: {_e}")
        return 0

    if not (snap and isinstance(snap, dict) and isinstance(snap.get("data"), dict)):
        print("[digest] no persisted top-picks snapshot yet — skipping")
        return 0

    gen_at = snap.get("generated_at", "unknown")
    # Don't present an old scan as "today's". snapshot_age_seconds() is
    # timezone-safe (warmer stamps aware UTC; legacy naive stamps read as UTC).
    # 16h still accepts last evening's post-close scan (same closing prices a
    # pre-open scan would see) but rejects Friday's snapshot on Monday. Not
    # marked fired, so a later run with a fresh snapshot can still send.
    from utils.snapshot_time import snapshot_age_seconds
    _age_s = snapshot_age_seconds(gen_at)
    _age_h = None if _age_s is None else _age_s / 3600
    if _age_h is None or _age_h > _DIGEST_MAX_SNAPSHOT_AGE_H:
        print(f"[digest] snapshot is stale (generated_at={gen_at}) — skipping")
        return 0

    buys = snap["data"].get("buys") or []
    if not buys:
        print("[digest] snapshot has 0 buys — nothing to send")
        return 0

    lines = [
        "📈 <b>Morning Delivery Digest</b>",
        f"<i>Top {min(max_picks, len(buys))} positional candidates from today's scan "
        f"(scan at {_snapshot_ist_label(gen_at)})</i>",
        "",
    ]
    for i, b in enumerate(buys[:max_picks], start=1):
        tk    = b.get("ticker", "?")
        score = b.get("score", 0)
        act   = action_display_label(b.get("action", "").replace("_", " "))
        price = b.get("price") or b.get("last_price") or b.get("current_price")
        entry = b.get("entry_zone") or b.get("entry")
        stop  = b.get("stop_loss")  or b.get("stop")
        tgt   = b.get("target")
        sec   = b.get("sector", "").strip()

        price_str = f"₹{float(price):,.2f}" if isinstance(price, (int, float)) else "n/a"
        head = f"{i}. <b>{tk}</b> — {act} · Score {score}/90 · {price_str}"
        if sec:
            head += f" · {sec}"
        lines.append(head)
        detail = []
        if entry:
            detail.append(f"Entry {entry}")
        if stop:
            detail.append(f"Stop {stop}")
        if tgt:
            detail.append(f"Target {tgt}")
        if detail:
            lines.append("   " + " · ".join(str(x) for x in detail))

    lines.extend([
        "",
        "<i>Descriptive scan of composite-score leaders — not advice. "
        "Each name's full breakdown is on the Analyze Stock page.</i>",
    ])
    msg = "\n".join(lines)

    if dispatch(msg, subject="Morning Delivery Digest — NSE Smart Investor"):
        _mark_fired(state, key, today)
        try:
            store.kv_set(_MORNING_PICKS_KEY,
                         {"date": today, "generated_at": gen_at, "buys": buys[:max_picks]},
                         user_id="_system")
        except Exception as _e:
            print(f"[digest] could not save today's picks for the intraday watchlist: {_e}")
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Momentum opportunities — pure scanner + fetcher, fires every 30 min
# ─────────────────────────────────────────────────────────────────────────────

def check_momentum_opportunities(state: dict, today: str, *, top_n: int = 8) -> int:
    """Run the Donchian-55 momentum scan over the niftytotalmarket universe
    and send a digest. Deduped by (today + hour bucket) so we get at most 2
    fires per day from the every-30-min workflow trigger — one mid-morning
    push and one afternoon push, not the same list every 30 min.

    The 30-min cadence is intentional: momentum breakouts hold for hours,
    not minutes, and a 15-min ping would spam identical lists.
    """
    now_ist = datetime.datetime.now(_IST)
    # Bucket: "am" (before 12:30) vs "pm" — one digest per bucket per day
    bucket = "am" if now_ist.hour < 12 or (now_ist.hour == 12 and now_ist.minute < 30) else "pm"
    key = f"momentum_{bucket}"
    if _already_fired(state, key, today):
        return 0

    try:
        from analysis.momentum_scanner import (
            scan_momentum, format_momentum_message, MomentumConfig,
        )
        from data.fetcher import fetch_single
        from data.universe import get_universe
    except Exception as _e:
        print(f"[momentum] import failed: {_e}")
        return 0

    try:
        universe = get_universe("niftytotalmarket")
    except Exception as _e:
        print(f"[momentum] universe load failed: {_e}")
        return 0

    def _fetch(ticker: str):
        try:
            df = fetch_single(ticker, period="2y")
            return df
        except Exception as _fe:
            _log.debug("momentum fetch failed for %s: %s", ticker, _fe)
            return None

    try:
        nifty_df = fetch_single("^NSEI", period="2y")
    except Exception as _e:
        print(f"[momentum] nifty fetch failed (RS will fall back to absolute): {_e}")
        nifty_df = None

    cfg = MomentumConfig(top_n=top_n)
    print(f"[momentum] scanning {len(universe)} tickers ({bucket} bucket)...")
    candidates = scan_momentum(universe, _fetch, nifty_history=nifty_df, cfg=cfg)

    msg = format_momentum_message(candidates)
    subject = (
        f"Momentum Scan — {len(candidates)} setup(s) surfaced"
        if candidates else "Momentum Scan — quiet day"
    )
    if dispatch(msg, subject=subject):
        _mark_fired(state, key, today)
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostic mode — proves end-to-end delivery
#
# check_test_alert unconditionally sends a "hello" message through EVERY
# configured channel, with per-channel success reporting printed to the
# workflow log. Use this to debug "I ran the workflow but nothing arrived":
# it will tell you exactly which channel is broken (or that both worked
# and the mail landed in Promotions / All Mail / a filter).
# ─────────────────────────────────────────────────────────────────────────────

def check_test_verdict_alert(state: dict, today: str) -> int:
    """Send ONE sample verdict alert, formatted by the real formatter, for a
    made-up ticker and clearly marked TEST. No dedup, no market data read."""
    class _FV:
        conviction = 88
    sample = {"ticker": "SAMPLE.NS", "score": 70.0, "price": 1234.50, "sl": 1180.00,
              "tp": 1340.00, "sector": "Pharma", "horizon": "Positional (2–4 weeks)"}
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    lines = ["🧪 <b>TEST — sample of a STRONG BUY verdict alert (not a real stock or signal)</b>", ""]
    lines += _format_verdict_alert("STRONG BUY", sample, _FV(), "NIFTY regime: trend down", now_utc)
    return 1 if dispatch("\n".join(lines), subject="NSE Smart Investor — sample verdict alert") else 0


def check_test_alert(state: dict, today: str) -> int:
    """Fire a one-shot diagnostic message via Telegram AND Gmail with per-
    channel success reporting. No dedup — every invocation attempts a send
    so you can retry until it works. Returns 1 if any channel succeeded.
    """
    now_ist = datetime.datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S")
    body_html = (
        "🧪 <b>NSE Smart Investor — Test Alert</b>\n"
        f"<i>Fired at {now_ist} IST from GitHub Actions workflow_dispatch.</i>\n\n"
        "If you see this, both the workflow secrets and the send path are "
        "wired correctly. Your real alerts (delivery digest, momentum scan, "
        "portfolio posture) will arrive on the same channel(s).\n\n"
        "<i>Diagnostic — no market data attached.</i>"
    )
    subject = f"NSE Smart Investor test alert — {now_ist} IST"

    # Report per-channel status BEFORE dispatch so the log tells the whole story
    _tg_env    = bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))
    _gmail_env = bool(
        os.environ.get("ALERT_GMAIL_ADDRESS")
        and os.environ.get("ALERT_GMAIL_APP_PASSWORD")
        and os.environ.get("ALERT_GMAIL_TO")
    )
    print(f"[test] TELEGRAM secrets present: {_tg_env}")
    print(f"[test]    ALERT_GMAIL_ADDRESS      set: {bool(os.environ.get('ALERT_GMAIL_ADDRESS'))}")
    print(f"[test]    ALERT_GMAIL_APP_PASSWORD set: {bool(os.environ.get('ALERT_GMAIL_APP_PASSWORD'))}")
    print(f"[test]    ALERT_GMAIL_TO           set: {bool(os.environ.get('ALERT_GMAIL_TO'))}")
    print(f"[test]    TELEGRAM_BOT_TOKEN       set: {bool(os.environ.get('TELEGRAM_BOT_TOKEN'))}")
    print(f"[test]    TELEGRAM_CHAT_ID         set: {bool(os.environ.get('TELEGRAM_CHAT_ID'))}")

    _tg_ok    = send_telegram(body_html)
    _gmail_ok = send_gmail(subject, body_html)

    print(f"[test] Telegram send returned: {_tg_ok}")
    print(f"[test] Gmail    send returned: {_gmail_ok}")

    if _tg_ok or _gmail_ok:
        print("[test] SUCCESS — at least one channel accepted the message. "
              "If you don't see it in Telegram/Gmail, check: (1) Gmail Spam / "
              "Promotions / All Mail folders, (2) that you sent your bot the "
              "initial 'hi' message so it can DM you, (3) the recipient "
              "address in ALERT_GMAIL_TO.")
        return 1

    print("[test] FAILURE — no channel accepted the message. "
          "Check the *_ok logs above: if send_telegram printed 'FAILED' the "
          "bot token or chat id is wrong; if send_gmail's GmailAlerter said "
          "console-fallback, one or more ALERT_GMAIL_* secrets are missing "
          "or misspelled.")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio posture — analyse Angel One holdings + intraday positions
#
# Fires once per day (typically 14:00 IST via the market-digest workflow's
# 3rd cron) and sends a message listing only the holdings that need
# attention: EXIT_WATCH, TRIM_WATCH, ADD_WATCH, plus intraday STOP_HIT /
# TARGET_HIT / TRAIL_TIGHTER. If everything is HOLD/RUNNING, no message
# (only_actionable=True by default in format_portfolio_message).
# ─────────────────────────────────────────────────────────────────────────────

def check_portfolio_posture(state: dict, today: str) -> int:
    """Fetch Angel One holdings + positions, run posture analysis, alert only
    on actionable rows. Requires ANGEL_* env vars — silently no-ops otherwise
    (Angel One is optional; we can't analyse a portfolio we can't see)."""
    key = "portfolio_posture"
    if _already_fired(state, key, today):
        return 0

    try:
        from analysis.portfolio_posture import (
            NO_FLAGGED_POSTURES, analyse_holdings, analyse_positions,
            format_portfolio_message,
        )
        from data.angel_fetcher import get_holdings, get_positions
        from data.fetcher import fetch_single
    except Exception as _e:
        print(f"[portfolio] import failed: {_e}")
        return 0

    try:
        holdings = get_holdings() or []
    except Exception as _e:
        print(f"[portfolio] get_holdings failed (Angel One session issue?): {_e}")
        return 0
    try:
        positions = (get_positions() or {}).get("day", []) or []
    except Exception as _e:
        print(f"[portfolio] get_positions failed: {_e}")
        positions = []

    if not holdings and not positions:
        print("[portfolio] no holdings / positions — nothing to analyse")
        return 0

    def _fetch(sym):
        try:
            return fetch_single(sym, period="2y")
        except Exception:
            return None

    try:
        nifty_df = fetch_single("^NSEI", period="2y")
    except Exception:
        nifty_df = None

    print(f"[portfolio] analysing {len(holdings)} holdings + {len(positions)} intraday positions")
    hpost = analyse_holdings(holdings, _fetch, nifty_history=nifty_df)
    ipost = analyse_positions(positions, _fetch)

    msg = format_portfolio_message(hpost, ipost, only_actionable=True)
    # Suppress the 'nothing to do' message from the daily digest — the whole
    # point of an alert is to surface something. If everything is HOLD,
    # count it as fired-successfully (dedup key set) so we don't keep polling.
    if NO_FLAGGED_POSTURES in msg:
        _mark_fired(state, key, today)
        print("[portfolio] all HOLD/RUNNING — no message sent")
        return 0

    if dispatch(msg, subject=f"Portfolio Posture — {len(hpost)+len(ipost)} row(s) need attention"):
        _mark_fired(state, key, today)
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Intraday morning watchlist — 09:20 IST
#
# The Morning Digest names with reference levels in plain words (no ORB /
# ATR / stop / target jargon — the user asked for clearer intraday language,
# 2026-10-02): yesterday's close ± half a typical day's move (ATR-14), and a
# next level 1.5 typical moves above the upside one. The levels are saved
# (KV intraday_levels) for the opening update and the ±2% move alerts.
# ─────────────────────────────────────────────────────────────────────────────

def check_intraday_watchlist(state: dict, today: str, *, max_picks: int = 5) -> int:
    """Reference levels for today's picks, in plain language. Once per day."""
    key = "intraday_watchlist"
    if _already_fired(state, key, today):
        return 0

    try:
        import trade_store as store
        from analysis.intraday_levels import daily_levels
        from data.fetcher import fetch_single
    except Exception as _e:
        print(f"[intraday] import failed: {_e}")
        return 0

    # Prefer the exact list this morning's digest sent; fall back to the
    # latest snapshot (and say so) if the digest didn't go out today.
    morning = _today_picks(store, today)
    if morning:
        buys = morning["buys"]
        source = (f"this morning's Delivery Digest "
                  f"(scan at {_snapshot_ist_label(morning.get('generated_at'))})")
    else:
        try:
            snap = store.kv_get("top_picks_snapshot", user_id="_system")
        except Exception as _e:
            print(f"[intraday] kv_get failed: {_e}")
            return 0

        if not (snap and isinstance(snap, dict) and isinstance(snap.get("data"), dict)):
            print("[intraday] no persisted top-picks snapshot — skipping")
            return 0

        buys = snap["data"].get("buys") or []
        source = f"the latest delivery scan (at {_snapshot_ist_label(snap.get('generated_at'))})"
    if not buys:
        print("[intraday] snapshot has 0 buys — nothing to reformat")
        return 0

    lines = [
        "⚡ <b>Intraday Watchlist — today's picks</b>",
        f"<i>Top {min(max_picks, len(buys))} names from {source}.</i>",
        "",
    ]
    saved: dict[str, dict] = {}
    for i, b in enumerate(buys[:max_picks], start=1):
        tk = b.get("ticker", "?")
        base = _base_symbol(tk)
        try:
            df = fetch_single(tk, period="3mo")
        except Exception as _fe:
            _log.debug("intraday fetch failed for %s: %s", tk, _fe)
            df = None
        lv = daily_levels(df)
        if lv is None:
            # Skip the levels for this row but still surface the name
            lines.append(f"{i}. <b>{base}</b> — not enough price history for levels")
            continue
        saved[base] = lv
        pct = lv["atr"] / lv["y_close"] * 100
        lines.append(f"{i}. <b>{base}</b> · closed ₹{lv['y_close']:,.2f} yesterday · "
                     f"typical day's move ₹{lv['atr']:,.2f} ({pct:.1f}%)")
        lines.append(f"   ▲ Upside level ₹{lv['up']:,.2f} · next level above it ₹{lv['next_up']:,.2f}")
        lines.append(f"   ▼ Downside level ₹{lv['down']:,.2f}")

    lines.extend([
        "",
        "<i>How to read it: the upside and downside levels sit half a typical day's "
        "move either side of yesterday's close. A move above ▲ means the stock is "
        "pushing past its normal range on the way up; below ▼, on the way down; "
        "between them it's moving normally. An opening update follows at about 09:30. "
        "Price arithmetic, not advice.</i>",
    ])

    if not saved:
        print("[intraday] no rows could be enriched — skipping send")
        return 0

    msg = "\n".join(lines)
    if dispatch(msg, subject="Intraday Watchlist — NSE Smart Investor"):
        _mark_fired(state, key, today)
        try:
            store.kv_set(_LEVELS_KEY, {"date": today, "levels": saved}, user_id="_system")
        except Exception as _e:
            print(f"[intraday] could not save today's levels: {_e}")
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Sunday weekly digest — 09:00 IST Sunday
#
# Zooms out from the daily fires: current top-picks list, journal entries
# added this week, and any reviews due next week. Meant for the "how did
# my thesis basket evolve" question rather than "what's fresh today".
# ─────────────────────────────────────────────────────────────────────────────

def check_weekly_digest(state: dict, today: str) -> int:
    """Sunday-only weekly summary. Once per week."""
    # Dedup by ISO week — one fire per week, not per day
    _week_id = datetime.datetime.strptime(today, "%Y-%m-%d").strftime("%G-W%V")
    key = f"weekly_digest_{_week_id}"
    if _already_fired(state, key, today):
        return 0

    now_ist = datetime.datetime.now(_IST)
    # Sunday guard — check_alerts.py's dispatcher gates the fire time but
    # the mode itself belt-and-braces refuses to run mid-week even if
    # someone workflow_dispatch's it (weekly ≠ daily).
    if now_ist.weekday() != 6:   # Monday=0 ... Sunday=6
        print(f"[weekly] not Sunday (weekday={now_ist.weekday()}) — skipping")
        return 0

    lines = [
        "📆 <b>Weekly Digest — Week Ahead</b>",
        f"<i>Sunday {today}. Roll-up of last week's picks + journal + review due.</i>",
        "",
    ]

    # Section 1 — current top-picks list (from the persisted snapshot)
    try:
        import trade_store as store
        snap = store.kv_get("top_picks_snapshot", user_id="_system")
        buys = (snap or {}).get("data", {}).get("buys", []) or []
    except Exception as _e:
        print(f"[weekly] snapshot read failed: {_e}")
        buys = []

    if buys:
        lines.append("<b>Top composite scores from the delivery scan</b>")
        for i, b in enumerate(buys[:5], start=1):
            tk = b.get("ticker", "?")
            score = b.get("score", 0)
            act   = action_display_label(b.get("action", "").replace("_", " "))
            lines.append(f"{i}. <b>{tk}</b> — {act} · Score {score}/90")
        lines.append("")
    else:
        lines.append("<i>No top-picks snapshot to summarise this week.</i>")
        lines.append("")

    # Section 2 — journal entries added this week
    try:
        from alerts.journal_store import read_entries
        entries = read_entries()
    except Exception as _e:
        print(f"[weekly] journal read failed: {_e}")
        entries = []

    _week_start = (now_ist - datetime.timedelta(days=7)).date().isoformat()
    recent = [e for e in entries if e.added_date >= _week_start]
    if recent:
        lines.append(f"<b>Journal entries this week — {len(recent)}</b>")
        for e in recent[:8]:
            lines.append(f"• <b>{e.ticker}</b> ({e.status}) — {e.thesis[:80]}")
        lines.append("")

    # Section 3 — reviews due in the next 7 days
    _due_cutoff = (now_ist + datetime.timedelta(days=7)).date().isoformat()
    due = [
        e for e in entries
        if e.status == "open" and e.review_date and e.review_date <= _due_cutoff
    ]
    if due:
        lines.append(f"<b>Journal reviews due next week — {len(due)}</b>")
        for e in sorted(due, key=lambda x: x.review_date)[:8]:
            lines.append(f"• <b>{e.ticker}</b> — review by {e.review_date}: {e.thesis[:80]}")
        lines.append("")

    if len(lines) <= 4:
        # Only the header + "no data" line landed — send a "quiet week" note
        # rather than a near-empty message.
        lines.append("<i>Quiet week — no snapshot, no journal activity. Nothing to review.</i>")

    lines.extend([
        "",
        "<i>Weekly roll-up. Descriptive, not advice.</i>",
    ])
    msg = "\n".join(lines)

    if dispatch(msg, subject=f"Weekly Digest — week of {today}"):
        _mark_fired(state, key, today)
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Main — mode-based dispatch
#
# Modes:
#   default              — the original 15-min market-alerts cadence
#                          (price CSV + portfolio holdings + VIX + Nifty trend)
#   morning-digest       — 09:00 IST daily: delivery top-picks digest
#   intraday-watchlist   — 09:20 IST daily: intraday framing of top picks
#   momentum             — every 30 min market hours: momentum opportunities
#   portfolio-posture    — 14:00 IST daily: Angel One holdings posture
#   weekly-digest        — 09:00 IST Sunday: week-ahead summary
#   test                 — diagnostic; proves both channels deliver
#
# --force skips the market-hours guard for local/manual runs.
# ─────────────────────────────────────────────────────────────────────────────

def _parse_mode(argv: list[str]) -> str:
    for a in argv:
        if a.startswith("--mode="):
            return a.split("=", 1)[1].strip().lower()
    return "default"


def main() -> int:
    force        = "--force" in sys.argv
    clear_today  = "--clear-today" in sys.argv
    mode         = _parse_mode(sys.argv)

    # Propagate --force into the module-level dedup-bypass flag so every
    # _already_fired() call in this run returns False and every
    # _mark_fired() call is a no-op. See _FORCE_SEND above for why.
    global _FORCE_SEND
    _FORCE_SEND = force

    # --clear-today: wipe today's dedup entries from the state file BEFORE
    # this run's own state pruning. Use this exactly once after a stale
    # manual-test dedup entry has starved a real scheduled fire — after
    # which the fix above (dedup bypass on --force) prevents recurrence.
    if clear_today:
        try:
            _today_iso = datetime.datetime.now(_IST).strftime("%Y-%m-%d")
            _existing = _load_state()
            _wiped    = {k: v for k, v in _existing.items() if v != _today_iso}
            _dropped  = len(_existing) - len(_wiped)
            _save_state(_wiped)
            print(f"[clear-today] dropped {_dropped} state entries for {_today_iso}")
        except Exception as _e:
            print(f"[clear-today] failed: {_e}")

    # morning-digest fires pre-market (09:00 IST); portfolio-posture can also
    # run after-hours (holdings LTP is snapshot-based from Angel One's last
    # trade). Test mode must ALWAYS run — its whole purpose is diagnostics.
    # Other modes still enforce the market-hours guard unless --force.
    _bypass_market_hours = (
        "morning-digest", "portfolio-posture", "test", "test-verdict",
        "weekly-digest", "intraday-watchlist",
    )
    if mode not in _bypass_market_hours and not force and not _is_market_hours():
        print(f"[main mode={mode}] outside NSE market hours — nothing to do.")
        return 0
    if not force and not _in_send_window(mode):
        _lo, _hi = _SEND_WINDOWS[mode]
        print(f"[main mode={mode}] {datetime.datetime.now(_IST):%H:%M} IST is outside its "
              f"{_lo:%H:%M}–{_hi:%H:%M} send window — late trigger, skipping.")
        return 0

    today = datetime.datetime.now(_IST).strftime("%Y-%m-%d")
    state = _prune_state(_load_state(), today)

    total = 0
    if mode == "morning-digest":
        total += check_delivery_digest(state, today)
    elif mode == "intraday-watchlist":
        total += check_intraday_watchlist(state, today)
    elif mode == "momentum":
        total += check_momentum_opportunities(state, today)
    elif mode == "portfolio-posture":
        total += check_portfolio_posture(state, today)
    elif mode == "weekly-digest":
        total += check_weekly_digest(state, today)
    elif mode == "test":
        total += check_test_alert(state, today)
    elif mode == "test-verdict":
        total += check_test_verdict_alert(state, today)
    else:   # default
        _counts = {
            "price":     check_price_alerts(state, today),
            "portfolio": check_portfolio_alerts(state, today) if _holding_alerts_on() else 0,
            "opening":   check_opening_update(state, today),
            "moves":     check_pick_moves(state, today),
            "recap":     check_picks_recap(state, today),
            "verdicts":  check_verdict_alerts(state, today),
            "vix":       check_vix_regime(state, today),
            "nifty":     check_nifty_trend(state, today),
        }
        print("[main] per-check: " + ", ".join(f"{k}={v}" for k, v in _counts.items()))
        total += sum(_counts.values())

    _save_state(state)
    print(f"[main mode={mode}] done — {total} alert(s) sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

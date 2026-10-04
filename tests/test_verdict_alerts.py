"""check_verdict_alerts — BUY/STRONG BUY verdicts from the Top Picks snapshot."""
import datetime as dt

import alerts.check_alerts as ca


def _pick(tk, score, action):
    return {"ticker": tk, "score": score, "action": action, "price": 100.0, "sl": 95.0,
            "tp": 110.0, "sector": "Pharma", "horizon": "Positional (2–4 weeks)"}


def _setup(monkeypatch, buys, verdicts, age_h=0.2):
    import trade_store
    gen = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=age_h)).isoformat()
    monkeypatch.setattr(trade_store, "kv_get",
                        lambda *a, **k: {"generated_at": gen, "data": {"buys": buys}})

    class _FV:
        def __init__(self, v):
            self.verdict, self.conviction = v, 86

    import dashboard.shared.pick_freshness as pf
    monkeypatch.setattr(pf, "compose_finalverdict_for_card",
                        lambda b, tqs=None, log_source=None: _FV(verdicts[b["ticker"]]))
    monkeypatch.setattr(ca, "_regime_note", lambda: "NIFTY regime: trend down")
    sent = []
    monkeypatch.setattr(ca, "dispatch", lambda m, **k: sent.append(m) or True)
    return sent


def test_sends_buy_and_strong_buy_once_per_day(monkeypatch):
    buys = [_pick("AAA.NS", 70, "STRONG_BUY"), _pick("BBB.NS", 62, "BUY"), _pick("CCC.NS", 55, "WATCHLIST")]
    sent = _setup(monkeypatch, buys, {"AAA.NS": "STRONG BUY", "BBB.NS": "BUY", "CCC.NS": "WATCH"})
    state = {}
    assert ca.check_verdict_alerts(state, "2026-10-02") == 2
    assert "AAA" in sent[0] and "Strong trend" in sent[0]     # strongest first
    assert "BBB" in sent[1] and "not investment advice" in sent[1]
    assert ca.check_verdict_alerts(state, "2026-10-02") == 0   # deduped


def test_upgrade_fires_again(monkeypatch):
    state = {}
    _setup(monkeypatch, [_pick("AAA.NS", 62, "BUY")], {"AAA.NS": "BUY"})
    assert ca.check_verdict_alerts(state, "2026-10-02") == 1
    _setup(monkeypatch, [_pick("AAA.NS", 70, "STRONG_BUY")], {"AAA.NS": "STRONG BUY"})
    assert ca.check_verdict_alerts(state, "2026-10-02") == 1


def test_stale_snapshot_sends_nothing(monkeypatch):
    sent = _setup(monkeypatch, [_pick("AAA.NS", 70, "STRONG_BUY")], {"AAA.NS": "STRONG BUY"}, age_h=5)
    assert ca.check_verdict_alerts({}, "2026-10-02") == 0 and not sent


def test_verdict_alert_copy_is_descriptive(monkeypatch):
    from tests.test_alert_copy_no_advice import _assert_descriptive
    sent = _setup(monkeypatch, [_pick("AAA.NS", 70, "STRONG_BUY"), _pick("BBB.NS", 62, "BUY")],
                  {"AAA.NS": "STRONG BUY", "BBB.NS": "BUY"})
    ca.check_verdict_alerts({}, "2026-10-02")
    assert ca.check_test_verdict_alert({}, "2026-10-02") == 1
    assert len(sent) == 3
    for m in sent:
        _assert_descriptive(m)


def test_digest_lists_extra_strong_reads(monkeypatch):
    from tests.test_alert_copy_no_advice import _assert_descriptive
    buys = [_pick(f"T{i}.NS", 60 - i, "WATCHLIST") for i in range(7)]
    sent = _setup(monkeypatch, buys, {f"T{i}.NS": ("BUY" if i == 6 else "WATCH") for i in range(7)})
    monkeypatch.setattr(ca, "_verdict_of",
                        lambda b: (("BUY", 79) if b["ticker"] == "T6.NS" else ("WATCH", 60)))
    assert ca.check_delivery_digest({}, "2026-10-02", max_picks=5) == 1
    msg = sent[0]
    _assert_descriptive(msg)
    assert "Other names in this scan" in msg and "T6.NS" in msg and "T5.NS" not in msg


def test_digest_shows_sl_tp_and_escapes_html(monkeypatch):
    buys = [dict(_pick("J&KBANK.NS", 72, "BUY"), entry=140.0)]
    sent = _setup(monkeypatch, buys, {"J&KBANK.NS": "WATCH"})
    monkeypatch.setattr(ca, "_verdict_of", lambda b: ("WATCH", 60))
    assert ca.check_delivery_digest({}, "2026-10-02") == 1
    assert "J&amp;KBANK" in sent[0] and "J&KBANK" not in sent[0]
    assert "Stop 95.0" in sent[0] and "Target 110.0" in sent[0]


def test_test_digest_ignores_staleness_and_saves_nothing(monkeypatch):
    import trade_store
    sent = _setup(monkeypatch, [_pick("AAA.NS", 58, "WATCHLIST")], {"AAA.NS": "WATCH"}, age_h=40)
    saved = []
    monkeypatch.setattr(trade_store, "kv_set", lambda *a, **k: saved.append(a), raising=False)
    monkeypatch.setattr(ca, "_verdict_of", lambda b: ("WATCH", 60))
    assert ca.check_test_digest({}, "2026-10-03") == 1
    assert sent[0].startswith("🧪") and "TEST" in sent[0] and not saved


def test_digest_marks_under_70_watch_only_with_record(monkeypatch):
    from tests.test_alert_copy_no_advice import _assert_descriptive
    buys = [_pick("HI.NS", 74, "BUY"), _pick("LO.NS", 58, "WATCHLIST")]
    sent = _setup(monkeypatch, buys, {"HI.NS": "BUY", "LO.NS": "WATCH"})
    monkeypatch.setattr(ca, "_verdict_of", lambda b: ("WATCH", 60))
    monkeypatch.setattr(ca, "_under_floor_record",
                        lambda: {"n": 114, "beat_rate": 39.0, "mean_vs_nifty": -0.84})
    assert ca.check_delivery_digest({}, "2026-10-02") == 1
    msg = sent[0]
    _assert_descriptive(msg)
    assert "1 of 2 names scored under 70" in msg and "39%" in msg and "n=114" in msg
    lo_line = next(ln for ln in msg.splitlines() if "LO.NS" in ln)
    hi_line = next(ln for ln in msg.splitlines() if "HI.NS" in ln)
    assert "Watch only" in lo_line and "Watch only" not in hi_line


def test_digest_still_sends_when_nothing_reaches_70(monkeypatch):
    buys = [_pick(f"T{i}.NS", 57 - i, "WATCHLIST") for i in range(5)]
    sent = _setup(monkeypatch, buys, {f"T{i}.NS": "WATCH" for i in range(5)})
    monkeypatch.setattr(ca, "_verdict_of", lambda b: ("WATCH", 60))
    monkeypatch.setattr(ca, "_under_floor_record", lambda: None)   # ledger unreachable
    assert ca.check_delivery_digest({}, "2026-10-02") == 1
    assert "None of today's top names scored 70+" in sent[0]
    assert sum("Watch only" in ln for ln in sent[0].splitlines() if ".NS" in ln) == 5


def test_digest_has_no_watch_note_when_all_clear_70(monkeypatch):
    sent = _setup(monkeypatch, [_pick("HI.NS", 72, "BUY")], {"HI.NS": "BUY"})
    monkeypatch.setattr(ca, "_verdict_of", lambda b: ("WATCH", 60))
    monkeypatch.setattr(ca, "_under_floor_record", lambda: (_ for _ in ()).throw(AssertionError))
    assert ca.check_delivery_digest({}, "2026-10-02") == 1
    assert "Watch only" not in sent[0]


def test_under_70_gets_levels_to_watch_not_a_trade_plan(monkeypatch):
    from tests.test_alert_copy_no_advice import _assert_descriptive
    buys = [dict(_pick("LO.NS", 58, "WATCHLIST"), entry=100.0)]
    sent = _setup(monkeypatch, buys, {"LO.NS": "WATCH"})
    monkeypatch.setattr(ca, "_verdict_of", lambda b: ("WATCH", 60))
    monkeypatch.setattr(ca, "_under_floor_record", lambda: None)
    assert ca.check_delivery_digest({}, "2026-10-02") == 1
    msg = sent[0]
    _assert_descriptive(msg)
    assert "Levels to watch: 95.0 below · 110.0 above" in msg
    assert "Entry" not in msg and "Stop " not in msg and "Target" not in msg
    head = next(ln for ln in msg.splitlines() if "LO.NS" in ln)
    assert "— <i>Watch only</i> · Score 58/90" in head and "Watch ·" not in head


def test_footer_mentions_brackets_only_when_present(monkeypatch):
    sent = _setup(monkeypatch, [_pick("AAA.NS", 58, "WATCHLIST")], {"AAA.NS": "WATCH"})
    monkeypatch.setattr(ca, "_verdict_of", lambda b: ("WATCH", 60))
    monkeypatch.setattr(ca, "_under_floor_record", lambda: None)
    ca.check_delivery_digest({}, "2026-10-02")
    assert "brackets" not in sent[0]
    sent2 = _setup(monkeypatch, [_pick("BBB.NS", 72, "BUY")], {"BBB.NS": "BUY"})
    monkeypatch.setattr(ca, "_verdict_of", lambda b: ("BUY", 79))
    ca.check_delivery_digest({}, "2026-10-02")
    assert "(79)" in sent2[0] and "conviction in brackets" in sent2[0]


def test_scan_label_date_only_where_asked():
    gen = "2026-10-01T17:23:13+00:00"                      # 22:53 IST, Thu 1 Oct
    assert ca._snapshot_ist_label(gen) == "22:53 IST"
    assert ca._snapshot_ist_label(gen, with_date=True) == "22:53 IST, Thu 1 Oct"
    assert ca._snapshot_ist_label("garbage", with_date=True) == "unknown time"


def test_digest_and_test_header_show_scan_date(monkeypatch):
    import trade_store
    sent = _setup(monkeypatch, [_pick("AAA.NS", 72, "BUY")], {"AAA.NS": "BUY"}, age_h=40)
    monkeypatch.setattr(trade_store, "kv_set", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ca, "_verdict_of", lambda b: ("WATCH", 60))
    assert ca.check_test_digest({}, "2026-10-05") == 1
    hdr, sub = sent[0].splitlines()[:2]
    # "(… HH:MM IST, Ddd D Mon; may be stale)" and "(scan at HH:MM IST, Ddd D Mon)"
    import re
    assert re.search(r"\d\d:\d\d IST, [A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2}; may be stale", hdr)
    assert re.search(r"scan at \d\d:\d\d IST, [A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2}\)", sub)

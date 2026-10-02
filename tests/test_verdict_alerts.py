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
    buys = [dict(_pick("J&KBANK.NS", 58, "WATCHLIST"), entry=140.0)]
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

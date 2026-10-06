"""The scheduled Top Picks scan writes the shadow ledger itself (6 Oct 2026:
28 scans, 0 shadow rows, because only a Command Centre page view logged them)."""
import importlib


def test_log_shadow_picks_logs_each_buy_as_shadow_auto(monkeypatch):
    import analysis.verdict_ledger as VL
    calls = []
    monkeypatch.setattr(VL, "log_verdict", lambda **k: calls.append(k) or 1)
    wtp = importlib.import_module("scripts.warm_top_picks")
    buys = [{"ticker": "AAA", "score": 62, "action": "BUY", "entry": 100.0,
             "horizon": "Swing (3-10 trading days)"},
            {"ticker": "BBB.NS", "score": 71, "action": "STRONG BUY", "price": 50.0}]
    assert wtp.log_shadow_picks(buys) == 2
    assert [c["ticker"] for c in calls] == ["AAA.NS", "BBB.NS"]
    assert {c["source"] for c in calls} == {"shadow_auto"}
    assert calls[0]["entry_price"] == 100.0


def test_log_shadow_picks_never_raises(monkeypatch):
    import analysis.verdict_ledger as VL

    def boom(**k):
        raise RuntimeError("db down")
    monkeypatch.setattr(VL, "log_verdict", boom)
    wtp = importlib.import_module("scripts.warm_top_picks")
    assert wtp.log_shadow_picks([{"ticker": "AAA", "score": 60, "action": "BUY"}]) == 1
    assert wtp.log_shadow_picks(None) == 0

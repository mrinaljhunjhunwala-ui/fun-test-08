"""Alert HTML safety: outside text is escaped; a rejected HTML send falls back to plain text."""
import io
import urllib.error
from types import SimpleNamespace

import alerts.check_alerts as ca


def test_h_escapes_ampersand_and_tags():
    assert ca._h("J&KBANK") == "J&amp;KBANK"
    assert ca._h("<x>") == "&lt;x&gt;"


def test_plain_strips_tags_and_unescapes():
    assert ca._plain("<b>J&amp;KBANK</b> up") == "J&KBANK up"


def test_portfolio_posture_message_escapes():
    from analysis.portfolio_posture import format_portfolio_message
    h = SimpleNamespace(symbol="J&KBANK", posture="TRIM_WATCH", pnl_pct=-3.2, reason="below 50-SMA & weak")
    p = SimpleNamespace(symbol="M&M", posture="STOP_HIT", pnl_pct=-1.1, reason="hit <stop>")
    msg = format_portfolio_message([h], [p], only_actionable=False)
    assert "J&amp;KBANK" in msg and "M&amp;M" in msg and "P&amp;L" in msg
    assert "&lt;stop&gt;" in msg and " & " not in msg and "P&L" not in msg


def test_momentum_message_escapes():
    from analysis.momentum_scanner import format_momentum_message
    c = {"ticker": "J&KBANK", "price": 140.0, "breakout_pct": 1.2, "volume_ratio": 2.0, "rsi": 61,
         "rs_63d": 4.0, "suggested_entry": 140.0, "suggested_stop": 135.0, "suggested_target": 150.0}
    msg = format_momentum_message([c])
    assert "J&amp;KBANK" in msg


def test_telegram_falls_back_to_plain_text_on_400(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    calls = []

    class _Resp:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=15):
        calls.append(req.data.decode())
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b""))
        return _Resp()

    monkeypatch.setattr(ca.urllib.request, "urlopen", fake_urlopen)
    assert ca.send_telegram("<b>J&KBANK</b> up") is True
    assert len(calls) == 2
    assert "parse_mode" in calls[0] and "parse_mode" not in calls[1]


def test_test_posture_sends_hold_rows_without_dedup(monkeypatch):
    import analysis.portfolio_posture as pp
    import data.angel_fetcher as af
    import data.fetcher as df_
    hold = SimpleNamespace(symbol="J&KBANK", posture="HOLD", pnl_pct=2.0, reason="trend intact")
    monkeypatch.setattr(af, "get_holdings", lambda: [{"tradingsymbol": "J&KBANK"}])
    monkeypatch.setattr(af, "get_positions", lambda: {"day": []})
    monkeypatch.setattr(df_, "fetch_single", lambda *a, **k: None)
    monkeypatch.setattr(pp, "analyse_holdings", lambda *a, **k: [hold])
    monkeypatch.setattr(pp, "analyse_positions", lambda *a, **k: [])
    sent = []
    monkeypatch.setattr(ca, "dispatch", lambda m, **k: sent.append(m) or True)
    state = {"portfolio_posture": "2026-10-03"}            # real alert already fired today
    assert ca.check_portfolio_posture(state, "2026-10-03", test=True) == 1
    assert sent[0].startswith("🧪") and "J&amp;KBANK" in sent[0] and "HOLD" in sent[0]
    assert state == {"portfolio_posture": "2026-10-03"}     # unchanged
    assert ca.check_portfolio_posture(state, "2026-10-03") == 0   # real path still deduped


def test_telegram_other_errors_do_not_retry(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    calls = []

    def fake_urlopen(req, timeout=15):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

    monkeypatch.setattr(ca.urllib.request, "urlopen", fake_urlopen)
    assert ca.send_telegram("hi") is False and len(calls) == 1

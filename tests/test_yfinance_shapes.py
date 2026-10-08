"""yfinance 1.x response shapes (audit #31): requirements.txt pins >=1.0,<2,
so the fetcher/news code must accept what that version returns."""
import numpy as np
import pandas as pd

from data.fetcher import normalize_ohlcv_frame
from utils.news import _flatten_yf_news_item


def _yf1_download_frame():
    """Shape of yf.download("RELIANCE.NS", auto_adjust=False) in yfinance 1.x."""
    idx = pd.DatetimeIndex(["2026-10-05", "2026-10-06", "2026-10-07"], name="Date")
    cols = pd.MultiIndex.from_product(
        [["Adj Close", "Close", "High", "Low", "Open", "Volume"], ["RELIANCE.NS"]],
        names=["Price", "Ticker"])
    data = np.array([[99, 100, 101, 98, 99, 1000],
                     [100, 101, 102, 99, 100, 1100],
                     [101, 102, 103, 100, 101, 1200]], dtype=float)
    return pd.DataFrame(data, index=idx, columns=cols)


def test_download_multiindex_price_first_is_flattened():
    out = normalize_ohlcv_frame(_yf1_download_frame())
    assert not isinstance(out.columns, pd.MultiIndex)
    assert {"Open", "High", "Low", "Close", "Volume"}.issubset(out.columns)
    assert out["Close"].tolist() == [100, 101, 102]


def test_ticker_first_multiindex_is_flattened():
    df = _yf1_download_frame().swaplevel(axis=1)
    out = normalize_ohlcv_frame(df)
    assert out["Open"].tolist() == [99, 100, 101]


def test_history_tz_aware_index_becomes_naive_ist_dates():
    idx = pd.DatetimeIndex(["2026-10-06 00:00", "2026-10-07 00:00"]).tz_localize("Asia/Kolkata")
    df = pd.DataFrame({"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2],
                       "Volume": [5, 6]}, index=idx)
    out = normalize_ohlcv_frame(df)
    assert out.index.tz is None
    assert [d.date().isoformat() for d in out.index] == ["2026-10-06", "2026-10-07"]


def test_news_item_nested_content_shape():
    item = {"id": "x", "content": {
        "title": "Reliance results beat", "pubDate": "2026-10-07T05:30:00Z",
        "provider": {"displayName": "Reuters"},
        "canonicalUrl": {"url": "https://example.com/a"}}}
    f = _flatten_yf_news_item(item)
    assert f["title"] == "Reliance results beat"
    assert f["publisher"] == "Reuters"
    assert f["link"] == "https://example.com/a"
    assert f["ts"] > 0


def test_news_item_legacy_flat_shape():
    f = _flatten_yf_news_item({"title": "T", "publisher": "P", "link": "L",
                               "providerPublishTime": 123})
    assert (f["title"], f["publisher"], f["link"], f["ts"]) == ("T", "P", "L", 123)

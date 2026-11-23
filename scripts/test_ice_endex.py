"""TTF=F priced from ICE Endex (Amsterdam), with the NYMEX copy on Yahoo as the labelled fallback. No network."""
import datetime as dt
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.argv = sys.argv[:1]
import build_data as B  # noqa: E402
import ice_endex as I  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 23, 10, 50, tzinfo=UTC)          # Monday 11:50 Zurich


def ice_payload(path, timeout=20):
    if path.startswith("contract-data"):
        return [{"marketId": 1, "marketStrip": "Dec26", "lastPrice": 91.7, "volume": 53570,
                 "lastTime": "11/23/2026 10:38 AM GMT", "change": 3.67},
                {"marketId": 2, "marketStrip": "Jan27", "lastPrice": 91.4, "volume": 35000,
                 "lastTime": "11/23/2026 10:38 AM GMT", "change": 3.5}]
    if path.startswith("data/historical"):
        days = pd.bdate_range("2026-09-01", "2026-11-20")
        return {"bars": [[d.strftime("%a %b %d 00:00:00 %Y"), 80.0 + i * 0.1] for i, d in enumerate(days[:-1])]
                + [["Fri Nov 20 00:00:00 2026", 88.451]]}
    return {"bars": [["Sun Nov 22 22:50:00 2026", 95.0], ["Mon Nov 23 03:00:00 2026", 95.9],
                     ["Mon Nov 23 09:00:00 2026", 91.5], ["Mon Nov 23 10:30:00 2026", 91.8]]}


def yahoo_bars():
    days = pd.bdate_range("2026-09-01", "2026-11-20")
    df = pd.DataFrame({"open": 80.0, "high": 82.0, "low": 79.0, "close": 81.0, "volume": 100.0}, index=days)
    df.loc[pd.Timestamp("2026-11-20"), ["open", "high", "low", "close"]] = [85.525, 88.7, 85.525, 88.7]
    return df


def test_quote_uses_the_amsterdam_month_and_prior_settlement(monkeypatch):
    monkeypatch.setattr(I, "_get", ice_payload)
    q = I.quote("Dec-26", now=NOW)
    assert (q["label"], q["last"], q["prev_settle"], str(q["prev_date"])) == ("Dec-26", 91.7, 88.451, "2026-11-20")
    assert q["state"] == "open" and str(q["trade_date"]) == "2026-11-23"
    assert (q["open"], q["high"], q["low"]) == (95.0, 95.9, 91.5)
    assert I.quote("Jan-27", now=NOW)["last"] == 91.4


def test_row_is_ice_endex_with_settlement_closes(monkeypatch):
    monkeypatch.setattr(I, "_get", ice_payload)
    monkeypatch.setattr(B, "_fetch_contract_yahoo", lambda sym, *a: (yahoo_bars(), {"exchangeName": "NYM"}, "yahoo-chart"))
    futx = {"TTF=F": dict(cont=(yahoo_bars(), {}, "yahoo-chart"), sym="TTFZ26.NYM", month="Dec-26", expiry="2026-11-27")}
    df, meta, src = B.fetch_contract("TTF=F", None, None, None, NOW, futx)
    assert src == "ice-endex" and meta["exchangeName"] == "ICE Endex"
    assert df["close"].iloc[-1] == 91.7 and df["close"].iloc[-2] == 88.451     # ICE settle, not NYMEX's 88.70 last trade
    assert list(df.loc[pd.Timestamp("2026-11-20"), ["open", "high", "low"]]) == [85.525, 88.7, 85.525]
    spec = dict(sym="TTF=F", name="Dutch TTF gas", kind="future", note="x")
    row = B.analyse(spec, df, meta, src, NOW)
    B.apply_contract(row, spec, futx["TTF=F"], NOW)
    assert row["prev_close"] == 88.451 and round(row["pct"]["1D"], 2) == round((91.7 / 88.451 - 1) * 100, 2)
    assert row["state"] == "open" and row["market_time"].startswith("2026-11-23T10:38")
    assert row["exch"] == "ICE Endex" and row["contract"]["code"] == "TFM Dec-26" and row["name"] == "Dutch TTF gas Dec-26"
    assert row["ohlc"]["d"][-1] == [95.0, 95.9, 91.5, 91.7]


def test_falls_back_to_yahoo_and_says_so(monkeypatch):
    def down(path, timeout=20):
        raise IOError("ICE returned a non-JSON page (blocked or changed)")
    monkeypatch.setattr(I, "_get", down)
    y = yahoo_bars()
    monkeypatch.setattr(B, "_fetch_contract_yahoo", lambda sym, *a: (y, {"exchangeName": "NYM"}, "yahoo-chart"))
    futx = {"TTF=F": dict(cont=(y, {}, "yahoo-chart"), sym="TTFZ26.NYM", month="Dec-26", expiry="2026-11-27")}
    df, meta, src = B.fetch_contract("TTF=F", None, None, None, NOW, futx)
    assert src == "yahoo-chart" and df["close"].iloc[-1] == 88.7
    spec = dict(sym="TTF=F", name="Dutch TTF gas", kind="future", note="x")
    row = B.analyse(spec, df, meta, src, NOW)
    B.apply_contract(row, spec, futx["TTF=F"], NOW)
    assert row["exch"].startswith("NYMEX") and "unavailable" in row["note"] and "blocked" in row["europe_error"]


def test_stale_ice_quote_is_rejected(monkeypatch):
    monkeypatch.setattr(I, "_get", ice_payload)
    try:
        B.fetch_europe("TTF=F", "Dec-26", None, NOW + dt.timedelta(days=10))
    except LookupError as e:
        assert "days old" in str(e)
    else:
        raise AssertionError("stale quote accepted")

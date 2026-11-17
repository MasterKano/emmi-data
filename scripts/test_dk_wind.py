"""Tests for Energinet-primary / ENTSO-E-fallback DK wind merge, shared by EMMi builders."""
import datetime as dt
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dk_wind as W  # noqa: E402


def test_merge_prefers_settlement_then_realtime_then_entsoe():
    days = pd.to_datetime(["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"])
    settlement = pd.Series([10.0, np.nan], index=days[:2])
    realtime = pd.Series([11.0, 21.0, 31.0], index=days[:3])
    entsoe = pd.Series([12.0, 22.0, 32.0, 42.0], index=days)
    mw, src = W.merge_dk_wind(entsoe=entsoe, settlement=settlement, realtime=realtime)
    assert list(mw.values) == [10.0, 21.0, 31.0, 42.0]
    assert list(src.values) == [
        "energinet_settlement",
        "energinet_realtime",
        "energinet_realtime",
        "entsoe_a75",
    ]


def test_merge_falls_back_to_entsoe_when_energinet_missing():
    days = pd.to_datetime(["2026-10-01", "2026-10-02"])
    entsoe = pd.Series([100.0, 200.0], index=days)
    mw, src = W.merge_dk_wind(entsoe=entsoe, settlement=pd.Series(dtype=float), realtime=None)
    assert list(mw.values) == [100.0, 200.0]
    assert set(src.values) == {"entsoe_a75"}


def test_merge_extends_past_entsoe_cutoff():
    entsoe = pd.Series([100.0], index=pd.to_datetime(["2026-11-03"]))
    realtime = pd.Series([150.0, 160.0], index=pd.to_datetime(["2026-11-15", "2026-11-16"]))
    mw, src = W.merge_dk_wind(entsoe=entsoe, settlement=None, realtime=realtime)
    assert pd.Timestamp("2026-11-16") in mw.index
    assert mw.loc["2026-11-16"] == 160.0
    assert src.loc["2026-11-16"] == "energinet_realtime"
    assert src.loc["2026-11-03"] == "entsoe_a75"


def test_overlap_pct():
    a = pd.Series([105.0, 110.0], index=pd.to_datetime(["2026-11-01", "2026-11-02"]))
    b = pd.Series([100.0, 100.0, 100.0], index=pd.to_datetime(["2026-11-01", "2026-11-02", "2026-11-03"]))
    r = W.overlap_pct(a, b)
    assert r["n"] == 2 and r["start"] == "2026-11-01" and r["end"] == "2026-11-02"
    assert abs(r["mean_pct"] - 7.5) < 1e-9


def test_daily_average_requires_full_cover_for_settlement():
    # 24 hourly points, one missing: settlement (min_cover=1) drops the day.
    idx = pd.date_range("2026-08-01 22:00", periods=24, freq="h", tz="UTC")  # CET 2 Aug is 00-24 in summer
    # 1 Aug 2026 00:00 CET = 2026-07-31 22:00 UTC, 24 hours.
    idx = pd.date_range("2026-07-31 22:00", periods=24, freq="h", tz="UTC")
    s = pd.Series(100.0, index=idx)
    full = W.daily_average_mw(s, 3600, min_cover=1.0)
    assert list(full.index) == [pd.Timestamp("2026-08-01")]
    assert abs(float(full.iloc[0]) - 100.0) < 1e-9
    s.iloc[3] = np.nan
    dropped = W.daily_average_mw(s.dropna(), 3600, min_cover=1.0)
    assert dropped.empty


def test_daily_average_realtime_allows_2pct_gaps():
    # 5-minute points for 1 Aug 2026 CET (24h): 288 expected. 2% missing is 5.76, so 6 missing is just over 2%.
    idx = pd.date_range("2026-07-31 22:00", periods=288, freq="5min", tz="UTC")
    s = pd.Series(50.0, index=idx)
    # drop 5 points (~1.7%) — should pass 98% cover
    keep = s.drop(s.index[:5])
    ok = W.daily_average_mw(keep, 300, min_cover=W.DK_WIND_REALTIME_COVER)
    assert list(ok.index) == [pd.Timestamp("2026-08-01")]
    # drop 10 points (~3.5%) — should fail
    keep2 = s.drop(s.index[:10])
    bad = W.daily_average_mw(keep2, 300, min_cover=W.DK_WIND_REALTIME_COVER)
    assert bad.empty


def test_monthly_twh_requires_complete_month_and_is_dst_aware():
    # August 2026: 31 * 24h days, 100 MW constant -> 0.0744 TWh
    days = pd.date_range("2026-08-01", "2026-08-31", freq="D")
    mw = pd.Series(100.0, index=days)
    m = W.monthly_twh(mw)
    assert abs(m["2026-08"] - 100 * 31 * 24 / 1e6) < 1e-12
    # missing a day: month omitted
    m2 = W.monthly_twh(mw.iloc[:-1])
    assert "2026-08" not in m2
    # 25-hour October DST day (25 Oct 2026 is the last Sunday)
    # 24 Oct 00:00 CET = 22:00 UTC 23 Oct; 26 Oct 00:00 CET = 23:00 UTC 25 Oct (after the 02:00->01:00 step)
    # cet_hours(2026-10-25) should be 25
    assert W.cet_hours(dt.date(2026, 10, 25)) == 25
    assert W.cet_hours(dt.date(2026, 3, 29)) == 23


def test_daily_gwh_list_matches_mw_times_hours():
    days = [dt.date(2026, 11, 1), dt.date(2026, 11, 2)]
    mw = pd.Series([1000.0], index=pd.to_datetime(["2026-11-01"]))
    gwh = W.daily_gwh_list(mw, days)
    assert gwh[0] == 24.0 and gwh[1] is None


def test_monthly_from_constant_is_documented():
    assert W.DK_WIND_MONTHLY_FROM == "2026-08"


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
            n += 1
            print("ok", k)
    print(f"{n} tests passed")

"""Negative checks: gaps, a stale but hole-free series, and the 1991-2020 gate."""
from datetime import date, timedelta

import pandas as pd
import pytest

import daily_products as dp
import freshness
import update


def _daily(start, end, drop=None):
    days = pd.date_range(start, end, freq="D")
    if drop is not None:
        days = days[days != pd.Timestamp(drop)]
    frame = pd.DataFrame({
        "date": days,
        "wind100": 5.0,
        "temp": 1.0,
        "rain": 1.0,
    })
    return frame


def test_gap_free_stale_series_fails_freshness_not_the_gap_check():
    daily = _daily("1940-01-01", "2020-12-31")
    freshness.assert_contiguous(daily["date"])
    with pytest.raises(RuntimeError, match="stale"):
        freshness.assert_series_fresh(daily["date"].max(), date(2026, 11, 25))


def test_lag_ceiling_matches_documented_era5_delay():
    today = date(2026, 11, 21)
    assert freshness.series_lag_days(date(2026, 11, 15), today) == 6
    assert freshness.assert_series_fresh(date(2026, 11, 15), today) == 6
    with pytest.raises(RuntimeError, match="stale"):
        freshness.assert_series_fresh(today - timedelta(days=7), today)
    with pytest.raises(RuntimeError, match="after the run date"):
        freshness.assert_series_fresh(today + timedelta(days=1), today)
    assert freshness.NOMINAL_ERA5_LAG_DAYS == 5
    assert freshness.MAX_ERA5_LAG_DAYS == 6
    assert "5 days delay" in freshness.LAG_SOURCE
    assert "540c0178" in freshness.LAG_SOURCE


def test_interior_gap_fails_contiguous_check():
    daily = _daily("1940-01-01", "1940-01-10", drop="1940-01-05")
    with pytest.raises(RuntimeError, match="Incomplete 1940-to-latest"):
        freshness.assert_contiguous(daily["date"])


def test_incomplete_1991_2020_blocks_normals(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "BASE", tmp_path)
    daily = _daily("1991-01-01", "2020-12-31", drop="1995-06-15")
    with pytest.raises(RuntimeError, match="1991-2020"):
        dp.build_daily_normals(daily)


def test_1940_01_01_rain_may_be_blank_and_is_not_imputed():
    df = pd.DataFrame({
        "date": ["1940-01-01", "1940-01-02"],
        "wind100": [1.0, 1.0],
        "temp": [1.0, 1.0],
        "rain": [float("nan"), 2.0],
    })
    ok = update.complete_point_rows(df)
    assert bool(ok.iloc[0]) and bool(ok.iloc[1])
    df.loc[1, "rain"] = float("nan")
    assert not bool(update.complete_point_rows(df).iloc[1])


def test_climatology_window_must_be_present():
    daily = _daily("1940-01-01", "2026-11-15", drop="2001-03-03")
    with pytest.raises(RuntimeError, match="1991-2020"):
        freshness.assert_climatology_present(daily["date"])

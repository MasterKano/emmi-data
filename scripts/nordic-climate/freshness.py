"""Freshness gate for the Nordic ERA5 series.

The historical gap check (1940-01-01 through the last date, with no holes) can
pass on a stale archive that simply stopped updating. This module rejects that
case. It does not change aggregation, calibration, or the published columns.

Lag
---
Open-Meteo documents ERA5 as updated daily with a 5-day delay:

- Historical Weather API, data-sources table, "ERA5 … Daily with 5 days delay"
  https://open-meteo.com/en/docs/historical-weather-api
- Open-Meteo open-data model table, ``copernicus_era5``, delay 5 days, and the
  note that ERA5 daily updates land with a 5–7 day delay
  https://github.com/open-meteo/open-data

The published ``nordic-climate/mtd.json`` note says the same thing ("ERA5 on
Open-Meteo lags ~5 days"). The 21 Nov 2026 production publish (emmi-data
``540c0178``) ended on 2026-11-15, a lag of 6 days.

``MAX_ERA5_LAG_DAYS`` is 6: it accepts that observed publish and the documented
about-five-to-six-day lag, and it rejects an older end date even when the
series has no internal gaps.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

NOMINAL_ERA5_LAG_DAYS = 5
MAX_ERA5_LAG_DAYS = 6
LAG_SOURCE = (
    "Open-Meteo Historical Weather API documents ERA5 as 'Daily with 5 days delay' "
    "(https://open-meteo.com/en/docs/historical-weather-api, data-sources table; "
    "also https://github.com/open-meteo/open-data copernicus_era5). "
    "The published mtd.json note says '~5 days'. "
    "emmi-data 540c0178 (21 Nov 2026) ended on 2026-11-15, a 6-day lag. "
    "MAX_ERA5_LAG_DAYS=6 is that ceiling."
)


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date"):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def series_lag_days(end, today: date) -> int:
    return (today - _as_date(end)).days


def assert_series_fresh(end, today: date, *, max_lag: int = MAX_ERA5_LAG_DAYS) -> int:
    """Fail if the latest day is older than the ERA5 lag, or in the future.

    ``today`` is the pipeline clock (runner date, UTC on GitHub-hosted runners).
    The Saturday / 11th schedules fire at 05:05 Europe/Zurich, which is still
    the same calendar date in UTC.
    """
    end_d = _as_date(end)
    lag = (today - end_d).days
    if lag < 0:
        raise RuntimeError(
            f"daily series ends {end_d.isoformat()}, after the run date {today.isoformat()}"
        )
    if lag > max_lag:
        raise RuntimeError(
            f"ERA5 series is stale: ends {end_d.isoformat()}, run date {today.isoformat()}, "
            f"lag {lag} days (maximum {max_lag}). A gap-free history is not enough. {LAG_SOURCE}"
        )
    return lag


def assert_contiguous(dates, start: str = "1940-01-01") -> None:
    """Same refusal as update.main: every day from 1940-01-01 through the last day."""
    idx = pd.DatetimeIndex(pd.to_datetime(dates))
    if len(idx) == 0:
        raise RuntimeError("Incomplete 1940-to-latest daily coverage; refusing product migration")
    expected = pd.date_range(start, idx.max(), freq="D")
    if not idx.equals(expected):
        raise RuntimeError("Incomplete 1940-to-latest daily coverage; refusing product migration")


def assert_climatology_present(dates) -> None:
    """1991–2020 must be complete or normals cannot be rebuilt."""
    idx = pd.DatetimeIndex(pd.to_datetime(dates))
    expected = pd.date_range("1991-01-01", "2020-12-31", freq="D")
    missing = expected.difference(idx)
    if len(missing):
        first = missing[0].strftime("%Y-%m-%d")
        raise RuntimeError(
            f"1991-2020 normals cannot be built; missing {len(missing)} days, first {first}"
        )


def assert_normals_file(path) -> None:
    """The rebuilt normals file has 366 calendar days and February 29 has 8 years."""
    frame = pd.read_csv(path)
    if len(frame) != 366:
        raise RuntimeError(f"daily_normals.csv has {len(frame)} rows, expected 366")
    feb29 = frame[(frame["month"] == 2) & (frame["day"] == 29)]
    if len(feb29) != 1 or int(feb29["n_years"].iloc[0]) != 8:
        raise RuntimeError("February 29 normal must be present with n_years=8")
    others = frame[~((frame["month"] == 2) & (frame["day"] == 29))]
    if not others["n_years"].eq(30).all():
        raise RuntimeError("1991-2020 normals must have 30 years except February 29")

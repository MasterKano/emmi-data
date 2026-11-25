"""Cache rules: partial months are not stored, numpy ints stay JSON-safe."""
import gzip
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

import fetch_daily_history as fh
import make_fixture_cache
import update


def test_fixture_january_1940_is_complete_with_blank_rain(tmp_path):
    path = tmp_path / "om_1940_01.csv"
    make_fixture_cache.write_month(path, 1940, 1)
    assert fh.month_complete(path, 1940, 1)
    frame = pd.read_csv(path)
    blank = frame[frame["date"] == "1940-01-01"]["rain"]
    assert blank.isna().all()
    later = frame[frame["date"] == "1940-01-02"]["rain"]
    assert later.notna().all()


def test_partial_current_month_is_not_a_complete_cache(tmp_path):
    path = tmp_path / "om_2026_11.csv"
    make_fixture_cache.write_month(path, 2026, 11, last_day=15)
    assert not fh.month_complete(path, 2026, 11)


def test_om_month_does_not_cache_a_partial_month(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "CACHE", tmp_path)
    update._MEM.clear()
    partial = pd.DataFrame({
        "date": ["2026-11-01"],
        "temp": [1.0],
        "rain": [1.0],
        "wind100": [1.0],
        "lat": [55],
        "lon": [4],
    })
    monkeypatch.setattr(update, "om_fetch", lambda start, end: partial)
    update.om_month(2026, 11, date(2026, 11, 5))
    assert not (tmp_path / "om_2026_11.csv").exists()


def _locations(params, wind_none=False):
    lats = [int(x) for x in params["latitude"].split(",")]
    lons = [int(x) for x in params["longitude"].split(",")]
    hours = [f"2024-06-01T{h:02d}:00" for h in range(24)]
    wind = [1.0] * 24
    if wind_none:
        wind[3] = None
    out = []
    for lat, lon in zip(lats, lons):
        out.append({
            "latitude": lat,
            "longitude": lon,
            "hourly": {"time": hours, "wind_speed_100m": list(wind)},
            "daily": {
                "time": ["2024-06-01"],
                "temperature_2m_mean": [10.0],
                "precipitation_sum": [1.0],
            },
        })
    return out


class _Resp:
    status_code = 200
    headers = {}
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def test_incomplete_hourly_response_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "CACHE", tmp_path)
    update._RL.update(remaining=None, reset=None, cost=None)

    def fake_get(url, params, timeout):
        return _Resp(_locations(params, wind_none=True))

    monkeypatch.setattr(update.requests, "get", fake_get)
    update.om_fetch(date(2024, 6, 1), date(2024, 6, 1))
    assert list((tmp_path / "hourly_raw").glob("*.json.gz")) == []


def test_complete_hourly_response_is_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "CACHE", tmp_path)
    update._RL.update(remaining=None, reset=None, cost=None)

    def fake_get(url, params, timeout):
        return _Resp(_locations(params, wind_none=False))

    monkeypatch.setattr(update.requests, "get", fake_get)
    update.om_fetch(date(2024, 6, 1), date(2024, 6, 1))
    files = list((tmp_path / "hourly_raw").glob("*.json.gz"))
    assert len(files) == 6
    with gzip.open(files[0], "rt") as fh:
        assert isinstance(json.load(fh), list)


def test_connection_timeout_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "CACHE", tmp_path)
    update._RL.update(remaining=None, reset=None, cost=None)
    calls = {"n": 0}

    def fake_get(url, params, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise update.requests.Timeout("handshake")
        return _Resp(_locations(params, wind_none=False))

    monkeypatch.setattr(update.requests, "get", fake_get)
    monkeypatch.setattr(update.time, "sleep", lambda s: None)
    update.om_fetch(date(2024, 6, 1), date(2024, 6, 1))
    assert calls["n"] == 7  # one timeout, then six successful batches


def test_climo_stats_json_has_plain_ints():
    master = pd.DataFrame({
        "month_num": [11] * 5,
        "year": np.array([1991, 1992, 1993, 2000, 2024], dtype=np.int64),
        "wind100": np.array([1.0, 2.0, 3.0, 4.0, 5.0]),
        "temp": np.zeros(5),
        "rain": np.ones(5),
    })
    stats = update.climo_stats(master, 11, "wind100", np.float64(3.0))
    json.dumps(stats)
    assert type(stats["min_year"]) is int
    assert type(stats["n_years_climo"]) is int
    assert type(stats["value"]) is float

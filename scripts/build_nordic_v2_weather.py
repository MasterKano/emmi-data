#!/usr/bin/env python3
"""Refresh nordic-v2 weather.json and monthly_climate.json from Open-Meteo ERA5.

The Energy weather charts read these files from main. This script is the daily
GitHub Actions updater; it uses the same archive API, 72-point grid, country
masks and 2006–2025 baseline as the box Nordic v2 builder. Only complete UTC
days (all required points, 24 hourly wind observations) are kept. Missing days
stay missing: nothing is invented or carried forward. ERA5 usually lags about
five days, so "as of" will trail the calendar.

Exit 0 when a published file changed, 3 when the latest complete day is
already published (the workflow then skips the commit).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
NORDIC = ROOT / "nordic-v2"
VARS = ["wind100", "temp", "rain"]
UNITS = {"wind100": "m/s at 100m", "temp": "degC at 2m", "rain": "mm/day"}
OM_URL = "https://archive-api.open-meteo.com/v1/archive"
SOURCE = "https://archive-api.open-meteo.com/v1/archive?models=era5"
BASELINE = (2006, 2025)
WINDOW_DAYS = 122
MONTHLY_START = pd.Timestamp("2021-01-01")


def log(msg):
    print(msg, flush=True)


def clean(v):
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return round(float(v), 3) if np.isfinite(v) else None
    if isinstance(v, (pd.Timestamp, dt.date, dt.datetime)):
        return v.isoformat()
    return v


def write_json(path: Path, obj):
    text = json.dumps(clean(obj), separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
    log(f"{path.name}: {len(text):,} bytes")
    return text


def load_grid():
    return json.loads((NORDIC / "weather_grid.json").read_text())


def load_history(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path, parse_dates=["date"])
    if not set(["date", *VARS, "region"]).issubset(d.columns):
        raise SystemExit(f"{path} missing required columns")
    return d.sort_values(["region", "date"])


def save_history(path: Path, d: pd.DataFrame):
    out = d.sort_values(["region", "date"]).copy()
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    tmp = path.with_suffix(".csv.tmp")
    out.to_csv(tmp, index=False, float_format="%.8f")
    tmp.replace(path)


def fetch_era5(start: dt.date, end: dt.date, lats, lons, cache_dir: Path | None) -> pd.DataFrame:
    """Hourly 100 m wind + daily 2 m temperature and rain; one row per point and UTC day."""
    pts = [(la, lo) for la in lats for lo in lons]
    frames = []
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
    for batch in range(0, len(pts), 12):
        sub = pts[batch:batch + 12]
        cache = (cache_dir / f"era5_{start}_{end}_b{batch:02d}.json") if cache_dir else None
        if cache and cache.exists():
            locations = json.loads(cache.read_text())
        else:
            params = {
                "latitude": ",".join(str(x[0]) for x in sub),
                "longitude": ",".join(str(x[1]) for x in sub),
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "hourly": "wind_speed_100m",
                "daily": "temperature_2m_mean,precipitation_sum",
                "wind_speed_unit": "ms",
                "models": "era5",
                "timezone": "GMT",
            }
            for attempt in range(12):
                r = requests.get(OM_URL, params=params, timeout=240)
                if r.status_code == 429:
                    wait = max(65 * (attempt + 1), float(r.headers.get("Retry-After", 0) or 0))
                    log(f"Open-Meteo 429; backoff {wait}s")
                    time.sleep(wait)
                    continue
                if r.status_code >= 500:
                    time.sleep(min(120, 5 * 2**attempt))
                    continue
                r.raise_for_status()
                try:
                    locations = r.json()
                except requests.exceptions.JSONDecodeError:
                    body = r.text[:160]
                    log(f"Archive returned non-JSON (status {r.status_code}): {body!r}")
                    if attempt >= 2:
                        raise
                    time.sleep(5 * (attempt + 1))
                    continue
                break
            else:
                raise RuntimeError("Open-Meteo retries exhausted")
            if not isinstance(locations, list) or len(locations) != len(sub):
                raise RuntimeError("Open-Meteo coordinate batch mismatch")
            if cache:
                cache.write_text(json.dumps(locations, separators=(",", ":")))
            log(f"100m {start}..{end}: points {batch+1}-{batch+len(sub)} fetched")
        for (lat, lon), loc in zip(sub, locations):
            h = loc["hourly"]
            hourly = pd.DataFrame({"time": pd.to_datetime(h["time"]), "wind100": h["wind_speed_100m"]})
            expected_hours = pd.date_range(start, end + dt.timedelta(days=1), freq="h", inclusive="left")
            if not pd.DatetimeIndex(hourly["time"]).equals(expected_hours):
                raise RuntimeError("Missing or duplicate UTC hourly timestamps")
            hourly["date"] = hourly["time"].dt.strftime("%Y-%m-%d")
            grouped = hourly.groupby("date")["wind100"].agg(["mean", "count"])
            grouped.loc[grouped["count"] != 24, "mean"] = np.nan
            dly = loc["daily"]
            frame = pd.DataFrame({"date": dly["time"], "temp": dly["temperature_2m_mean"], "rain": dly["precipitation_sum"]})
            frame["wind100"] = frame["date"].map(grouped["mean"])
            frame["lat"], frame["lon"] = loc.get("latitude", lat), loc.get("longitude", lon)
            frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def regional_means(points: pd.DataFrame, grid) -> pd.DataFrame:
    lats, lons = grid["lats"], grid["lons"]
    d = points.copy()
    d["la"] = d.lat.map(lambda x: min(lats, key=lambda a: abs(a - x)))
    d["lo"] = d.lon.map(lambda x: min(lons, key=lambda a: abs(a - x)))
    if ((d.lat - d.la).abs().max() > 0.6) or ((d.lon - d.lo).abs().max() > 0.6):
        raise RuntimeError("Cache coordinates outside expected ERA5 grid")
    frames = []
    for c, raw in grid["weights"].items():
        w = {(float(a), float(b)): float(v) for a, b, v in raw}
        x = d.copy()
        x["w"] = [w.get((float(a), float(b)), 0.0) for a, b in zip(x.la, x.lo)]
        x = x[x.w > 0].drop_duplicates(["date", "la", "lo"])
        count = sum(v > 0 for v in w.values())
        x = x[x.groupby("date")["w"].transform("size").eq(count)]
        for k in VARS:
            x[k] = x[k] * x.w
        sums = x.groupby("date")[[*VARS, "w"]].sum(min_count=count)
        result = sums[list(VARS)].div(sums.w, axis=0).dropna(how="all").reset_index()
        result["region"] = c
        frames.append(result)
    if not frames:
        return pd.DataFrame(columns=["date", *VARS, "region"])
    result = pd.concat(frames, ignore_index=True)
    result["date"] = pd.to_datetime(result["date"])
    return result


def trailing(s):
    if s.empty:
        return s
    return s.reindex(pd.date_range(s.index.min(), s.index.max())).rolling(30, min_periods=30).mean()


def summary(v, n, unit):
    return dict(
        value=v,
        normal=n,
        difference=(v - n if pd.notna(v) and pd.notna(n) else None),
        difference_unit=unit,
        difference_pct=((v / n - 1) * 100 if unit != "degC" and pd.notna(v) and pd.notna(n) and n != 0 else None),
    )


def build_weather(d: pd.DataFrame, grid) -> dict:
    result = {
        "schema_version": 1,
        "as_of": str(d.date.max().date()),
        "baseline": list(BASELINE),
        "history_start": grid["history_start"],
        "units": UNITS,
        "source": SOURCE,
        "method": grid["method"],
        "grid": grid["grid"],
        "point_counts": grid["point_counts"],
        "columns": ["date", "value", "ma30", "normal", "normal_ma30"],
        "regions": {},
    }
    for c, g in d.groupby("region"):
        g = g.sort_values("date").set_index("date")
        idx = pd.date_range(g.index.min(), g.index.max())
        g = g.reindex(idx)
        b = g.loc[f"{BASELINE[0]}-01-01":f"{BASELINE[1]}-12-31"]
        expected = pd.date_range(f"{BASELINE[0]}-01-01", f"{BASELINE[1]}-12-31")
        if len(b) != len(expected) or b[VARS].isna().any().any():
            raise ValueError(f"incomplete baseline {c}")
        norm = b.groupby(b.index.strftime("%m-%d"))[VARS].mean()
        reg = {"as_of": str(g[VARS].dropna().index.max().date()), "metrics": {}}
        for k in VARS:
            s = g[k]
            n = pd.Series([norm.loc[t.strftime("%m-%d"), k] for t in idx], index=idx)
            v30, n30 = trailing(s), trailing(n)
            end = s.dropna().index.max()
            start = end - pd.Timedelta(days=WINDOW_DAYS)
            dates = idx[(idx >= start) & (idx <= end)]
            starts = grid.get("metric_history_start") or {}
            reg["metrics"][k] = {
                "as_of": str(end.date()),
                "history_start": starts.get(k, grid["history_start"]),
                "summary": summary(v30.loc[end], n30.loc[end], UNITS[k]),
                "rows": [[str(t.date()), s.loc[t], v30.loc[t], n.loc[t], n30.loc[t]] for t in dates],
            }
        result["regions"][c] = reg
    return result


def build_monthly_climate(d: pd.DataFrame, grid) -> dict:
    result = {
        "schema_version": 1,
        "as_of": str(d.date.max().date()),
        "baseline": list(BASELINE),
        "history_start": "2021-01",
        "units": UNITS,
        "source": SOURCE,
        "method": grid["method"],
        "grid": grid["grid"],
        "point_counts": grid["point_counts"],
        "aggregation": "mean of daily values in the month (or the available days if partial); rain stays mm/day, not a monthly total",
        "columns": ["month", "value", "normal", "min", "max", "difference", "difference_pct", "days_covered", "days_in_month", "partial"],
        "regions": {},
    }
    for c, g in d.groupby("region"):
        g = g.sort_values("date").set_index("date")
        end = g[VARS].dropna(how="all").index.max()
        months = pd.period_range(MONTHLY_START, end, freq="M")
        metrics = {k: {"as_of": str(g[k].dropna().index.max().date()) if g[k].notna().any() else None, "rows": []} for k in VARS}
        for period in months:
            days = pd.date_range(period.start_time, period.end_time, freq="D")
            days = days[days <= end]
            days_in_month = period.days_in_month
            days_covered = int(g.loc[days, VARS].dropna(how="all").shape[0]) if len(days) else 0
            partial = len(days) < days_in_month
            for k in VARS:
                cur = g.reindex(days)[k]
                value = float(cur.mean()) if cur.notna().sum() == len(days) and len(days) else None
                yearly = []
                for year in range(BASELINE[0], BASELINE[1] + 1):
                    use = []
                    for t in days:
                        try:
                            use.append(t.replace(year=year))
                        except ValueError:
                            continue
                    hs = g.reindex(use)[k]
                    if hs.notna().sum() == len(use) and use:
                        yearly.append(float(hs.mean()))
                normal = float(np.mean(yearly)) if yearly else None
                mn = float(np.min(yearly)) if yearly else None
                mx = float(np.max(yearly)) if yearly else None
                diff = (value - normal) if value is not None and normal is not None else None
                pct = ((value / normal - 1) * 100 if k != "temp" and value is not None and normal not in (None, 0) else None)
                metrics[k]["rows"].append([
                    period.strftime("%Y-%m"), value, normal, mn, mx, diff, pct,
                    int(cur.notna().sum()), days_in_month, partial,
                ])
        result["regions"][c] = {"as_of": str(end.date()), "metrics": metrics}
    return result


def update_manifest(files):
    path = NORDIC / "manifest.json"
    man = json.loads(path.read_text())
    for name, raw in files.items():
        d = json.loads(raw)
        man["files"][name] = {"bytes": len(raw.encode("utf-8") if isinstance(raw, str) else raw), "sha256": hashlib.sha256(raw.encode("utf-8") if isinstance(raw, str) else raw).hexdigest(), "as_of": d["as_of"]}
    # Manifest as_of/built_at belong to the last full Nordic v2 pack; only the weather file entries change here.
    path.write_text(json.dumps(man, separators=(",", ":"), ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", type=Path, default=NORDIC / "regional_daily.csv")
    ap.add_argument("--out", type=Path, default=NORDIC)
    ap.add_argument("--cache-dir", type=Path, default=None)
    ap.add_argument("--no-fetch", action="store_true", help="rebuild from the history file only")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD (default: today UTC)")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    grid = load_grid()
    history = load_history(args.history)
    last = history.date.max().date()
    log(f"history through {last}; today {today}")
    if not args.no_fetch:
        # Re-request a short overlap so a day that was incomplete can fill in; never drop an already-complete day if the new pull is empty.
        start = min(last - dt.timedelta(days=7), dt.date(today.year, today.month, 1))
        if start < last + dt.timedelta(days=1) or start <= today:
            start = min(start, today)
            if start <= today:
                points = fetch_era5(start, today, grid["lats"], grid["lons"], args.cache_dir)
                fresh = regional_means(points, grid)
                if not fresh.empty:
                    # Keep existing complete rows when the new pull has no values for that date.
                    fresh = fresh.dropna(subset=VARS, how="all")
                    before = history.copy()
                    # For overlapping dates, prefer a complete fresh row.
                    old = history.copy()
                    old["_k"] = old.date.dt.strftime("%Y-%m-%d") + "|" + old.region
                    fresh = fresh.copy()
                    fresh["_k"] = fresh.date.dt.strftime("%Y-%m-%d") + "|" + fresh.region
                    replaced = set(fresh["_k"])
                    keep = old[~old["_k"].isin(replaced)].drop(columns="_k")
                    history = pd.concat([keep, fresh.drop(columns="_k")], ignore_index=True).sort_values(["region", "date"])
                    added = sorted(set(fresh.date.dt.date) - set(before.date.dt.date))
                    log("new complete days: " + (", ".join(str(x) for x in added) if added else "none (overlap only)"))
    last = history.date.max().date()
    weather = build_weather(history, grid)
    monthly = build_monthly_climate(history, grid)
    args.out.mkdir(parents=True, exist_ok=True)
    wx_path, mo_path = args.out / "weather.json", args.out / "monthly_climate.json"
    prev_wx = wx_path.read_bytes() if wx_path.exists() else b""
    prev_mo = mo_path.read_bytes() if mo_path.exists() else b""
    wx_text = write_json(wx_path, weather)
    mo_text = write_json(mo_path, monthly)
    save_history(args.history, history)
    if args.out.resolve() == NORDIC.resolve():
        update_manifest({"weather.json": wx_text, "monthly_climate.json": mo_text})
    changed = wx_path.read_bytes() != prev_wx or mo_path.read_bytes() != prev_mo
    log(f"as_of {weather['as_of']}; {'updated' if changed else 'unchanged'}")
    if not changed:
        sys.exit(3)


if __name__ == "__main__":
    main()

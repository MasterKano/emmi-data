#!/usr/bin/env python3
"""Download Open-Meteo ERA5 daily history for the Nordic 72-point grid.

Resumable: complete months already in cache/openmeteo100m/om_YYYY_MM.csv are skipped.
Raw per-point files stay out of the public repo. Reuses update.om_fetch.

The cache directory is update.CACHE (env NORDIC_CLIMATE_CACHE), read at call
time so a restored seed is visible after import.

Usage:
  python fetch_daily_history.py
  python fetch_daily_history.py --start 1991 --end 2023
"""
from __future__ import annotations

import argparse
import calendar
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

import update


def cache_dir() -> Path:
    return update.CACHE


LOG = Path(__file__).resolve().parent / "fetch_history.log"


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    with LOG.open("a") as f:
        f.write(msg + "\n")


def month_complete(path: Path, y: int, m: int) -> bool:
    if not path.exists() or path.stat().st_size < 100:
        return False
    try:
        df = pd.read_csv(path)
    except Exception:
        return False
    need = calendar.monthrange(y, m)[1] * len(update.LATS) * len(update.LONS)
    if len(df) != need:
        return False
    if not set(["date", "lat", "lon", *update.VARS]).issubset(df.columns):
        return False
    if not update.complete_point_rows(df).all():
        return False
    dates = pd.to_datetime(df["date"])
    if dates.min().date() != date(y, m, 1) or dates.max().date() != date(y, m, calendar.monthrange(y, m)[1]):
        return False
    if df.duplicated(["date", "lat", "lon"]).any():
        return False
    if not df.groupby("date").size().eq(len(update.LATS) * len(update.LONS)).all():
        return False
    if df.groupby(["lat", "lon"]).ngroups != len(update.LATS) * len(update.LONS):
        return False
    return True


def write_month(df: pd.DataFrame, y: int, m: int) -> None:
    cache = cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"om_{y}_{m:02d}.csv"
    start = date(y, m, 1)
    end = date(y, m, calendar.monthrange(y, m)[1])
    sub = df[(df["date"] >= start.isoformat()) & (df["date"] <= end.isoformat())].copy()
    sub = sub.sort_values(["lat", "lon", "date"])
    tmp = path.with_suffix(path.suffix + ".tmp")
    sub.to_csv(tmp, index=False)
    tmp.replace(path)
    if not month_complete(path, y, m):
        path.unlink(missing_ok=True)
        raise RuntimeError(f"cache validation failed for {y}-{m:02d} (rows={len(sub)})")


def fetch_range(start: date, end: date, pause: float) -> pd.DataFrame:
    log(f"  fetching Open-Meteo ERA5 {start.isoformat()} .. {end.isoformat()}")
    t0 = time.time()
    df = update.om_fetch(start, end)
    log(f"  got {len(df)} rows in {time.time()-t0:.1f}s")
    if pause > 0:
        time.sleep(pause)
    return df


def missing_months(start_year: int, end_year: int) -> list[tuple[int, int]]:
    out = []
    cache = cache_dir()
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            if not month_complete(cache / f"om_{y}_{m:02d}.csv", y, m):
                out.append((y, m))
    return out


def fill_year(y: int, missing: list[int], pause: float) -> None:
    """Fetch a whole year when several months are missing, else month by month."""
    if len(missing) >= 3:
        last_m = max(missing)
        df = fetch_range(date(y, min(missing), 1), date(y, last_m, calendar.monthrange(y, last_m)[1]), pause)
        for m in missing:
            write_month(df, y, m)
            log(f"  wrote om_{y}_{m:02d}.csv")
        return
    for m in missing:
        end = date(y, m, calendar.monthrange(y, m)[1])
        df = fetch_range(date(y, m, 1), end, pause)
        write_month(df, y, m)
        log(f"  wrote om_{y}_{m:02d}.csv")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=int, default=1940)
    p.add_argument("--end", type=int, default=date.today().year - 1)
    p.add_argument("--pause", type=float, default=3.0, help="seconds between successful requests")
    args = p.parse_args()
    missing = missing_months(args.start, args.end)
    total_months = (args.end - args.start + 1) * 12
    log(f"cache {cache_dir()}: {total_months - len(missing)}/{total_months} months already complete; {len(missing)} to fetch")
    if not missing:
        log("nothing to fetch")
        return 0
    by_year: dict[int, list[int]] = {}
    for y, m in missing:
        by_year.setdefault(y, []).append(m)
    for y, months in by_year.items():
        try:
            fill_year(y, months, args.pause)
        except Exception as e:
            log(f"ERROR year {y}: {type(e).__name__}: {e}")
            return 1
    still = missing_months(args.start, args.end)
    if still:
        log(f"still missing {len(still)} months: {still[:12]}...")
        return 1
    log("history cache complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())

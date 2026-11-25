#!/usr/bin/env python3
"""Write a deterministic synthetic openmeteo100m monthly cache.

Used by the hosted fixture dry run and nowhere in production. Complete months
from 1940-01 through the month before ``--today`` satisfy ``month_complete``.
The current month is a partial CSV so ``om_month`` will not call Open-Meteo.
1940-01-01 rain is blank. Values are not ERA5.
"""
from __future__ import annotations

import argparse
import calendar
import csv
from datetime import date
from pathlib import Path

LATS = list(range(55, 72, 2))
LONS = list(range(4, 33, 4))


def _month_iter(start: tuple[int, int], end: tuple[int, int]):
    y, m = start
    while (y, m) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def write_month(path: Path, y: int, m: int, last_day: int | None = None) -> None:
    nd = calendar.monthrange(y, m)[1]
    end_d = nd if last_day is None else min(last_day, nd)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "temp", "rain", "wind100", "lat", "lon"])
        for lat in LATS:
            for lon in LONS:
                for day in range(1, end_d + 1):
                    if (y, m, day) == (1940, 1, 1):
                        rain = ""
                    else:
                        rain = f"{((day + lat + lon) % 9) + 0.2:.1f}"
                    wind = 4.0 + (lat - 55) * 0.05 + (day % 7) * 0.1 + (lon % 8) * 0.02
                    temp = -8.0 + (lat - 55) * 0.15 + (day - 15) * 0.25 + ((m - 6) * 1.2)
                    writer.writerow(
                        [f"{y:04d}-{m:02d}-{day:02d}", f"{temp:.4f}", rain, f"{wind:.4f}", lat, lon]
                    )
    tmp.replace(path)


def build(cache: Path, today: date) -> None:
    last_complete = today.replace(day=1)
    # Month before today.
    if last_complete.month == 1:
        end = (last_complete.year - 1, 12)
    else:
        end = (last_complete.year, last_complete.month - 1)
    for y, m in _month_iter((1940, 1), end):
        write_month(cache / f"om_{y}_{m:02d}.csv", y, m)
    # Partial current month, present so the pipeline does not hit the network.
    write_month(cache / f"om_{today.year}_{today.month:02d}.csv", today.year, today.month, today.day)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--today", required=True, help="YYYY-MM-DD fixture clock")
    parser.add_argument("--cache", required=True, type=Path)
    args = parser.parse_args()
    today = date.fromisoformat(args.today)
    build(args.cache, today)
    n = len(list(args.cache.glob("om_*.csv")))
    print(f"fixture cache: {n} files in {args.cache}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

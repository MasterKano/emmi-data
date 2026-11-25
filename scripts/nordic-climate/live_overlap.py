#!/usr/bin/env python3
"""Live Open-Meteo dry run for the published overlap. Does not publish.

Fetches ERA5 for 2026-10-01..2026-11-15 (the dates in emmi-data 540c0178 that
are recent enough to re-request cheaply) and rebuilds daily grid means with
the production calibration. Shared dates must match the published
daily_grid_mean.csv at 2 decimal places.

Also requests the single day ``today - MAX_ERA5_LAG_DAYS``. If that complete
UTC day is missing, the archive is outside the lag this migration accepts and
the job fails. No historical seed is required.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import daily_products as dp
import freshness
import update

PUBLISHED_OVERLAP = (date(2026, 10, 1), date(2026, 11, 15))


def _summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


def _cal() -> dict:
    cal = json.loads((HERE / "calibration.json").read_text())
    cal["vars"]["wind100"] = {
        "scheme": "coslat",
        "slope": 1.0,
        "intercept": 0.0,
        "note": "Raw ERA5 100m wind, not calibrated to 10m history",
    }
    return cal


def _means(start: date, end: date, cal: dict) -> pd.DataFrame:
    points = update.om_fetch(start, end)
    daily = dp.calibrated_daily_means(points, cal)
    daily["date"] = pd.to_datetime(daily["date"]).dt.strftime("%Y-%m-%d")
    for col in update.VARS:
        daily[col] = daily[col].round(2)
    return daily


def main() -> int:
    repo = Path(os.environ.get("GITHUB_WORKSPACE", HERE.parents[1]))
    published = pd.read_csv(repo / "nordic-climate" / "daily_grid_mean.csv")
    published["date"] = published["date"].astype(str).str[:10]
    out = Path(os.environ.get("NORDIC_CLIMATE_LIVE_REPORT", HERE / "live_overlap_report.json"))
    cache = Path(tempfile.mkdtemp(prefix="nordic-live-"))
    update.CACHE = cache
    today = date.fromisoformat(os.environ["NORDIC_CLIMATE_TODAY"]) if os.environ.get("NORDIC_CLIMATE_TODAY") else date.today()
    probe = today - timedelta(days=freshness.MAX_ERA5_LAG_DAYS)
    report: dict = {
        "today": today.isoformat(),
        "freshness_probe_day": probe.isoformat(),
        "max_lag_days": freshness.MAX_ERA5_LAG_DAYS,
        "lag_source": freshness.LAG_SOURCE,
        "overlap": [PUBLISHED_OVERLAP[0].isoformat(), PUBLISHED_OVERLAP[1].isoformat()],
    }
    try:
        cal = _cal()
        rebuilt = _means(PUBLISHED_OVERLAP[0], PUBLISHED_OVERLAP[1], cal)
        window = published[
            (published["date"] >= PUBLISHED_OVERLAP[0].isoformat())
            & (published["date"] <= PUBLISHED_OVERLAP[1].isoformat())
        ]
        merged = window.merge(rebuilt, on="date", how="left", suffixes=("_pub", "_new"))
        mismatches = []
        for _, row in merged.iterrows():
            for col in ("wind100", "temp", "rain"):
                pub, new = row[f"{col}_pub"], row[f"{col}_new"]
                if pd.isna(pub) and pd.isna(new):
                    continue
                if pd.isna(pub) or pd.isna(new) or round(float(pub), 2) != round(float(new), 2):
                    mismatches.append(
                        {
                            "date": row["date"],
                            "column": col,
                            "published": None if pd.isna(pub) else round(float(pub), 2),
                            "rebuilt": None if pd.isna(new) else round(float(new), 2),
                        }
                    )
        report["overlap_days"] = int(len(window))
        report["rebuilt_days"] = int(len(rebuilt))
        report["n_mismatches"] = len(mismatches)
        report["mismatches_head"] = mismatches[:30]
        probe_frame = _means(probe, probe, cal)
        report["probe_present"] = bool((probe_frame["date"] == probe.isoformat()).any())
        report["ok"] = report["n_mismatches"] == 0 and report["probe_present"] and report["rebuilt_days"] == report["overlap_days"]
        out.write_text(json.dumps(report, indent=2))
        print(json.dumps({k: report[k] for k in ("ok", "overlap_days", "n_mismatches", "probe_present", "freshness_probe_day")}, indent=2))
        if not report["ok"]:
            _summary(
                "### Nordic climate live overlap failed\n\n"
                f"Mismatches: {report['n_mismatches']}. "
                f"Freshness probe {probe.isoformat()} present: {report['probe_present']}.\n"
            )
            return 1
        _summary(
            "### Nordic climate live overlap\n\n"
            f"Matched {report['overlap_days']} published days "
            f"{PUBLISHED_OVERLAP[0].isoformat()} to {PUBLISHED_OVERLAP[1].isoformat()} "
            f"at 2 decimals. Freshness probe {probe.isoformat()} is present. "
            "Publish disabled.\n"
        )
        return 0
    except Exception as exc:
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        out.write_text(json.dumps(report, indent=2))
        _summary(
            "### Nordic climate live overlap failed\n\n"
            f"`{type(exc).__name__}: {exc}`\n\nNothing was published.\n"
        )
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

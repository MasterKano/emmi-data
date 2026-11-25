#!/usr/bin/env python3
"""Commit the five compact Nordic climate files as github-actions[bot].

Does nothing unless repository variable NORDIC_CLIMATE_PUBLISH=1 is exported
into the environment. Refuses to commit when freshness, 1940 coverage, or
1991–2020 coverage fails. When the five files are unchanged, exits 0 without
creating a commit. Never stages the Open-Meteo cache.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd

import freshness

PUBLISH_NAMES = [
    "monthly.csv",
    "daily_grid_mean.csv",
    "daily_normals.csv",
    "mtd.json",
    "README.md",
]
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898333+github-actions[bot]@users.noreply.github.com"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _fail(message: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(f"### Nordic climate publish refused\n\n{message}\n")
    print(message, file=sys.stderr)
    raise SystemExit(message)


def validate_built(built: Path, today: date) -> pd.DataFrame:
    daily_path = built / "daily_grid_mean.csv"
    if not daily_path.exists():
        _fail(f"missing {daily_path}")
    daily = pd.read_csv(daily_path)
    if daily.empty:
        _fail("daily_grid_mean.csv is empty")
    freshness.assert_contiguous(daily["date"])
    freshness.assert_climatology_present(daily["date"])
    end = pd.to_datetime(daily["date"]).max().date()
    freshness.assert_series_fresh(end, today)
    normals = built / "daily_normals.csv"
    if not normals.exists():
        _fail("daily_normals.csv is missing; refusing to publish")
    freshness.assert_normals_file(normals)
    for name in PUBLISH_NAMES:
        if not (built / name).exists():
            _fail(f"missing publish file {name}")
    # 1940-01-01 rain stays blank.
    first = daily.loc[daily["date"].astype(str).str[:10].eq("1940-01-01")]
    if first.empty or not pd.isna(first["rain"].iloc[0]):
        _fail("1940-01-01 rain must stay blank")
    return daily


def publish(repo: Path, built: Path, today: date | None = None) -> str | None:
    if os.environ.get("NORDIC_CLIMATE_PUBLISH") != "1":
        _fail(
            "NORDIC_CLIMATE_PUBLISH is not 1. Publishing stays disabled until "
            "cutover sets that repository variable. No commit was created."
        )
    today = today or date.today()
    daily = validate_built(built, today)
    dest = repo / "nordic-climate"
    dest.mkdir(parents=True, exist_ok=True)
    for name in PUBLISH_NAMES:
        target = dest / name
        target.write_bytes((built / name).read_bytes())
    _git(repo, "config", "user.name", BOT_NAME)
    _git(repo, "config", "user.email", BOT_EMAIL)
    _git(repo, "add", "--", *[f"nordic-climate/{name}" for name in PUBLISH_NAMES])
    status = _git(repo, "status", "--porcelain", "--", "nordic-climate")
    if not status.stdout.strip():
        print("no changes to publish")
        return None
    end = pd.to_datetime(daily["date"]).max().date().isoformat()
    start = pd.to_datetime(daily["date"]).min().date().isoformat()
    sizes = ", ".join(
        f"{name} {((dest / name).stat().st_size / 1024):.1f} KB" for name in PUBLISH_NAMES
    )
    mtd = json.loads((dest / "mtd.json").read_text())
    cov = mtd.get("daily_coverage") or {"start": start, "end": end, "n_days": int(len(daily))}
    body = "\n".join(
        [
            f"Coverage: {cov.get('start')} to {cov.get('end')} ({cov.get('n_days')} days).",
            f"Files: {sizes}.",
            "Raw per-point Open-Meteo cache is not committed.",
        ]
    )
    _git(repo, "commit", "-m", "Update Nordic climate daily files", "-m", body)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    print(f"commit={sha}")
    return sha


def main() -> int:
    repo = Path(os.environ.get("GITHUB_WORKSPACE", Path(__file__).resolve().parents[1]))
    built = Path(os.environ.get("NORDIC_CLIMATE_HOME", Path(__file__).resolve().parent))
    publish(repo, built)
    return 0


if __name__ == "__main__":
    sys.exit(main())

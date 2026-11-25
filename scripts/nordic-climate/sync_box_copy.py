#!/usr/bin/env python3
"""Refresh the box copy the daily IFS top-up reads.

Not deployed and not run against the box by this repository. After Jerry
retires Mark's routine, something on the box has to copy the published
compact files back to ``/home/box/data/nordic-climate/``, because
``nordic_topup.py`` and ``nordic_windspeed.py`` read those paths and do not
read GitHub.

Sources are public raw URLs. This script does not push, does not take a
token, and does not call Open-Meteo. ``--dry-run`` prints the plan and
writes nothing.

    python sync_box_copy.py --dest /home/box/data/nordic-climate
    python sync_box_copy.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

RAW = "https://raw.githubusercontent.com/MasterKano/emmi-data/main/"
# The five published files, plus the calibration the top-up applies to rain.
# calibration.json is vendored next to the scripts; it is not one of the five
# published products. It is public only after this branch is on main.
FILES = {
    "nordic-climate/daily_grid_mean.csv": "daily_grid_mean.csv",
    "nordic-climate/daily_normals.csv": "daily_normals.csv",
    "nordic-climate/monthly.csv": "monthly.csv",
    "nordic-climate/mtd.json": "mtd.json",
    "nordic-climate/README.md": "README.md",
    "scripts/nordic-climate/calibration.json": "calibration.json",
}


def urls() -> list[tuple[str, str]]:
    return [(RAW + src, dest) for src, dest in FILES.items()]


def sync(dest: Path, dry_run: bool) -> int:
    dest.mkdir(parents=True, exist_ok=True) if not dry_run else None
    for url, name in urls():
        target = dest / name
        if dry_run:
            print(f"would write {target} from {url}")
            continue
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(response.content)
        tmp.replace(target)
        print(f"wrote {target} ({len(response.content)} bytes)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=Path("/home/box/data/nordic-climate"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return sync(args.dest, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

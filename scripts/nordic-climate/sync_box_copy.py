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

RAW_ROOT = "https://raw.githubusercontent.com/MasterKano/emmi-data"
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


def urls(ref: str = "main") -> list[tuple[str, str]]:
    base = f"{RAW_ROOT}/{ref}/"
    return [(base + src, dest) for src, dest in FILES.items()]


def sync(dest: Path, dry_run: bool, ref: str = "main") -> int:
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    for url, name in urls(ref):
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
    parser.add_argument("--ref", default="main", help="git ref for the raw URLs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-box",
        action="store_true",
        help="required before writing under /home/box; not used by tests",
    )
    args = parser.parse_args(argv)
    resolved = args.dest.expanduser()
    if not args.dry_run and str(resolved).startswith("/home/box") and not args.allow_box:
        raise SystemExit("Refusing to write under /home/box without --allow-box. Not deployed.")
    return sync(args.dest, args.dry_run, args.ref)


if __name__ == "__main__":
    sys.exit(main())

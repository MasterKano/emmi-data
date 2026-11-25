#!/usr/bin/env python3
"""Decide whether a scheduled Nordic climate run should publish.

GitHub cron is UTC and can start late. The intended clock is 05:05
Europe/Zurich on Saturday and on the 11th (the morning after mr-climate).
Two UTC hours cover CET and CEST. They are one cron each, with day-of-month
and day-of-week both set, which GitHub treats as OR: one firing on a
Saturday that is also the 11th, not two.

A runner that starts after 05:05 still publishes, as long as it is the same
Zurich date and that date has not already been published. A run before
04:50 Zurich is the off-season hour arriving early, and is skipped.
``workflow_dispatch`` is not tied to the clock. Skipping is exit 0 so it
does not send a failure notification.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ZURICH = ZoneInfo("Europe/Zurich")
# Not before the 05:05 slot. There is no end: a delayed runner later that
# Zurich day is still the scheduled run.
WINDOW_START = time(4, 50)


def as_zurich(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZURICH)


def is_scheduled_day(local: datetime) -> bool:
    """Saturday, or the 11th. A Saturday that is the 11th is still one day."""
    return local.weekday() == 5 or local.day == 11


def decision(moment: datetime, event: str, last_published: date | None = None) -> tuple[bool, str]:
    if event != "schedule":
        return True, "manual run is not tied to the clock"
    local = as_zurich(moment)
    if not is_scheduled_day(local):
        return False, f"{local.date().isoformat()} is not a Saturday or the 11th in Zurich"
    if last_published == local.date():
        return False, f"already published on {local.date().isoformat()} Zurich; skipping the duplicate"
    if local.time() < WINDOW_START:
        return False, (
            f"{local.isoformat()} is before 04:50 Zurich; "
            "this is the early off-season hour, not a delayed 05:05 run"
        )
    return True, (
        f"{local.isoformat()} is a scheduled Zurich day at or after 04:50 "
        "and that date is not yet published"
    )


def allows(moment: datetime, event: str, last_published: date | None = None) -> bool:
    return decision(moment, event, last_published)[0]


def last_published_date(path: Path | None) -> date | None:
    """The ``generated`` date in the published mtd.json, if it is there."""
    if path is None or not path.exists():
        return None
    raw = json.loads(path.read_text()).get("generated")
    if not raw:
        return None
    return date.fromisoformat(str(raw)[:10])


def main(argv: list[str] | None = None) -> int:
    event = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    if argv and len(argv) > 1:
        event = argv[1]
    raw_now = os.environ.get("NORDIC_CLIMATE_NOW")
    if raw_now:
        moment = datetime.fromisoformat(raw_now)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = datetime.now(timezone.utc)
    mtd = os.environ.get("NORDIC_CLIMATE_PUBLISHED_MTD")
    published = last_published_date(Path(mtd) if mtd else None)
    ok, reason = decision(moment, event, published)
    print("proceed=yes" if ok else "proceed=no")
    print(reason, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

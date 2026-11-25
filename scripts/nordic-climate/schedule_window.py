#!/usr/bin/env python3
"""Decide whether a scheduled Nordic climate run is the 05:05 Europe/Zurich slot.

GitHub cron is UTC and cannot express CET/CEST. The publish workflow therefore
lists both UTC hours once it is enabled:

- 03:05 UTC — 05:05 CEST (last Sunday of March through last Sunday of October)
- 04:05 UTC — 05:05 CET (the rest of the year)

Saturday 05:05 and the 11th 05:05 each need both hours. The hour that is not
05:05 in Zurich is a no-op (exit 0, ``proceed=no``) so it does not publish and
does not raise a failure notification. 05:05 is outside the DST fallback
(02:00–03:00) and spring-forward (02:00) windows, so the local time exists
once.

``workflow_dispatch`` is not tied to the clock. The mr-climate workbook
refresh is a different system and runs at 06:00 UTC on the 10th; this window
is the following morning, the 11th.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

ZURICH = ZoneInfo("Europe/Zurich")
WINDOW_START = time(4, 50)
WINDOW_END = time(5, 20)


def in_zurich_window(moment: datetime) -> bool:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(ZURICH)
    return WINDOW_START <= local.time() <= WINDOW_END


def is_scheduled_day(moment: datetime) -> bool:
    """Saturday, or the 11th (the morning after mr-climate)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(ZURICH)
    return local.weekday() == 5 or local.day == 11


def allows(moment: datetime, event: str) -> bool:
    if event != "schedule":
        return True
    return in_zurich_window(moment) and is_scheduled_day(moment)


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
    ok = allows(moment, event)
    print("proceed=yes" if ok else "proceed=no")
    if not ok:
        local = moment.astimezone(ZURICH)
        print(
            f"Outside the 05:05 Europe/Zurich window "
            f"(local {local.isoformat()}); not publishing.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

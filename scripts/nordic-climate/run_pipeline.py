#!/usr/bin/env python3
"""Rebuild the five compact files. Never publishes.

Publishing is ``publish_outputs.py``, and only when NORDIC_CLIMATE_PUBLISH=1.
``--restore-seed`` (or NORDIC_CLIMATE_RESTORE_SEED=1) downloads the private
monthly-CSV seed when the Actions cache missed.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

os.environ.setdefault("NORDIC_CLIMATE_CACHE", str(HERE / "cache" / "openmeteo100m"))


def _summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


def _record(payload: dict) -> None:
    dest = Path(os.environ.get("NORDIC_CLIMATE_RUN_RECORD", HERE / "run_record.json"))
    dest.write_text(json.dumps(payload, indent=2))


def main() -> int:
    from locking import RunLock

    lock_path = Path(os.environ.get("NORDIC_CLIMATE_LOCK", HERE / ".run.lock"))
    lock = RunLock(lock_path)
    try:
        lock.acquire()
        cache = Path(os.environ["NORDIC_CLIMATE_CACHE"])
        cache.mkdir(parents=True, exist_ok=True)
        if "--restore-seed" in sys.argv or os.environ.get("NORDIC_CLIMATE_RESTORE_SEED") == "1":
            import restore_seed

            info = restore_seed.restore(cache)
            print(
                f"seed restored {info['n_monthly_csvs']} monthly CSVs "
                f"sha256={info['sha256']}"
            )
        import update

        update.CACHE = cache
        # The legacy publisher pushes as Kano Jigoro. The Action never calls it.
        update.main(publish=False)
        mtd_path = HERE / "mtd.json"
        mtd = json.loads(mtd_path.read_text()) if mtd_path.exists() else {}
        cov = mtd.get("daily_coverage") or {}
        record = {
            "finished_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "publish_enabled": False,
            "cache": str(cache),
            "coverage": cov,
            "generated": mtd.get("generated"),
            "today": os.environ.get("NORDIC_CLIMATE_TODAY") or "runner-clock",
        }
        _record(record)
        _summary(
            "### Nordic climate dry run\n\n"
            f"Publish disabled. Coverage {cov.get('start')} to {cov.get('end')} "
            f"({cov.get('n_days')} days).\n"
        )
        print("nordic-climate rebuild finished; publish disabled")
        return 0
    except Exception as exc:
        _summary(
            "### Nordic climate run failed\n\n"
            f"`{type(exc).__name__}: {exc}`\n\n"
            "The job exits non-zero so GitHub Actions failure notifications fire. "
            "Nothing was published.\n"
        )
        traceback.print_exc()
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())

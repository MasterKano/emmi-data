"""Small helpers shared by the build scripts (no secrets handled here).
- age_days(path): age of a data file from its own generated_utc stamp. File mtimes are useless in CI (a fresh
  checkout stamps every file 'now'), so skip-if-fresh checks must read the stamp inside the file.
- is_fresh(path, days): True when the file exists and its stamp is younger than `days`; '--force' on the command line
  always returns False.
- write_json(path, obj): compact JSON, written atomically (temp file + rename) so a crash never leaves half a file.
- openmeteo(url, pts, **params): one batched Open-Meteo request for many (lat, lon) points, 429-aware retries
  (free tier, non-commercial use, attribution CC BY 4.0). Returns one result dict per point."""
import datetime as dt, json, os, sys, tempfile, time

import requests


def _stamp(d):
    for k in ("generated_utc", "generated", "asof"):
        v = d.get(k) if isinstance(d, dict) else None
        if isinstance(v, str) and len(v) >= 10:
            v = v.replace("Z", "+00:00")
            try:
                t = dt.datetime.fromisoformat(v)
            except ValueError:
                continue
            return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    return None


def age_days(path):
    try:
        t = _stamp(json.load(open(path)))
    except Exception:  # noqa: BLE001
        return None
    if t is None:
        return None
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 86400


def is_fresh(path, days):
    if "--force" in sys.argv:
        return False
    a = age_days(path)
    return a is not None and a < days


def write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def openmeteo(url, pts, tries=4, wait=65, timeout=120, **params):
    params = dict(params, latitude=",".join(f"{a:.2f}" for a, _ in pts),
                  longitude=",".join(f"{b:.2f}" for _, b in pts))
    params.setdefault("timezone", "GMT")
    for i in range(tries):
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 429:
            time.sleep(wait * (i + 1)); continue
        r.raise_for_status()
        j = r.json()
        return j if isinstance(j, list) else [j]
    raise RuntimeError("Open-Meteo rate limit")

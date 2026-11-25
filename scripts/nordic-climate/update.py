#!/usr/bin/env python3
"""Nordic ERA5 daily history from 1940, raw 100m wind, calibrated temperature/rain.

One monthly entry point updates history, normals and MTD consistently. Raw
hourly cache stays off the public repo; --no-publish prevents compact-file
publication. See README.md for aggregation, height, calibration and archival
conventions.

Ported from MasterKano/newsletter-archive production/nordic-climate/update.py
at handover edc94177. Aggregation, grid, calibration and published columns are
unchanged. Operational deltas, all inert unless the named env var is set:

- CACHE reads NORDIC_CLIMATE_CACHE (default remains the box path).
- pipeline_today() reads NORDIC_CLIMATE_TODAY (fixture clock).
- sync_repo skips HTTP when NORDIC_CLIMATE_SKIP_SYNC=1.
- After the existing 1940 gap check, freshness.assert_series_fresh refuses a
  stale end date. A gap-free history is not enough.
- publish_emmi_data refuses unless NORDIC_CLIMATE_ALLOW_LEGACY_PUBLISH=1.
  The Action publishes with publish_outputs.py as github-actions[bot].
"""
import calendar, io, json, os, sys, time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BASE = Path(__file__).resolve().parent
REPO = BASE / "repo_data"
CACHE = Path(os.getenv("NORDIC_CLIMATE_CACHE", "/home/box/data/nordic-climate/cache/openmeteo100m"))
RAW = BASE / "raw"
RAW_URL = "https://raw.githubusercontent.com/MasterKano/mr-climate/main/"
AREA = [71, 4, 55, 32]  # N, W, S, E (repo constant)
LATS = list(range(55, 72, 2))
LONS = list(range(4, 33, 4))
CAL_START, CAL_END = (2024, 1), (2026, 9)
VARS = {"wind100": "wind_speed_100m", "temp": "temperature_2m_mean", "rain": "precipitation_sum"}
OM_URL = "https://archive-api.open-meteo.com/v1/archive"


def pipeline_today():
    """Runner clock. NORDIC_CLIMATE_TODAY=YYYY-MM-DD is only for fixtures."""
    raw = os.getenv("NORDIC_CLIMATE_TODAY")
    if not raw:
        return date.today()
    return date.fromisoformat(raw)


def complete_point_rows(df):
    """Only ERA5's unavailable first-day rain is allowed missing; never impute it."""
    valid = df[list(VARS)].notna()
    valid.loc[df["date"].astype(str).str[:10].eq("1940-01-01"), "rain"] = True
    return valid.all(axis=1)


def log(*a):
    print(*a, flush=True)


def atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def months(a, b):
    y, m = a
    while (y, m) <= b:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# ---------- 1. history ----------
def sync_repo():
    REPO.mkdir(parents=True, exist_ok=True)
    if os.getenv("NORDIC_CLIMATE_SKIP_SYNC") == "1":
        log("sync skipped (NORDIC_CLIMATE_SKIP_SYNC=1); using local repo_data")
    else:
        for name in ["data/weather_history.csv", "data/weather_excel_feed.csv"]:
            try:
                r = requests.get(RAW_URL + name, timeout=60)
                r.raise_for_status()
                pd.read_csv(io.StringIO(r.text))  # sanity parse
                atomic_write(REPO / Path(name).name, r.text)
                log(f"synced {name}")
            except Exception as e:
                log(f"WARN could not sync {name}: {type(e).__name__}; using local copy")
    h = pd.read_csv(REPO / "weather_history.csv")
    w = h.pivot_table(index=["year", "month"], columns="variable", values="value", aggfunc="last")
    src = h.groupby(["year", "month"])["source_workbook"].last()
    w["source"] = src.map(lambda s: "repo:workbook(KNMI/ERA5)" if str(s).endswith(".xlsx") else "repo:era5_cds")
    return w.dropna(subset=["wind", "temp", "rain"])


# ---------- 2. exact repo method via CDS ----------
def cds_available():
    return bool(os.getenv("CDSAPI_URL") and os.getenv("CDSAPI_KEY")) or Path.home().joinpath(".cdsapirc").exists()


def cds_month(y, m):
    import cdsapi, xarray as xr
    RAW.mkdir(parents=True, exist_ok=True)
    out = RAW / f"era5_{y}_{m:02d}.nc"
    if not out.exists():
        nd = calendar.monthrange(y, m)[1]
        req = {"product_type": ["reanalysis"],
               "variable": sorted(["10m_u_component_of_wind", "10m_v_component_of_wind", "total_precipitation", "2m_temperature"]),
               "year": [str(y)], "month": [f"{m:02d}"], "day": [f"{d:02d}" for d in range(1, nd + 1)],
               "time": [f"{h:02d}:00" for h in range(24)], "data_format": "netcdf",
               "download_format": "unarchived", "area": AREA}
        kw = {}
        if os.getenv("CDSAPI_URL") and os.getenv("CDSAPI_KEY"):
            kw = {"url": os.environ["CDSAPI_URL"], "key": os.environ["CDSAPI_KEY"]}
        cdsapi.Client(quiet=True, **kw).retrieve("reanalysis-era5-single-levels", req, str(out))
    # same processing as scripts/process_era5_file.py
    sys.path.insert(0, str(REPO / "scripts"))
    from process_era5_file import process_file
    df = process_file(out, y, m)
    return {r.variable: float(r.value) for r in df.itertuples()}


# ---------- 3. Open-Meteo ----------
# Last Open-Meteo rate-limit snapshot (cost is weighted; window is 60s, limit 600).
_RL = {"remaining": None, "reset": None, "cost": None}


def _remember_rl(resp):
    try:
        if "X-RateLimit-Remaining" in resp.headers:
            _RL["remaining"] = float(resp.headers["X-RateLimit-Remaining"])
        if "X-RateLimit-Reset" in resp.headers:
            _RL["reset"] = float(resp.headers["X-RateLimit-Reset"])
        if "X-RateLimit-Cost" in resp.headers:
            _RL["cost"] = float(resp.headers["X-RateLimit-Cost"])
    except (TypeError, ValueError):
        pass


def _wait_for_budget(need):
    rem, reset = _RL["remaining"], _RL["reset"]
    if rem is None or reset is None:
        return
    if rem >= need:
        return
    wait = max(0.0, reset - time.time()) + 1.0
    log(f"  Open-Meteo rate limit, waiting {wait:.0f}s (remaining {rem:.0f}, need {need:.0f})")
    time.sleep(wait)
    _RL["remaining"] = None


def om_fetch(start, end):
    """ERA5 100m hourly speed -> complete UTC daily means; all 72 requested points.

    Raw responses are gzipped beside the cache, never published. Coordinate batches
    are small enough for annual hourly downloads and resumable independently.
    """
    import gzip
    pts = [(la, lo) for la in LATS for lo in LONS]
    frames = []
    rawdir = CACHE / "hourly_raw"
    rawdir.mkdir(parents=True, exist_ok=True)
    for batch in range(0, len(pts), 12):
        sub = pts[batch:batch + 12]
        cache = rawdir / f"era5_{start}_{end}_b{batch:02d}.json.gz"
        if cache.exists():
            with gzip.open(cache, "rt") as f:
                locations = json.load(f)
        else:
            params = {"latitude": ",".join(str(x[0]) for x in sub),
                      "longitude": ",".join(str(x[1]) for x in sub),
                      "start_date": start.isoformat(), "end_date": end.isoformat(),
                      "hourly": "wind_speed_100m", "daily": "temperature_2m_mean,precipitation_sum",
                      "wind_speed_unit": "ms", "models": "era5", "timezone": "GMT"}
            est = max(len(sub), len(sub) * ((end-start).days+1) / 14 * 0.3)
            for attempt in range(12):
                _wait_for_budget(est + 5)
                try:
                    r = requests.get(OM_URL, params=params, timeout=240)
                except (requests.Timeout, requests.ConnectionError) as exc:
                    # Actions runners share egress. A dropped handshake is retried;
                    # the response itself is still validated below.
                    log(f"Open-Meteo connection error ({type(exc).__name__}); retry {attempt + 1}")
                    time.sleep(min(60, 5 * (attempt + 1)))
                    continue
                _remember_rl(r)
                if r.status_code == 429:
                    wait = max(65 * (attempt+1), float(r.headers.get("Retry-After", 0)))
                    log(f"Open-Meteo 429; backoff {wait}s")
                    time.sleep(wait)
                    continue
                if r.status_code >= 500:
                    time.sleep(min(120, 5 * 2**attempt))
                    continue
                r.raise_for_status()
                try:
                    locations = r.json()
                except requests.exceptions.JSONDecodeError:
                    body = r.text[:160]
                    log(f"Archive returned non-JSON (status {r.status_code}, content-type {r.headers.get('Content-Type')!r}): {body!r}")
                    if any(word in body.lower() for word in ("blocked", "rejected", "access denied", "captcha", "forbidden")):
                        raise RuntimeError("Archive request blocked; stop rather than bypass")
                    if attempt >= 2:
                        raise
                    time.sleep(5 * (attempt + 1))
                    continue
                break
            else:
                raise RuntimeError("Open-Meteo retries exhausted")
            if not isinstance(locations, list) or len(locations) != len(sub):
                raise RuntimeError("Open-Meteo coordinate batch mismatch")
            # Cache only responses whose hourly period is wholly complete.
            # Partial current-month responses are re-fetched on subsequent runs.
            complete = all(all(v is not None for v in loc["hourly"]["wind_speed_100m"]) for loc in locations)
            if complete:
                tmp = cache.with_suffix(".tmp")
                with gzip.open(tmp, "wt") as f:
                    json.dump(locations, f, separators=(",", ":"))
                tmp.replace(cache)
            log(f"100m {start}..{end}: points {batch+1}-{batch+len(sub)} fetched")
        for (lat, lon), loc in zip(sub, locations):
            h = loc["hourly"]
            hourly = pd.DataFrame({"time": pd.to_datetime(h["time"]), "wind100": h["wind_speed_100m"]})
            expected_hours = pd.date_range(start, end + timedelta(days=1), freq="h", inclusive="left")
            if not pd.DatetimeIndex(hourly["time"]).equals(expected_hours):
                raise RuntimeError("Missing or duplicate UTC hourly timestamps")
            hourly["date"] = hourly["time"].dt.strftime("%Y-%m-%d")
            grouped = hourly.groupby("date")["wind100"].agg(["mean", "count"])
            grouped.loc[grouped["count"] != 24, "mean"] = np.nan
            d = loc["daily"]
            frame = pd.DataFrame({"date": d["time"], "temp": d["temperature_2m_mean"], "rain": d["precipitation_sum"]})
            frame["wind100"] = frame["date"].map(grouped["mean"])
            frame["lat"], frame["lon"] = loc.get("latitude", lat), loc.get("longitude", lon)
            frames.append(frame)
    return pd.concat(frames, ignore_index=True)


_MEM = {}


def om_month(y, m, today):
    """Daily grid data for a month; complete months cached permanently."""
    if (y, m) in _MEM:
        return _MEM[(y, m)]
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"om_{y}_{m:02d}.csv"
    if f.exists():
        return pd.read_csv(f)
    start = date(y, m, 1)
    end = min(date(y, m, calendar.monthrange(y, m)[1]), today)
    log(f"  fetching Open-Meteo ERA5 {y}-{m:02d}")
    df = om_fetch(start, end)
    full = df.groupby("date")[list(VARS)].apply(lambda g: g.notna().all().all())
    if len(full) == calendar.monthrange(y, m)[1] and full.all():
        df.to_csv(f, index=False)
    _MEM[(y, m)] = df
    return df


def om_daily_means(df):
    """Area mean per day for both unweighted (repo style) and cos-lat weighted means; only complete days."""
    ok = df.groupby("date")[list(VARS)].transform(lambda s: s.notna().all()).all(axis=1)
    df = df[ok].copy()
    df["w"] = np.cos(np.deg2rad(df["lat"]))
    out = {}
    for scheme in ("unweighted", "coslat"):
        if scheme == "unweighted":
            g = df.groupby("date")[list(VARS)].mean()
        else:
            g = df.groupby("date").apply(lambda x: pd.Series({k: np.average(x[k], weights=x["w"]) for k in VARS}))
        out[scheme] = g.sort_index()
    return out


def om_monthly(y, m, today):
    d = om_daily_means(om_month(y, m, today))
    res = {}
    for scheme, g in d.items():
        res[scheme] = {"days": len(g), "first": g.index.min() if len(g) else None, "last": g.index.max() if len(g) else None,
                       **{k: float(g[k].mean()) if len(g) else None for k in VARS}}
    return res


def calibrate(hist, today):
    rows = []
    for y, m in months(CAL_START, CAL_END):
        if (y, m) not in hist.index:
            continue
        om = om_monthly(y, m, today)
        rows.append({"y": y, "m": m, **{f"h_{k}": hist.loc[(y, m), k] for k in ("temp", "rain")},
                     **{f"{s}_{k}": om[s][k] for s in om for k in ("temp", "rain")}})
    c = pd.DataFrame(rows)
    c.to_csv(BASE / "calibration_months.csv", index=False)
    cal = {"period": f"{CAL_START[0]}-{CAL_START[1]:02d}..{CAL_END[0]}-{CAL_END[1]:02d}", "n_months": len(c),
           "grid": f"lat {LATS[0]}-{LATS[-1]} step 2, lon {LONS[0]}-{LONS[-1]} step 4 ({len(LATS)*len(LONS)} pts), Open-Meteo models=era5 daily",
           "vars": {}}
    for k in ("temp", "rain"):
        best = None
        for s in ("unweighted", "coslat"):
            x, yv = c[f"{s}_{k}"].values, c[f"h_{k}"].values
            b, a = np.polyfit(x, yv, 1)
            resid = yv - (a + b * x)
            st = {"scheme": s, "raw_bias_om_minus_hist": float(np.mean(x - yv)), "raw_rmse": float(np.sqrt(np.mean((x - yv) ** 2))),
                  "corr": float(np.corrcoef(x, yv)[0, 1]), "slope": float(b), "intercept": float(a),
                  "calibrated_rmse": float(np.sqrt(np.mean(resid ** 2)))}
            if best is None or st["calibrated_rmse"] < best["calibrated_rmse"]:
                best = st
        cal["vars"][k] = best
    cal["vars"]["wind100"] = {"scheme": "coslat", "slope": 1.0, "intercept": 0.0, "note": "Raw 100m ERA5; no 10m calibration"}
    atomic_write(BASE / "calibration.json", json.dumps(cal, indent=2))
    return cal


def apply_cal(cal, k, v):
    if k == "wind100":
        return v
    p = cal["vars"][k]
    return p["intercept"] + p["slope"] * v


# ---------- 4. MTD ----------
def climo_stats(master, m, k, value):
    s = master[(master["month_num"] == m) & (master["year"].between(1970, 2025))][k]
    s91 = master[(master["month_num"] == m) & (master["year"].between(1991, 2020))][k]
    allv = pd.concat([s, pd.Series([value])])
    rank = int((allv > value).sum() + 1)
    mean = float(s.mean())
    return {"value": round(float(value), 3), "mean_1970_2025": round(mean, 3), "min_1970_2025": round(float(s.min()), 3),
            "min_year": int(master.loc[s.idxmin(), "year"]), "max_1970_2025": round(float(s.max()), 3),
            "max_year": int(master.loc[s.idxmax(), "year"]), "mean_1991_2020": round(float(s91.mean()), 3),
            "rank_high_to_low": f"{rank} of {len(allv)}", "anomaly_vs_1970_2025": round(float(value) - mean, 3),
            "pct_vs_1970_2025_mean": round(100 * (float(value) / mean - 1), 1), "n_years_climo": int(len(s))}


def main(publish=True):
    today = pipeline_today()
    backfill_daily_history()
    import daily_products as dp
    cal = json.loads((BASE / "calibration.json").read_text())
    cal["vars"]["wind100"] = {"scheme": "coslat", "slope": 1.0, "intercept": 0.0,
                                   "note": "Raw ERA5 100m wind, not calibrated to 10m history"}
    atomic_write(BASE / "calibration.json", json.dumps(cal, indent=2))
    extra = om_month(today.year, today.month, today)
    daily = dp.build_daily_grid_mean(cal, today, extra_point_df=extra)
    expected = pd.date_range("1940-01-01", daily["date"].max(), freq="D")
    if not pd.DatetimeIndex(daily["date"]).equals(expected):
        raise RuntimeError("Incomplete 1940-to-latest daily coverage; refusing product migration")
    from freshness import assert_series_fresh
    assert_series_fresh(daily["date"].max(), today)
    hist = sync_repo()
    master = hist.reset_index()
    legacy = pd.DataFrame({"month": master.apply(lambda r: f"{int(r.year)}-{int(r.month):02d}", axis=1),
                           "wind10_legacy": master["wind"], "temp": master["temp"], "rain": master["rain"],
                           "source": master["source"]})
    d = daily.copy()
    d["month"] = d["date"].dt.strftime("%Y-%m")
    complete = d.groupby("month")["date"].nunique()
    good = [m for m, n in complete.items() if n == calendar.monthrange(int(m[:4]), int(m[5:]))[1]]
    agg = d[d["month"].isin(good)].groupby("month")[list(VARS)].mean().reset_index()
    agg["rain_days"] = agg["month"].map(d.groupby("month")["rain"].count())
    # Preserve existing monthly temperature/rain; use calibrated daily monthly
    # means only where Jerry's historical workbook has no month.
    out = agg.merge(legacy, on="month", how="left", suffixes=("_daily", ""))
    for k in ("temp", "rain"):
        out[k] = out[k].fillna(out[k+"_daily"])
        out = out.drop(columns=k+"_daily")
    out["source"] = out["source"].fillna("calibrated:era5_daily")
    out["wind_source"] = "ERA5:100m_raw_72pt_coslat"
    atomic_write(BASE / "monthly.csv", out.round(6).to_csv(index=False))
    mc = out.copy()
    mc["year"] = mc["month"].str[:4].astype(int)
    mc["month_num"] = mc["month"].str[5:].astype(int)
    mtd = {"generated": str(today), "wind_height_m": 100,
           "basis": "ERA5 72-point grid: raw 100m wind; temperature/rain calibrated to workbook",
           "months": {}}
    for month, g in d.groupby("month"):
        # Only the latest two months belong in MTD; pre-1970 history is not MTD.
        if month not in sorted(d["month"].unique())[-2:]:
            continue
        entry = {"days_covered": len(g), "first_day": str(g["date"].min().date()),
                 "last_day": str(g["date"].max().date()),
                 "days_in_month": calendar.monthrange(int(month[:4]), int(month[5:]))[1],
                 "units": {"wind100": "m/s at 100m", "temp": "degC", "rain": "mm/d"}}
        for k in VARS:
            entry[k] = climo_stats(mc, int(month[5:]), k, float(g[k].mean()))
        mtd["months"][month] = entry
    dp.build_daily_normals(daily)
    dp.add_same_dates(mtd, daily)
    mtd["daily_coverage"] = {"start": "1940-01-01", "end": str(d["date"].max().date()),
                             "n_days": len(d), "expected_days": len(expected), "gaps": []}
    mtd["variable_coverage"] = {
        k: {"start": str(d.loc[d[k].notna(), "date"].min().date()),
            "end": str(d.loc[d[k].notna(), "date"].max().date()),
            "missing_dates": d.loc[d[k].isna(), "date"].dt.strftime("%Y-%m-%d").tolist()}
        for k in VARS}
    dp.sanity_check(daily)
    atomic_write(BASE / "mtd.json", json.dumps(mtd, indent=2))
    if publish:
        publish_emmi_data()
    log("100m daily products complete")


def next_m(ym):
    y, m = ym
    return (y + 1, 1) if m == 12 else (y, m + 1)


def backfill_daily_history(start_year=1940, end_year=None, pause=3.0):
    """Resume a polite Open-Meteo download into the monthly point cache.

    Default range is 1940 through the last completed month. Already-cached months are skipped.
    """
    import fetch_daily_history as fh
    today = pipeline_today()
    last = today.replace(day=1) - timedelta(days=1)
    if end_year is None:
        end_year = last.year
    missing = []
    for y, m in fh.missing_months(start_year, end_year):
        if date(y, m, calendar.monthrange(y, m)[1]) <= last:
            missing.append((y, m))
    total = len(list(months((start_year, 1), (last.year, last.month)))) if end_year >= last.year else (end_year - start_year + 1) * 12
    log(f"daily cache: {total - len(missing)}/{total} complete months present; {len(missing)} to fetch")
    if not missing:
        return
    by_year = {}
    for y, m in missing:
        by_year.setdefault(y, []).append(m)
    for y, months_ in by_year.items():
        fh.fill_year(y, months_, pause)
    still = [ym for ym in fh.missing_months(start_year, end_year)
             if date(ym[0], ym[1], calendar.monthrange(ym[0], ym[1])[1]) <= last]
    if still:
        raise RuntimeError(f"daily cache still missing {len(still)} months, first: {still[:12]}")
    log("daily cache complete")


PUBLISH_NAMES = ["monthly.csv", "daily_grid_mean.csv", "daily_normals.csv", "mtd.json", "README.md"]
EMMI_DATA_REPO = "MasterKano/emmi-data"
EMMI_DATA_DIR = Path(os.getenv("EMMI_DATA_DIR", "/workspace/emmi-data"))


def _git(repo, *args, check=True):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


def publish_emmi_data():
    """Legacy box publisher. Disabled in this port unless explicitly armed.

    The emmi-data Action uses publish_outputs.py and github-actions[bot].
    Setting NORDIC_CLIMATE_ALLOW_LEGACY_PUBLISH=1 restores the box behaviour
    (Kano Jigoro identity, push to origin/main). Do not set that on Actions.
    """
    if os.getenv("NORDIC_CLIMATE_ALLOW_LEGACY_PUBLISH") != "1":
        raise RuntimeError(
            "Legacy direct publish is disabled. Use publish_outputs.py, which "
            "commits as github-actions[bot] only when NORDIC_CLIMATE_PUBLISH=1."
        )
    import shutil, subprocess
    repo = EMMI_DATA_DIR
    if not (repo / ".git").exists():
        log(f"cloning {EMMI_DATA_REPO} into {repo}")
        subprocess.run(["gh", "repo", "clone", EMMI_DATA_REPO, str(repo)], check=True)
    _git(repo, "config", "user.name", "Kano Jigoro")
    _git(repo, "config", "user.email", "kimurame@duck.com")
    _git(repo, "fetch", "origin")
    listing = _git(repo, "ls-tree", "origin/main", "nordic-climate", check=False)
    first_time = listing.returncode != 0 or not listing.stdout.strip()

    if first_time:
        branch = "nordic-climate-daily-normals"
        _git(repo, "checkout", "main")
        _git(repo, "reset", "--hard", "origin/main")
        existing = _git(repo, "branch", "--list", branch)
        if existing.stdout.strip():
            _git(repo, "checkout", branch)
            _git(repo, "reset", "--hard", "origin/main")
        else:
            _git(repo, "checkout", "-B", branch, "origin/main")
    else:
        branch = "main"
        _git(repo, "checkout", "main")
        _git(repo, "pull", "--ff-only", "origin", "main")

    dest = repo / "nordic-climate"
    dest.mkdir(parents=True, exist_ok=True)
    for name in PUBLISH_NAMES:
        src = BASE / name
        if not src.exists():
            raise FileNotFoundError(f"missing publish file: {src}")
        shutil.copy2(src, dest / name)

    if first_time:
        root = repo / "README.md"
        text = root.read_text() if root.exists() else ""
        marker = "## Nordic climate"
        if marker not in text:
            blurb = (
                "\n" + marker + "\n\n"
                "Versioned Nordic climate history and 1991–2020 daily normals live in "
                "[`nordic-climate/`](nordic-climate/), on `main`. "
                "They are not published to the `live-data` branch. "
                "The Monthly Nordic climate update routine refreshes that folder.\n"
            )
            key = "## Keep this repo public"
            if key in text:
                text = text.replace(key, blurb + key, 1)
            else:
                text = text.rstrip() + "\n" + blurb
            root.write_text(text)
            _git(repo, "add", "README.md")

    _git(repo, "add", "nordic-climate")
    status = _git(repo, "status", "--porcelain", "nordic-climate")
    if not status.stdout.strip():
        log("emmi-data nordic-climate: no changes to publish")
        return None

    sizes = ", ".join(f"{name} {((repo/'nordic-climate'/name).stat().st_size/1024):.1f} KB" for name in PUBLISH_NAMES)
    mtd = json.loads((BASE / "mtd.json").read_text())
    cov = mtd.get("daily_coverage") or {}
    if not cov and (BASE / "daily_grid_mean.csv").exists():
        d = pd.read_csv(BASE / "daily_grid_mean.csv", usecols=["date"])
        cov = {"start": str(d["date"].iloc[0])[:10], "end": str(d["date"].iloc[-1])[:10], "n_days": int(len(d))}
    body = [
        f"Coverage: {cov.get('start')} to {cov.get('end')} ({cov.get('n_days')} days).",
        f"Files: {sizes}.",
        "Raw per-point Open-Meteo cache is not committed.",
    ]
    msg = "Add Nordic climate daily history and 1991-2020 normals" if first_time else "Update Nordic climate daily files"
    _git(repo, "commit", "-m", msg, "-m", "\n".join(body))
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if first_time:
        _git(repo, "push", "-u", "origin", branch)
        pr = subprocess.run(
            [
                "gh", "pr", "create",
                "--repo", EMMI_DATA_REPO,
                "--base", "main",
                "--head", branch,
                "--title", "Add Nordic climate daily history and 1991-2020 normals",
                "--body",
                "Compact, versioned Nordic climate files so weather panels can compare "
                "the current period with the same calendar dates in 1991–2020, not month on month.\n\n"
                + "\n".join(f"- {b}" for b in body)
                + "\n\n"
                "The raw 72-point Open-Meteo cache stays on the build machine. "
                "Later runs of the Monthly Nordic climate update routine will commit refreshes to main.",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        url = pr.stdout.strip()
        log(f"emmi-data PR: {url} ({sha[:8]})")
        return url
    _git(repo, "push", "origin", "main")
    log(f"emmi-data published {sha[:8]} to main ({sizes})")
    return sha


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Update Nordic climate monthly mirror, daily normals, and publish to emmi-data")
    p.add_argument("--backfill-daily", action="store_true",
                   help="(Deprecated alias) history backfill now runs automatically; this flag is ignored")
    p.add_argument("--no-publish", action="store_true", help="Rebuild local files without pushing to emmi-data")
    args = p.parse_args()
    main(publish=not args.no_publish)

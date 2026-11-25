#!/usr/bin/env python3
"""Build daily grid-means, 1991-2020 daily normals, same-dates MTD fields, and a sanity check.

Imported by update.py so the monthly routine keeps these fresh. Can also be run alone
once the Open-Meteo monthly cache is in place.

Ported from handover edc94177. Aggregation is unchanged. The cache path is update.CACHE.
"""
from __future__ import annotations

import calendar
import json
import sys
from datetime import date, timedelta
import time
from pathlib import Path

import numpy as np
import pandas as pd

if __name__ == "__main__" or "update" not in sys.modules:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import update as u
else:
    import update as u

CLIMO_START, CLIMO_END = date(1991, 1, 1), date(2020, 12, 31)
SEASON_START_MD = (10, 1)  # 1 Oct
SMOOTH_WINDOW = 7  # ±7 days
NPTS = len(u.LATS) * len(u.LONS)


def _month_path(y, m):
    return u.CACHE / f"om_{y}_{m:02d}.csv"


def month_cache_complete(y, m):
    f = _month_path(y, m)
    if not f.exists() or f.stat().st_size < 100:
        return False
    try:
        df = pd.read_csv(f, usecols=["date", "lat", "lon", *u.VARS])
    except Exception:
        return False
    nd = calendar.monthrange(y, m)[1]
    if df.groupby(["lat", "lon"]).ngroups != NPTS:
        return False
    if df["date"].nunique() != nd:
        return False
    if not u.complete_point_rows(df).all():
        return False
    return True


def coverage_report(start_year=1940, end_year=None, today=None):
    today = today or date.today()
    end_year = end_year or today.year
    missing, complete = [], []
    for y in range(start_year, end_year + 1):
        last_m = 12 if y < today.year else today.month
        for m in range(1, last_m + 1):
            if (y, m) == (today.year, today.month):
                continue
            key = f"{y}-{m:02d}"
            (complete if month_cache_complete(y, m) else missing).append(key)
    return {"complete": complete, "missing": missing}


def load_point_cache(start=date(1940, 1, 1), end=None):
    """Load all complete monthly cache files in [start, end]."""
    files = sorted(u.CACHE.glob("om_????_??.csv"))
    frames = []
    for f in files:
        try:
            y, m = int(f.stem[3:7]), int(f.stem[8:10])
        except ValueError:
            continue
        month_start = date(y, m, 1)
        month_end = date(y, m, calendar.monthrange(y, m)[1])
        if month_end < start:
            continue
        if end is not None and month_start > end:
            continue
        if not month_cache_complete(y, m):
            u.log(f"WARN skipping incomplete cache {f.name}")
            continue
        frames.append(pd.read_csv(f))
    if not frames:
        return pd.DataFrame(columns=["date", "lat", "lon", *u.VARS])
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df


def _grid_mean_one(df, scheme, cols):
    if scheme == "unweighted":
        return df.groupby("date")[cols].mean()
    df = df.copy()
    df["w"] = np.cos(np.deg2rad(df["lat"]))
    def wavg(x):
        return pd.Series({k: np.average(x[k], weights=x["w"]) for k in cols})
    try:
        return df.groupby("date").apply(wavg, include_groups=False)
    except TypeError:
        return df.groupby("date").apply(wavg)


def calibrated_daily_means(point_df, cal):
    """Area-mean each complete day, then apply the linear calibration. One row per date."""
    if point_df.empty:
        return pd.DataFrame(columns=["date", *u.VARS])
    df = point_df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    ok = u.complete_point_rows(df).groupby(df["date"]).transform("all")
    df = df[ok]
    df = df.drop_duplicates(["date", "lat", "lon"])
    counts = df.groupby("date").size()
    good_days = counts[counts == NPTS].index
    df = df[df["date"].isin(good_days)]
    schemes = {k: "coslat" if k == "wind100" else cal["vars"][k]["scheme"] for k in u.VARS}
    parts = {}
    for scheme in set(schemes.values()):
        cols = [k for k, s in schemes.items() if s == scheme]
        parts[scheme] = _grid_mean_one(df, scheme, cols)
    g = pd.concat(parts.values(), axis=1)
    out = pd.DataFrame({"date": pd.to_datetime(g.index)})
    for k in u.VARS:
        out[k] = [u.apply_cal(cal, k, v) for v in g[k].tolist()]
    out["rain"] = out["rain"].clip(lower=0)
    return out.sort_values("date").reset_index(drop=True)


def build_daily_grid_mean(cal, today, extra_point_df=None):
    """Write daily_grid_mean.csv from cache plus any extra (e.g. current incomplete month)."""
    point = load_point_cache(start=date(1940, 1, 1))
    if extra_point_df is not None and len(extra_point_df):
        extra = extra_point_df.copy()
        extra["date"] = pd.to_datetime(extra["date"]).dt.strftime("%Y-%m-%d")
        have = set(point["date"]) if len(point) else set()
        extra = extra[~extra["date"].isin(have)]
        if len(extra):
            point = pd.concat([point, extra], ignore_index=True) if len(point) else extra
    daily = calibrated_daily_means(point, cal)
    if daily.empty:
        raise RuntimeError("no complete daily grid means could be built from the Open-Meteo cache")
    out = daily.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    for k in u.VARS:
        out[k] = out[k].round(2)
    dest = u.BASE / "daily_grid_mean.csv"
    u.atomic_write(dest, out.to_csv(index=False))
    u.log(f"daily_grid_mean.csv: {len(out)} days, {out['date'].iloc[0]} .. {out['date'].iloc[-1]}")
    daily = out.copy()
    daily["date"] = pd.to_datetime(daily["date"])
    return daily


def _doy366(ts):
    """Leap-year day-of-year: Feb 29 = 60, Mar 1 = 61 always."""
    return date(2020, ts.month, ts.day).timetuple().tm_yday


def build_daily_normals(daily):
    """1991-2020 mean, p10, p90, min, max per calendar day; mean smoothed ±7 days."""
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    climo = d[(d["date"] >= pd.Timestamp(CLIMO_START)) & (d["date"] <= pd.Timestamp(CLIMO_END))].copy()
    if len(climo) != len(pd.date_range(CLIMO_START, CLIMO_END)) or climo["date"].duplicated().any():
        raise RuntimeError("1991-2020 must be complete and unique before normals can be rebuilt")
    climo["month"] = climo["date"].dt.month
    climo["day"] = climo["date"].dt.day
    climo["doy"] = climo["date"].map(lambda t: _doy366(t.date() if hasattr(t, "date") else t))
    rows = []
    for (month, day), g in climo.groupby(["month", "day"], sort=True):
        rec = {"month": int(month), "day": int(day),
               "doy": int(date(2020, month, day).timetuple().tm_yday),
               "n_years": int(g["date"].dt.year.nunique())}
        for k in u.VARS:
            v = g[k].astype(float)
            rec[f"{k}_mean"] = float(v.mean())
            rec[f"{k}_p10"] = float(np.percentile(v, 10))
            rec[f"{k}_p90"] = float(np.percentile(v, 90))
            rec[f"{k}_min"] = float(v.min())
            rec[f"{k}_max"] = float(v.max())
        rows.append(rec)
    norms = pd.DataFrame(rows).sort_values("doy").reset_index(drop=True)
    doy_to_idx = {int(r.doy): i for i, r in norms.iterrows()}
    doys = [int(x) for x in norms["doy"].tolist()]
    for k in u.VARS:
        raw = norms[f"{k}_mean"].to_numpy()
        smoothed = []
        for doy in doys:
            window = []
            for off in range(-SMOOTH_WINDOW, SMOOTH_WINDOW + 1):
                md = ((doy - 1 + off) % 366) + 1
                if md in doy_to_idx:
                    window.append(raw[doy_to_idx[md]])
            smoothed.append(float(np.mean(window)))
        norms[f"{k}_mean_smooth"] = smoothed
    cols = ["month", "day", "doy", "n_years"]
    for k in u.VARS:
        cols += [f"{k}_mean", f"{k}_mean_smooth", f"{k}_p10", f"{k}_p90", f"{k}_min", f"{k}_max"]
    norms = norms[cols]
    out = norms.copy()
    for c in out.columns:
        if c not in ("month", "day", "doy", "n_years"):
            out[c] = out[c].round(6)
    dest = u.BASE / "daily_normals.csv"
    u.atomic_write(dest, out.to_csv(index=False))
    feb29 = out[(out["month"] == 2) & (out["day"] == 29)]
    n29 = int(feb29["n_years"].iloc[0]) if len(feb29) else 0
    u.log(f"daily_normals.csv: {len(out)} calendar days (1991-2020), Feb 29 n_years={n29}, "
          f"mean smoothed ±{SMOOTH_WINDOW}d")
    return norms


def _shift_period(start, end, year_start):
    """Map [start, end] onto a climatology year, dropping 29 Feb in non-leap years."""
    days = []
    d = start
    while d <= end:
        try:
            days.append(date(year_start + (d.year - start.year), d.month, d.day))
        except ValueError:
            pass
        d += timedelta(days=1)
    return days


def _lookup(daily_idx, days, cols):
    vals = []
    missing = 0
    for d in days:
        if d in daily_idx.index:
            vals.append(daily_idx.loc[d, cols])
        else:
            missing += 1
    if not vals:
        return None, missing, len(days)
    frame = pd.DataFrame(vals)
    return frame, missing, len(days)


def _pct_rank(sample, value):
    s = pd.Series(sample).dropna()
    if len(s) == 0 or value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    return float((s <= value).mean() * 100.0)


def _round(x, n=3):
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return None
    return round(float(x), n)


def period_comparison(daily, start, end, how="mean"):
    """Compare a period with 1991-2020 same-dates and with last year."""
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.date
    idx = d.set_index("date")
    years = range(CLIMO_START.year, CLIMO_END.year + 1)
    yearly = {k: [] for k in u.VARS}
    for y in years:
        days = _shift_period(start, end, y)
        frame, missing, n = _lookup(idx, days, list(u.VARS))
        if frame is None or missing or len(frame) != n:
            continue
        agg = frame.sum() if how == "sum" else frame.mean()
        for k in u.VARS:
            yearly[k].append(float(agg[k]))
    cur_days = _shift_period(start, end, start.year)
    cur, cur_missing, cur_n = _lookup(idx, cur_days, list(u.VARS))
    current = None
    if cur is not None and not cur_missing:
        current = (cur.sum() if how == "sum" else cur.mean()).to_dict()
    ly_days = _shift_period(start, end, start.year - 1)
    ly, ly_missing, ly_n = _lookup(idx, ly_days, list(u.VARS))
    last_year = None
    if ly is not None and not ly_missing:
        last_year = (ly.sum() if how == "sum" else ly.mean()).to_dict()
    out = {"n_years": len(yearly["wind100"]), "current": current, "last_year": last_year,
           "current_days": cur_n - (cur_missing or 0), "last_year_complete": last_year is not None}
    for k in u.VARS:
        sample = yearly[k]
        val = None if current is None else current[k]
        out[k] = {
            "value": val,
            "normal_1991_2020": float(np.mean(sample)) if sample else None,
            "p10": float(np.percentile(sample, 10)) if sample else None,
            "p90": float(np.percentile(sample, 90)) if sample else None,
            "anomaly": (None if val is None or not sample else val - float(np.mean(sample))),
            "percentile": _pct_rank(sample, val),
            "same_dates_last_year": None if last_year is None else last_year[k],
            "n_years": len(sample),
        }
    return out


def season_start(last_day):
    m, d = SEASON_START_MD
    if (last_day.month, last_day.day) >= (m, d):
        return date(last_day.year, m, d)
    return date(last_day.year - 1, m, d)


def add_same_dates(mtd, daily):
    """Extend each mtd['months'] entry with a same-dates comparison; keep existing fields."""
    mtd["note"] = (
        "Partial months are compared with full-month climatology (legacy fields) and with the "
        "1991-2020 normal for the same calendar dates (same_dates). ERA5 on Open-Meteo lags ~5 days."
    )
    mtd.setdefault("same_dates_note",
                   "same_dates compares the month-to-date mean with the 1991-2020 mean of those exact "
                   "calendar dates (unsmoothed). daily_normals.csv also has a ±7-day smoothed seasonal "
                   "curve, which is for charts and is not used in these figures. Rain season_cumulative "
                   "is the sum since 1 Oct, in mm.")
    for key, entry in mtd.get("months", {}).items():
        first, last = entry.get("first_day"), entry.get("last_day")
        if not first or not last or not entry.get("days_covered"):
            continue
        start = date.fromisoformat(str(first)[:10])
        end = date.fromisoformat(str(last)[:10])
        cmp_mean = period_comparison(daily, start, end, how="mean")
        block = {
            "period": f"{start.isoformat()} to {end.isoformat()}",
            "climatology": "1991-2020, exact calendar dates, unsmoothed grid-mean: raw 100m wind, calibrated temperature/rain",
        }
        for k in u.VARS:
            s = cmp_mean[k]
            rec = {
                "value": _round(s["value"]),
                "normal_1991_2020": _round(s["normal_1991_2020"]),
                "anomaly": _round(s["anomaly"]),
                "percentile": _round(s["percentile"], 1),
                "same_dates_last_year": _round(s["same_dates_last_year"]),
                "n_years": s["n_years"],
            }
            if k == "rain":
                rec["unit"] = "mm/d"
            block[k] = rec
        ss = season_start(end)
        cmp_sum = period_comparison(daily, ss, end, how="sum")
        rs = cmp_sum["rain"]
        block["rain"]["season_cumulative"] = {
            "season_start": ss.isoformat(),
            "through": end.isoformat(),
            "unit": "mm",
            "value": _round(rs["value"], 2),
            "normal_1991_2020": _round(rs["normal_1991_2020"], 2),
            "anomaly": _round(rs["anomaly"], 2),
            "percentile": _round(rs["percentile"], 1),
            "last_year": _round(rs["same_dates_last_year"], 2),
            "n_years": rs["n_years"],
        }
        entry["same_dates"] = block

        def _fmt(v, signed=False):
            if v is None:
                return "NA"
            return f"{v:+}" if signed else str(v)

        u.log(
            f"same-dates {key} ({start} to {end}): " +
            "  ".join(
                f"{k}={_fmt(block[k]['value'])} (normal {_fmt(block[k]['normal_1991_2020'])}, "
                f"anom {_fmt(block[k]['anomaly'], True)}, pctl {_fmt(block[k]['percentile'])})"
                for k in u.VARS
            )
        )
        sc = block["rain"]["season_cumulative"]
        u.log(f"  season rain {sc['season_start']} to {sc['through']}: "
              f"{_fmt(sc['value'])} mm (normal {_fmt(sc['normal_1991_2020'])}, "
              f"anom {_fmt(sc['anomaly'], True)}, pctl {_fmt(sc['percentile'])})")
    return mtd


def sanity_check(daily, monthly_path=None):
    """Compare monthly means of the daily series with monthly.csv. Write a detailed CSV + JSON summary."""
    monthly_path = Path(monthly_path or u.BASE / "monthly.csv")
    monthly = pd.read_csv(monthly_path)
    monthly["month"] = monthly["month"].astype(str)
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    d["month"] = d["date"].dt.strftime("%Y-%m")
    days_in = d.groupby("month")["date"].nunique()
    def expected_days(m):
        y, mo = int(m[:4]), int(m[5:7])
        return calendar.monthrange(y, mo)[1]
    complete_months = [m for m, n in days_in.items() if n == expected_days(m)]
    agg = d[d["month"].isin(complete_months)].groupby("month")[list(u.VARS)].mean().reset_index()
    merged = agg.merge(monthly, on="month", suffixes=("_daily", "_monthly"))
    if merged.empty:
        u.log("WARN sanity check: no overlapping complete months")
        return None
    rows = []
    for _, r in merged.iterrows():
        rec = {"month": r["month"], "source": r.get("source", "")}
        for k in u.VARS:
            rec[f"{k}_daily"] = float(r[f"{k}_daily"])
            rec[f"{k}_monthly"] = float(r[f"{k}_monthly"])
            rec[f"{k}_diff"] = float(r[f"{k}_daily"] - r[f"{k}_monthly"])
        rows.append(rec)
    detail = pd.DataFrame(rows)
    dest = u.BASE / "daily_vs_monthly_sanity.csv"
    out = detail.copy()
    for c in out.columns:
        if c not in ("month", "source"):
            out[c] = out[c].round(6)
    u.atomic_write(dest, out.to_csv(index=False))

    def period_stats(sub, label):
        st = {"label": label, "n_months": int(len(sub))}
        if sub.empty:
            return st
        for k in u.VARS:
            diff = sub[f"{k}_diff"]
            st[k] = {
                "mean_bias_daily_minus_monthly": _round(float(diff.mean()), 4),
                "rmse": _round(float(np.sqrt((diff ** 2).mean())), 4),
                "mae": _round(float(diff.abs().mean()), 4),
                "max_abs": _round(float(diff.abs().max()), 4),
                "max_abs_month": str(sub.loc[diff.abs().idxmax(), "month"]) if len(sub) else None,
            }
        return st

    def sel(start, end):
        return detail[(detail["month"] >= start) & (detail["month"] <= end)]

    periods = [
        period_stats(sel("1991-01", "2020-12"), "1991-2020 climatology (out of sample vs monthly.csv)"),
        period_stats(sel("2021-01", "2023-12"), "2021-2023 (after climatology, before calibration window)"),
        period_stats(sel("2024-01", "2026-09"), "2024-01..2026-09 (calibration window)"),
        period_stats(detail, "all overlapping complete months"),
    ]
    summary = {
        "n_months_compared": int(len(detail)),
        "first": detail["month"].iloc[0] if len(detail) else None,
        "last": detail["month"].iloc[-1] if len(detail) else None,
        "periods": periods,
        "note": ("daily means are Open-Meteo ERA5, 72-point grid: raw 100m wind; temperature/rain calibrated to Jerry's history. "
                 "monthly.csv is that history (workbook / CDS). Differences in 2024-01..2026-09 should be "
                 "close to the calibrated RMSE in calibration.json; earlier years are a true historical check."),
    }
    u.atomic_write(u.BASE / "daily_vs_monthly_sanity.json", json.dumps(summary, indent=2))
    u.log(f"sanity check: {len(detail)} complete months {summary['first']} .. {summary['last']}")
    for p in periods:
        if p["n_months"] == 0:
            u.log(f"  {p['label']}: no months")
            continue
        bits = [f"{k} bias {p[k]['mean_bias_daily_minus_monthly']:+.3f} rmse {p[k]['rmse']:.3f}" for k in u.VARS]
        u.log(f"  {p['label']} (n={p['n_months']}): " + "; ".join(bits))
    return summary


def load_or_read_daily():
    p = u.BASE / "daily_grid_mean.csv"
    if not p.exists():
        return None
    d = pd.read_csv(p)
    d["date"] = pd.to_datetime(d["date"])
    return d


def refresh(cal, today, mtd=None, extra_point_df=None):
    """Rebuild daily products from the cache. Returns (daily, mtd, sanity)."""
    cov = coverage_report(1940, today.year, today)
    n_miss = len(cov["missing"])
    if n_miss:
        raise RuntimeError(f"Daily cache missing {n_miss} complete months since 1940 (first: {cov['missing'][:8]})")
    u.log(f"daily cache complete for all finished months 1940-{today.isoformat()}")
    climo_missing = [m for m in cov["missing"] if m <= "2020-12"]
    if climo_missing:
        u.log(f"WARN 1991-2020 cache has {len(climo_missing)} missing months; normals will use whatever is present")
    daily = build_daily_grid_mean(cal, today, extra_point_df=extra_point_df)
    build_daily_normals(daily)
    if mtd is not None:
        add_same_dates(mtd, daily)
        dts = pd.to_datetime(daily["date"])
        expected = pd.date_range(dts.min(), dts.max(), freq="D")
        gaps = expected.difference(pd.DatetimeIndex(dts))
        mtd["daily_coverage"] = {
            "start": dts.min().strftime("%Y-%m-%d"),
            "end": dts.max().strftime("%Y-%m-%d"),
            "n_days": int(len(daily)),
            "expected_days": int(len(expected)),
            "gaps": [x.strftime("%Y-%m-%d") for x in gaps],
        }
    summary = sanity_check(daily)
    return daily, mtd, summary


def main():
    today = u.pipeline_today()
    cal_path = u.BASE / "calibration.json"
    if not cal_path.exists():
        raise SystemExit("calibration.json is missing; run update.py first")
    cal = json.loads(cal_path.read_text())
    mtd_path = u.BASE / "mtd.json"
    mtd = json.loads(mtd_path.read_text()) if mtd_path.exists() else None
    extra = None
    try:
        extra = u.om_month(today.year, today.month, today)
    except Exception as e:
        u.log(f"WARN could not fetch current month points: {type(e).__name__}: {e}")
    daily, mtd, _ = refresh(cal, today, mtd=mtd, extra_point_df=extra)
    if mtd is not None:
        mtd["generated"] = today.isoformat()
        u.atomic_write(mtd_path, json.dumps(mtd, indent=2, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
        u.log("mtd.json updated with same_dates")
    u.log("daily products done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

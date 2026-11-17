"""Danish wind: Energinet first, ENTSO-E A75 as the fallback.

Shared by the Nordic v2 builder, the monthly overview and the month-to-date
builder so the three paths do not drift. Official settlement
(ProductionConsumptionSettlement, hourly, DK1+DK2 onshore and offshore) is
preferred; Energinet 5-minute actuals (ElectricityProdex5MinRealtime) fill a
day that is not yet settled, when at least DK_WIND_REALTIME_COVER of the
intervals are present; ENTSO-E A75 is the fallback. Installed-capacity
denominators are not set here.

Monthly TWh figures on EMMi are revised from DK_WIND_MONTHLY_FROM (August 2026)
onwards, which is when ENTSO-E A75 began to run systematically below settlement.
Earlier months stay on A75 until a separate historical revision.
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

import pandas as pd

import build_power as bp
import entsoe_client as ec

DK_WIND_REALTIME_COVER = 0.98
DK_WIND_SETTLE_COLS = [
    "HourUTC", "PriceArea",
    "OffshoreWindLt100MW_MWh", "OffshoreWindGe100MW_MWh",
    "OnshoreWindLt50kW_MWh", "OnshoreWindGe50kW_MWh",
]
DK_WIND_MWH = DK_WIND_SETTLE_COLS[2:]
DK_ZONES = ("DK1", "DK2")
# First month whose published EMMi monthly TWh uses Energinet rather than ENTSO-E A75.
DK_WIND_MONTHLY_FROM = "2026-08"
BOX_CACHE = Path("/home/box/data/nordic-v2/cache")


def default_cache_dir() -> Path:
    if BOX_CACHE.is_dir():
        return BOX_CACHE
    return Path(__file__).resolve().parent.parent / "nordic-v2" / "cache"


def default_today() -> dt.date:
    return dt.datetime.now(ec.CET).date()


def cet_hours(d: dt.date) -> float:
    lo, hi = ec._cet_bounds(d, d + dt.timedelta(days=1))
    return (hi - lo).total_seconds() / 3600.0


def daily_average_mw(s, step_seconds, min_cover=1.0):
    """Daily mean MW from a UTC time series, indexed by naive midnight of the CET delivery day.

    A day is kept only when the fraction of expected intervals present is >= min_cover.
    Missing intervals are omitted from the mean, not filled.
    """
    if s is None or len(s) == 0:
        return pd.Series(dtype=float)
    s = s.dropna().sort_index()
    if s.index.tz is None:
        s = s.tz_localize("UTC")
    else:
        s = s.tz_convert("UTC")
    out = {}
    for day, part in s.groupby(s.index.tz_convert(ec.CET).strftime("%Y-%m-%d")):
        d = dt.date.fromisoformat(day)
        lo, hi = ec._cet_bounds(d, d + dt.timedelta(days=1))
        need = int((hi - lo).total_seconds() / step_seconds)
        part = part.loc[(part.index >= lo) & (part.index < hi)]
        n = int(part.notna().sum())
        if need and n > 0 and n / need >= min_cover:
            out[pd.Timestamp(d)] = float(part.mean())
    return pd.Series(out, dtype=float)


def _naive_series(s):
    if s is None or len(s) == 0:
        return pd.Series(dtype=float)
    x = s.copy()
    x.index = pd.to_datetime(x.index).tz_localize(None)
    x = x[~x.index.duplicated(keep="last")].sort_index()
    return x


def merge_dk_wind(entsoe=None, settlement=None, realtime=None):
    """Per day: official settlement, else Energinet 5-minute actuals, else ENTSO-E A75.

    Returns (mw Series, source Series) on the union of dates, naive midnight index.
    """
    ev, sv, rv = _naive_series(entsoe), _naive_series(settlement), _naive_series(realtime)
    idx = ev.index.union(sv.index).union(rv.index).sort_values()
    if len(idx) == 0:
        return pd.Series(dtype=float), pd.Series(dtype=object)
    mw, src = [], []
    for ts in idx:
        if ts in sv.index and pd.notna(sv.loc[ts]):
            mw.append(float(sv.loc[ts])); src.append("energinet_settlement")
        elif ts in rv.index and pd.notna(rv.loc[ts]):
            mw.append(float(rv.loc[ts])); src.append("energinet_realtime")
        elif ts in ev.index and pd.notna(ev.loc[ts]):
            mw.append(float(ev.loc[ts])); src.append("entsoe_a75")
        else:
            mw.append(float("nan")); src.append(None)
    return pd.Series(mw, index=idx, dtype=float), pd.Series(src, index=idx, dtype=object)


def overlap_pct(a, b):
    """a versus b, percent difference 100*(a/b-1), only on dates both have values."""
    if a is None or b is None or len(a) == 0 or len(b) == 0:
        return {}
    aa, bb = a.copy(), b.copy()
    aa.index = pd.to_datetime(aa.index).tz_localize(None)
    bb.index = pd.to_datetime(bb.index).tz_localize(None)
    j = pd.concat({"a": aa, "b": bb}, axis=1, sort=True).dropna()
    j = j[(j.b != 0)]
    if j.empty:
        return {}
    pct = (j.a / j.b - 1) * 100
    return dict(
        n=int(len(j)),
        start=str(j.index.min().date()),
        end=str(j.index.max().date()),
        mean_pct=round(float(pct.mean()), 2),
        median_pct=round(float(pct.median()), 2),
        max_abs_pct=round(float(pct.abs().max()), 2),
    )


def _fresh(path: Path, ttl: float) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < ttl


def _ttl(year: int, today: dt.date) -> int:
    return 6 * 3600 if year >= today.year else 24 * 3600


def _read_series(path: Path) -> pd.Series:
    s = pd.read_csv(path, parse_dates=["date"], index_col="date").iloc[:, 0]
    s.index = pd.to_datetime(s.index).tz_localize(None)
    return s


def _write_series(path: Path, s: pd.Series, name="wind"):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    s.rename(name).rename_axis("date").to_csv(tmp)
    tmp.replace(path)


def _read_zones(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"], index_col="date")
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def _write_zones(path: Path, df: pd.DataFrame):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.rename_axis("date").to_csv(tmp)
    tmp.replace(path)


def _clip(obj, start, end):
    if obj is None or len(obj) == 0:
        return obj
    return obj[(obj.index >= pd.Timestamp(start)) & (obj.index < pd.Timestamp(end))]


def fetch_energinet_settlement_zones(start, end, cache_dir=None, today=None):
    """Official hourly settlement, daily mean MW per DK bidding zone. end exclusive."""
    cache_dir = Path(cache_dir or default_cache_dir())
    today = today or default_today()
    parts = []
    for y in range(start.year, end.year + 1):
        a = max(start, dt.date(y, 1, 1))
        b = min(end, dt.date(y + 1, 1, 1))
        if a >= b:
            continue
        path = cache_dir / f"dk_wind_settlement_zones_{y}.csv"
        if _fresh(path, _ttl(y, today)):
            parts.append(_read_zones(path))
            continue
        lo, _ = ec._cet_bounds(a, a + dt.timedelta(days=1))
        _, hi = ec._cet_bounds(b, b)
        raw = bp._eds("ProductionConsumptionSettlement", lo, hi, {"PriceArea": list(DK_ZONES)}, DK_WIND_SETTLE_COLS)
        if raw is None or raw.empty:
            country = cache_dir / f"dk_wind_settlement_{y}.csv"
            if not country.exists():
                print(f"DK settlement {y}: no rows", flush=True)
            continue
        raw = raw.assign(t=pd.to_datetime(raw["HourUTC"], utc=True))
        raw["mwh"] = raw[DK_WIND_MWH].astype(float).sum(axis=1, min_count=1)
        p = raw.pivot_table(index="t", columns="PriceArea", values="mwh", aggfunc="last")
        missing = [z for z in DK_ZONES if z not in p.columns]
        if missing:
            print(f"DK settlement {y}: missing {missing}", flush=True)
            continue
        cols = {}
        for z in DK_ZONES:
            cols[z] = daily_average_mw(p[z].sort_index(), 3600, min_cover=1.0)
        df = pd.DataFrame(cols).sort_index()
        _write_zones(path, df)
        # Keep the Pulse country-total cache in step when we refetch a year.
        country = cache_dir / f"dk_wind_settlement_{y}.csv"
        tot = df[list(DK_ZONES)].sum(axis=1, min_count=2).dropna()
        if len(tot):
            _write_series(country, tot)
        parts.append(df)
    if not parts:
        return pd.DataFrame(columns=list(DK_ZONES), dtype=float)
    out = pd.concat(parts)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return _clip(out, start, end)


def fetch_energinet_settlement_wind(start, end, cache_dir=None, today=None):
    """Official hourly settlement, DK1+DK2 onshore+offshore, daily mean MW. end exclusive."""
    cache_dir = Path(cache_dir or default_cache_dir())
    today = today or default_today()
    # Prefer a fresh country cache (written by the Pulse builder) when zone files are absent.
    parts = []
    missing_years = []
    for y in range(start.year, end.year + 1):
        a = max(start, dt.date(y, 1, 1))
        b = min(end, dt.date(y + 1, 1, 1))
        if a >= b:
            continue
        path = cache_dir / f"dk_wind_settlement_{y}.csv"
        if _fresh(path, _ttl(y, today)):
            parts.append(_read_series(path))
        else:
            missing_years.append(y)
    if missing_years:
        zones = fetch_energinet_settlement_zones(start, end, cache_dir=cache_dir, today=today)
        if zones is not None and not zones.empty:
            tot = zones[list(DK_ZONES)].sum(axis=1, min_count=2).dropna()
            if len(tot):
                parts.append(tot)
    if not parts:
        return pd.Series(dtype=float)
    out = pd.concat(parts)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return _clip(out, start, end)


def fetch_energinet_realtime_zones(start, end):
    """5-minute SCADA actuals, daily mean MW per DK bidding zone. end exclusive."""
    if start >= end:
        return pd.DataFrame(columns=list(DK_ZONES), dtype=float)
    lo, _ = ec._cet_bounds(start, start + dt.timedelta(days=1))
    _, hi = ec._cet_bounds(end, end)
    hi = min(pd.Timestamp(dt.datetime.now(dt.timezone.utc)), pd.Timestamp(hi))
    raw = bp._eds(
        "ElectricityProdex5MinRealtime", lo, hi, {"PriceArea": list(DK_ZONES)},
        ["Minutes5UTC", "PriceArea", "OffshoreWindPower", "OnshoreWindPower"],
    )
    if raw is None or raw.empty:
        return pd.DataFrame(columns=list(DK_ZONES), dtype=float)
    raw = raw.assign(t=pd.to_datetime(raw["Minutes5UTC"], utc=True))
    raw["wind"] = raw[["OffshoreWindPower", "OnshoreWindPower"]].astype(float).sum(axis=1, min_count=1)
    p = raw.pivot_table(index="t", columns="PriceArea", values="wind", aggfunc="last")
    if not set(DK_ZONES).issubset(p.columns):
        return pd.DataFrame(columns=list(DK_ZONES), dtype=float)
    cols = {z: daily_average_mw(p[z].sort_index(), 300, min_cover=DK_WIND_REALTIME_COVER) for z in DK_ZONES}
    df = pd.DataFrame(cols).sort_index()
    return _clip(df, start, end)


def fetch_energinet_realtime_wind(start, end):
    """5-minute SCADA actuals, DK1+DK2 onshore+offshore, daily mean MW. end exclusive."""
    zones = fetch_energinet_realtime_zones(start, end)
    if zones is None or zones.empty:
        return pd.Series(dtype=float)
    return zones[list(DK_ZONES)].sum(axis=1, min_count=2).dropna()


def daily_twh(mw: pd.Series) -> pd.Series:
    """CET-day TWh from a daily mean-MW series (DST-aware)."""
    if mw is None or len(mw) == 0:
        return pd.Series(dtype=float)
    s = _naive_series(mw).dropna()
    if s.empty:
        return s
    hours = [cet_hours(ts.date()) for ts in s.index]
    return pd.Series(s.values * pd.Series(hours, index=s.index).values / 1e6, index=s.index, dtype=float)


def monthly_twh(mw: pd.Series) -> dict:
    """{YYYY-MM: TWh} from a daily mean-MW series, only for months with every calendar day present."""
    twh = daily_twh(mw)
    if twh.empty:
        return {}
    out = {}
    for period, part in twh.groupby(twh.index.to_period("M")):
        need = period.days_in_month
        if len(part.dropna()) == need:
            out[str(period)] = float(part.sum())
    return out


def daily_gwh_list(mw: pd.Series, days) -> list:
    """GWh per CET day in `days`, or None where the MW series has no value."""
    s = _naive_series(mw) if mw is not None else pd.Series(dtype=float)
    out = []
    for d in days:
        ts = pd.Timestamp(d)
        if ts in s.index and pd.notna(s.loc[ts]):
            out.append(round(float(s.loc[ts]) * cet_hours(d) / 1000.0, 2))
        else:
            out.append(None)
    return out


def merged_dk_wind(start, end, entsoe=None, cache_dir=None, today=None):
    """Country-level daily MW and sources over [start, end). Today is excluded as incomplete."""
    today = today or default_today()
    cache_dir = cache_dir or default_cache_dir()
    end = min(end, today)
    sett = fetch_energinet_settlement_wind(start, end, cache_dir=cache_dir, today=today)
    rt_start = max(start, end - dt.timedelta(days=90))
    realtime = fetch_energinet_realtime_wind(rt_start, end + dt.timedelta(days=1))
    if realtime is not None and len(realtime):
        realtime = realtime[realtime.index < pd.Timestamp(today)]
    return merge_dk_wind(entsoe=entsoe, settlement=sett, realtime=realtime)


def merged_dk_wind_zones(start, end, entsoe_zones=None, cache_dir=None, today=None):
    """Daily MW and sources per DK zone over [start, end). entsoe_zones is {zone: Series}."""
    today = today or default_today()
    cache_dir = cache_dir or default_cache_dir()
    end = min(end, today)
    sett = fetch_energinet_settlement_zones(start, end, cache_dir=cache_dir, today=today)
    rt_start = max(start, end - dt.timedelta(days=90))
    realtime = fetch_energinet_realtime_zones(rt_start, end + dt.timedelta(days=1))
    if realtime is not None and len(realtime):
        realtime = realtime[realtime.index < pd.Timestamp(today)]
    entsoe_zones = entsoe_zones or {}
    out_mw, out_src = {}, {}
    for z in DK_ZONES:
        ev = entsoe_zones.get(z)
        sv = sett[z] if sett is not None and z in sett.columns else None
        rv = realtime[z] if realtime is not None and z in realtime.columns else None
        out_mw[z], out_src[z] = merge_dk_wind(entsoe=ev, settlement=sv, realtime=rv)
    return out_mw, out_src

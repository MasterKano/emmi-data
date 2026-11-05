"""Nordic hydro-region precipitation (rain + snow, water equivalent, mm) from Open-Meteo (free, non-commercial use,
attribution: Open-Meteo.com, CC BY 4.0; ERA5 data: Copernicus Climate Change Service).
  * monthly part: last 13 complete months from the historical weather API (ERA5) vs the 1991-2020 normal in
    data/precip_normals.json (same ERA5 source). Refreshed when older than 20 days or the last month is missing.
  * forecast part: next 14 days from the forecast API (best-match models) vs the 1991-2020 normal for the same days.
    Refreshed every run (daily with the power builds).
One batched request per part (all points in one call). Writes data/nordic_precip.json (a few KB)."""
import datetime as dt, json, os, sys, time
import requests
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from common import openmeteo, write_json  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from precip_regions import REGIONS
D = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUT, NORM = os.path.join(D, "nordic_precip.json"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "precip_normals.json")
ARCH, FC = "https://archive-api.open-meteo.com/v1/archive", "https://api.open-meteo.com/v1/forecast"
PTS = [(k, p) for k, R in REGIONS.items() for p in R["pts"]]


def call(url, **p):
    return openmeteo(url, [p for _, p in PTS], wait=70, daily="precipitation_sum", **p)


def by_region(js):
    """-> times, {region: [daily mean over its points]}"""
    t = js[0]["daily"]["time"]; acc = {}
    for (k, _), j in zip(PTS, js):
        acc.setdefault(k, []).append(j["daily"]["precipitation_sum"])
    out = {}
    for k, rows in acc.items():
        out[k] = [None if any(r[i] is None for r in rows) else sum(r[i] for r in rows) / len(rows) for i in range(len(t))]
    return t, out


def main():
    N = json.load(open(NORM))["regions"]
    old = {}
    if os.path.exists(OUT):
        try: old = json.load(open(OUT))
        except Exception: old = {}
    today = dt.date.today(); now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    first = today.replace(day=1)
    last = (first - dt.timedelta(days=1)).replace(day=1)
    if (today - first).days < 6:                       # ERA5 lags ~5 days: last month not final yet
        last = (last - dt.timedelta(days=1)).replace(day=1)
    lm = last.strftime("%Y-%m")
    mon = old.get("monthly")
    fresh = mon and mon.get("last") == lm and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(mon["generated_utc"])).days < 20
    if not fresh or "--force" in sys.argv:
        try:
            y, m = last.year - 1, last.month          # 13 months: same month last year .. last
            a = dt.date(y, m, 1); b = (last.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
            t, R = by_region(call(ARCH, start_date=a.isoformat(), end_date=b.isoformat(), models="era5"))
            months = sorted({x[:7] for x in t})
            reg = {}
            for k, v in R.items():
                mm = [round(sum(x for d, x in zip(t, v) if d.startswith(mo) and x is not None), 1) for mo in months]
                nn = [N[k]["month"][int(mo[5:]) - 1] for mo in months]
                reg[k] = dict(mm=mm, norm=nn)
            mon = dict(generated_utc=now, last=lm, months=months, regions=reg, model="ERA5 (Open-Meteo historical weather API)")
            print("precip monthly:", months[0], "-", months[-1])
        except Exception as e:  # noqa: BLE001
            print("precip monthly failed, keeping previous:", str(e)[:120])
            if mon: mon["stale"] = True
    fc = old.get("fc")
    try:
        t, R = by_region(call(FC, forecast_days=14))
        reg = {}
        for k, v in R.items():
            doys = [dt.date.fromisoformat(d).timetuple().tm_yday for d in t]
            nrm = [N[k]["doy"][min(x, len(N[k]["doy"])) - 1] for x in doys]
            reg[k] = dict(mm=round(sum(x or 0 for x in v), 1), norm=round(sum(nrm), 1),
                          daily=[None if x is None else round(x, 1) for x in v], dnorm=[round(x, 1) for x in nrm])
        fc = dict(generated_utc=now, start=t[0], end=t[-1], regions=reg, model="Open-Meteo forecast API (best match: ECMWF IFS, GFS, ICON, MET Nordic blend)")
        print("precip fc:", t[0], "-", t[-1])
    except Exception as e:  # noqa: BLE001
        print("precip forecast failed, keeping previous:", str(e)[:120])
        if fc: fc["stale"] = True
    if not mon and not fc:
        return
    out = dict(generated_utc=now, normal="1991-2020, ERA5 via Open-Meteo", monthly=mon, fc=fc,
               regions={k: dict(name=R["name"], zones=R["zones"], pts=R["pts"]) for k, R in REGIONS.items()},
               attribution="Weather data by Open-Meteo.com (CC BY 4.0); ERA5: Copernicus Climate Change Service",
               url="https://open-meteo.com/")
    write_json(OUT, out)
    print("wrote", OUT, os.path.getsize(OUT), "B")


if __name__ == "__main__":
    main()

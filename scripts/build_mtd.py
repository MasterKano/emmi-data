"""Nordic month-to-date panel: daily energy per bidding zone from the first day of the month two months back to the
last complete CET day, plus daily weather per country. The browser sums zones into countries and the Nordic total and
compares the current month with the same days of the previous month.
Sources: ENTSO-E Transparency (needs ENTSOE_API_TOKEN, never printed): A75 actual generation per type (hydro = run-of-river + reservoir; pumped storage B10 is
excluded), A65 actual load. Open-Meteo (CC BY 4.0): daily mean temperature (main load centres), precipitation and mean 10 m wind speed
(fixed points per country). Daily GWh = mean of the available hourly MW values x hours in the CET day; a day with
less than 90% of its hours reported is left empty (n/a). Writes data/nordic_mtd.json."""
import datetime as dt, os, sys, time
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_power as bp
from common import openmeteo, write_json  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "nordic_mtd.json")
ZONES = ["NO1", "NO2", "NO3", "NO4", "NO5", "SE1", "SE2", "SE3", "SE4", "FI", "DK1", "DK2"]
TYPES = ["hydro", "nuclear", "wind", "solar"]
# temperature: main load centres (same points as the 30-day dashboard)
TEMP_PTS = {"NO": [(59.91, 10.75), (60.39, 5.32), (63.43, 10.39)], "SE": [(59.33, 18.07), (57.71, 11.97), (55.60, 13.00)],
            "FI": [(60.17, 24.94), (61.50, 23.76), (65.01, 25.47)], "DK": [(55.68, 12.57), (56.16, 10.20)]}
# rain and wind: points spread over each country (hydro catchments in NO/SE, wind regions elsewhere)
GEO_PTS = {"NO": [(59.10, 7.10), (60.30, 7.40), (62.30, 9.40), (63.90, 10.20), (66.30, 14.30), (68.70, 18.30)],
           "SE": [(67.30, 18.40), (65.90, 16.20), (63.40, 15.00), (62.40, 17.30), (59.60, 15.40), (57.10, 14.80)],
           "FI": [(60.80, 24.80), (62.60, 22.00), (64.70, 25.30), (67.50, 26.50)],
           "DK": [(56.20, 8.50), (55.30, 9.40), (55.40, 11.80)]}
WX = [("temp", "temperature_2m_mean", TEMP_PTS, 1), ("rain", "precipitation_sum", GEO_PTS, 2), ("wind", "wind_speed_10m_mean", GEO_PTS, 2)]


def cet_days(d0, d1):
    return [d0 + dt.timedelta(days=i) for i in range((d1 - d0).days)]


def daily_gwh(s, days):
    """s: MW series (UTC index, any resolution) -> GWh per CET day, or None where < 90% of hours are present."""
    if s is None or not len(s):
        return [None] * len(days)
    h = s.resample("1h").mean().dropna()
    out = []
    for d in days:
        a, b = bp._cet_bounds(d, d + dt.timedelta(days=1))
        n = int((b - a) / pd.Timedelta("1h"))
        x = h[(h.index >= a) & (h.index < b)]
        out.append(round(float(x.mean()) * n / 1000, 2) if len(x) >= 0.9 * n else None)
    return out


def main():
    if not bp._token():
        print("mtd: ENTSOE_API_TOKEN not set; skipped"); return
    t0 = time.time()
    today = dt.datetime.now(bp.CET).date()
    m = today.month - 2
    d0 = dt.date(today.year + (m - 1) // 12, (m - 1) % 12 + 1, 1)
    days = cet_days(d0, today)          # complete days only (today excluded)
    per = bp._period(d0, today)
    Z, gaps = {}, []

    def one(z):
        eic = bp.ENTSOE_EIC[z]; rec = {}
        try:
            by = {}
            for it in bp.entsoe_get(dict(documentType="A75", processType="A16", in_Domain=eic, **per)):
                if not it["is_in"] or it["psr"] == "B10":   # hydro excludes pumped storage
                    continue
                k = bp.PSR.get(it["psr"], "other")
                if k in TYPES:
                    by.setdefault(k, []).append(it["s"])
            for k in TYPES:
                if k in by:
                    v = daily_gwh(bp._sum(by[k]), days)
                    if any(x and x > 0.01 for x in v):
                        rec[k] = v
        except Exception as e:  # noqa: BLE001
            gaps.append(f"A75 {z}: {bp._redact(e)[:60]}")
        try:
            items = bp.entsoe_get(dict(documentType="A65", processType="A16", outBiddingZone_Domain=eic, **per))
            rec["load"] = daily_gwh(bp._combine(items), days)
        except Exception as e:  # noqa: BLE001
            gaps.append(f"A65 {z}: {bp._redact(e)[:60]}")
        print("mtd", z, "ok", round(time.time() - t0), "s", flush=True)
        return z, rec
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        for z, rec in ex.map(one, ZONES):
            Z[z] = rec
    iso = [d.isoformat() for d in days]
    wx = {}
    for key, var, P, nd in WX:
        try:
            pts = [p for c in P for p in P[c]]
            for att in range(3):  # Open-Meteo read timeouts happen on CI; retry before giving up
                try:
                    js = openmeteo("https://api.open-meteo.com/v1/forecast", pts, wait=60, timeout=90, daily=var,
                                   past_days=min(92, (today - d0).days + 1), forecast_days=1, wind_speed_unit="ms",
                                   timezone="Europe/Berlin")
                    break
                except Exception:  # noqa: BLE001
                    if att == 2:
                        raise
                    time.sleep(30 * (att + 1))
            i, res, allv = 0, {}, {}
            for c, cp in P.items():
                rows = [j["daily"] for j in js[i:i + len(cp)]]; i += len(cp)
                mp = {}
                for q, d in enumerate(rows[0]["time"]):
                    v = [r[var][q] for r in rows if r[var][q] is not None]
                    allv.setdefault(d, []).extend(v)
                    mp[d] = round(sum(v) / len(v), nd) if len(v) == len(cp) else None
                res[c] = [mp.get(d) for d in iso]
            res["Nordic"] = [round(sum(allv[d]) / len(allv[d]), nd) if d in allv and len(allv[d]) == len(pts) else None for d in iso]
            wx[key] = res
            time.sleep(2)
        except Exception as e:  # noqa: BLE001
            gaps.append(f"Open-Meteo {key}: {str(e)[:60]}")
    out = dict(generated_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), start=iso[0], days=iso,
               zones=Z, types=TYPES + ["load"], wx=wx,
               wx_pts={k: {c: len(v) for c, v in P.items()} for k, _, P, _ in WX},
               src=dict(entsoe="https://transparency.entsoe.eu", openmeteo="https://open-meteo.com"),
               gaps=gaps, build_s=round(time.time() - t0))
    write_json(OUT, out)
    print(f"mtd: {len(iso)} days {iso[0]}..{iso[-1]}, {os.path.getsize(OUT)} B, {out['build_s']} s, gaps {gaps}")


if __name__ == "__main__":
    main()

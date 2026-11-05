"""Nordic power dashboard: rolling 30 days, hourly, per bidding zone (countries and Nordic are summed in the browser).
Sources: ENTSO-E Transparency (needs ENTSOE_API_TOKEN, never printed): A75 actual generation per type, A44 day-ahead
prices, A65 actual load, A69 day-ahead wind forecast. Open-Meteo (CC BY 4.0): hourly temperature for main load centres,
daily precipitation for hydro regions. Reused files: data/hydro_nordic.json (reservoirs vs normal), data/power.json (gas,
carbon settlements; nuclear UMM units). Net imports are derived (load - generation). Writes data/nordic_dash.json."""
import datetime as dt, json, os, sys, time
import pandas as pd, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_power as bp
from precip_regions import REGIONS
from common import openmeteo, write_json  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "nordic_dash.json")
# Hourly zone series are delta-encoded to keep the file small (~20% less): value x scale rounded to an integer,
# each entry = that integer minus the previous non-null one (first one absolute), null stays null. dash.js decodes
# on load: acc += e; value = acc / scale. Prices have 0.1 EUR/MWh resolution, MW series are whole numbers.
ENC = {"kind": "delta", "scale": {"p": 10, "l": 1, "wf": 1, "g": 1}}


def _delta(a, scale):
    out, prev = [], 0
    for v in a:
        if v is None:
            out.append(None)
            continue
        x = int(round(v * scale))
        out.append(x - prev)
        prev = x
    return out


def encode_zones(Z):
    sc = ENC["scale"]
    E = {}
    for z, S in Z.items():
        E[z] = {k: (_delta(v, sc[k]) if k in sc and k != "g" and isinstance(v, list) else v) for k, v in S.items() if k != "g"}
        if isinstance(S.get("g"), dict):
            E[z]["g"] = {t: (_delta(a, sc["g"]) if isinstance(a, list) else a) for t, a in S["g"].items()}
    return E
ZONES = ["NO1", "NO2", "NO3", "NO4", "NO5", "SE1", "SE2", "SE3", "SE4", "FI", "DK1", "DK2"]
TYPES = ["hydro", "nuclear", "wind", "solar", "thermal"]
CITIES = {"NO": [(59.91, 10.75), (60.39, 5.32), (63.43, 10.39)], "SE": [(59.33, 18.07), (57.71, 11.97), (55.60, 13.00)],
          "FI": [(60.17, 24.94), (61.50, 23.76), (65.01, 25.47)], "DK": [(55.68, 12.57), (56.16, 10.20)]}
DAYS = 30


def hourly(s, idx):
    if s is None or not len(s):
        return [None] * len(idx)
    h = s.resample("1h").mean().reindex(idx)
    return [None if pd.isna(v) else v for v in h.values]


def ints(a, nd=0):
    return [None if v is None else (round(v) if nd == 0 else round(v, nd)) for v in a]


def om(url, pts, **p):
    return openmeteo(url, pts, wait=60, timeout=60, **p)


def main():
    if not bp._token():
        print("dashboard: ENTSOE_API_TOKEN not set; skipped"); return
    t_start = time.time(); today = dt.date.today()
    d0 = today - dt.timedelta(days=DAYS)
    a, b = bp._cet_bounds(d0, today + dt.timedelta(days=2))
    idx = pd.date_range(a, b, freq="1h", inclusive="left")
    now_h = pd.Timestamp.now(tz="UTC").floor("1h")
    per = bp._period(d0, today + dt.timedelta(days=1)); per2 = bp._period(d0, today + dt.timedelta(days=2))
    Z, gaps = {}, []

    def one(z):
        eic = bp.ENTSOE_EIC[z]; rec = {"g": {}}
        try:
            by = {}
            for it in bp.entsoe_get(dict(documentType="A75", processType="A16", in_Domain=eic, **per)):
                if not it["is_in"]:
                    continue
                k = bp.PSR.get(it["psr"], "other"); k = k if k in TYPES else "thermal"
                by.setdefault(k, []).append(it["s"])
            for k in TYPES:
                if k in by:
                    v = hourly(bp._sum(by[k]), idx)
                    if any(x and x > 0.5 for x in v):
                        rec["g"][k] = ints(v)
        except Exception as e:  # noqa: BLE001
            gaps.append(f"A75 {z}: {bp._redact(e)[:60]}")
        try:
            items = bp.entsoe_get(dict(documentType="A44", in_Domain=eic, out_Domain=eic, **per2))
            rec["p"] = ints(hourly(bp._combine(items), idx), 1)
        except Exception as e:  # noqa: BLE001
            gaps.append(f"A44 {z}: {bp._redact(e)[:60]}")
        try:
            items = bp.entsoe_get(dict(documentType="A65", processType="A16", outBiddingZone_Domain=eic, **per))
            rec["l"] = ints(hourly(bp._combine(items), idx))
        except Exception as e:  # noqa: BLE001
            gaps.append(f"A65 {z}: {bp._redact(e)[:60]}")
        try:
            w = [it["s"] for it in bp.entsoe_get(dict(documentType="A69", processType="A01", in_Domain=eic, **per2))
                 if bp.PSR.get(it["psr"]) == "wind"]
            if w:
                rec["wf"] = ints(hourly(bp._sum(w), idx))
        except Exception as e:  # noqa: BLE001
            if "No matching data" not in str(e):
                gaps.append(f"A69 {z}: {bp._redact(e)[:60]}")
        # trim generation/load beyond the last complete hour
        cut = int((now_h - idx[0]) / pd.Timedelta("1h"))
        for k in rec["g"]:
            rec["g"][k] = [v if i < cut else None for i, v in enumerate(rec["g"][k])]
        if "l" in rec:
            rec["l"] = [v if i < cut else None for i, v in enumerate(rec["l"])]
        print(z, "ok", round(time.time() - t_start), "s", flush=True)
        return z, rec
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        for z, rec in ex.map(one, ZONES):
            Z[z] = rec
    days = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(DAYS + 2)]
    # temperature (daily mean per country, main load centres) and rain (daily, hydro regions)
    temp, rain = {}, {}
    try:
        pts = [p for c in CITIES for p in CITIES[c]]
        js = om("https://api.open-meteo.com/v1/forecast", pts, daily="temperature_2m_mean", past_days=DAYS + 1, forecast_days=2)
        i = 0
        for c, cp in CITIES.items():
            rows = [j["daily"] for j in js[i:i + len(cp)]]; i += len(cp)
            tm = rows[0]["time"]; m = {}
            for k, d in enumerate(tm):
                v = [r["temperature_2m_mean"][k] for r in rows if r["temperature_2m_mean"][k] is not None]
                m[d] = round(sum(v) / len(v), 1) if v else None
            temp[c] = [m.get(d) for d in days]
        time.sleep(2)
        pts = [p for R in REGIONS.values() for p in R["pts"]]
        js = om("https://api.open-meteo.com/v1/forecast", pts, daily="precipitation_sum", past_days=DAYS + 1, forecast_days=2)
        i = 0
        for k, R in REGIONS.items():
            rows = [j["daily"] for j in js[i:i + len(R["pts"])]]; i += len(R["pts"])
            m = {}
            for q, d in enumerate(rows[0]["time"]):
                v = [r["precipitation_sum"][q] for r in rows if r["precipitation_sum"][q] is not None]
                m[d] = round(sum(v) / len(v), 1) if v else None
            rain[k] = [m.get(d) for d in days]
    except Exception as e:  # noqa: BLE001
        gaps.append(f"Open-Meteo: {str(e)[:60]}")
    # reservoirs vs normal (weekly, NVE + ENTSO-E A72), gas & carbon (EEX/ICE settlements)
    res, fuel, units = None, {}, []
    try:
        H = json.load(open(os.path.join(ROOT, "data", "hydro_nordic.json")))
        res = {"year": H.get("year"), "hist": H.get("hist")}
        for k in ("nose", "no", "se"):
            B = H.get(k)
            if B:
                res[k] = {"cur": B["cur"], "med": [x[1] if x else None for x in B["band"]]}
    except Exception as e:  # noqa: BLE001
        gaps.append(f"reservoirs: {str(e)[:60]}")
    try:
        P = json.load(open(os.path.join(ROOT, "data", "power.json")))
        for x in (P.get("eex") or {}).get("drivers") or []:
            if x["key"] in ("ttf_da", "ttf_m", "eua"):
                fuel[x["key"]] = dict(name=x["name"], unit=x["unit"], d=x["dates"], v=x["hist"])
        nu = P.get("nuclear") or {}
        units = [dict(n=u.get("n"), c=u.get("c"), cap=u.get("cap"), now=u.get("now"), start=u.get("start"), end=u.get("back"), why=u.get("why"))
                 for u in (nu.get("units") or [])][:40]
    except Exception as e:  # noqa: BLE001
        gaps.append(f"power.json: {str(e)[:60]}")
    out = dict(generated_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), t0=bp._ts(idx[0]), n=len(idx),
               enc=ENC, days=days, zones=encode_zones(Z), types=TYPES, temp=temp, rain=rain, rain_regions={k: R["name"] for k, R in REGIONS.items()},
               res=res, fuel=fuel, nuc_units=units, gaps=gaps, build_s=round(time.time() - t_start))
    write_json(OUT, out)
    print(f"dashboard: {len(idx)} h, {os.path.getsize(OUT)} B, {out['build_s']} s, gaps {gaps}")


if __name__ == "__main__":
    main()

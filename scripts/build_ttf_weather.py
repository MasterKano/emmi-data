"""NW Europe temperature and heating degree days for gas (TTF): latest ECMWF IFS run vs the previous run vs normal.
Source: ECMWF IFS 0.25 deg (open data) via Open-Meteo (free, non-commercial use, CC BY 4.0): the single-runs API, so
each run is fetched by its own initialisation time (latest 00z/12z run and the run 12 h before it), one batched
request per run for all 10 cities. Normal: scripts/nwe_temp_normals.json (ERA5 1991-2020, built once, static).
  * day 1 = first full UTC day of the latest run (00z run: its own date, 12z run: the next day); days 1-14
    (both runs cover all 14 days: 360 h for 00z/12z).
  * NW Europe temperature = population-weighted mean of 10 cities (UK, FR, NL, BE, DE; weights in WEIGHTS).
  * HDD = 18 - T when T <= 15 deg C, else 0 (Eurostat), on the weighted daily mean; sums for days 1-7, 8-14, 1-14.
  * Indicative EU gas demand change = HDD change x 0.72 TWh per degree-day (EU27 ~0.72 TWh/d per 1 deg C colder,
    REKK Policy Brief 2026/01). Indicative only: NW Europe temperatures stand in for the whole EU.
Writes data/ttf_weather.json (a few KB). With --skip-if-same FILE: exits 3 without writing when FILE already holds
the same run (so a catch-up run publishes nothing)."""
import argparse, datetime as dt, json, os, sys, time
import requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import openmeteo, write_json  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "data", "ttf_weather.json")
NORM = os.path.join(HERE, "nwe_temp_normals.json")
META = "https://api.open-meteo.com/data/ecmwf_ifs025/static/meta.json"
RUNS = "https://single-runs-api.open-meteo.com/v1/forecast"
MODEL = "ecmwf_ifs025"
CITIES = [("London", 51.51, -0.13), ("Manchester", 53.48, -2.24), ("Paris", 48.86, 2.35), ("Lyon", 45.76, 4.84),
          ("Amsterdam", 52.37, 4.90), ("Brussels", 50.85, 4.35), ("Berlin", 52.52, 13.40), ("Hamburg", 53.55, 9.99),
          ("Frankfurt", 50.11, 8.68), ("Munich", 48.14, 11.58)]
WEIGHTS = {"London": 34, "Manchester": 34, "Paris": 34, "Lyon": 34, "Amsterdam": 18, "Brussels": 12,
           "Berlin": 21, "Hamburg": 21, "Frankfurt": 21, "Munich": 21}
TWH_PER_HDD = 0.72
NDAYS, MIN_HOURS = 14, 20
hdd = lambda t: None if t is None else round(18 - t if t <= 15 else 0.0, 2)
iso = lambda t: t.strftime("%Y-%m-%dT%H:%MZ")


def latest_full_run():
    """Latest 00z/12z initialisation on Open-Meteo (06z/18z runs only reach 144 h, so they are skipped)."""
    for i in range(3):
        try:
            m = requests.get(META, timeout=60).json(); break
        except Exception:
            if i == 2: raise
            time.sleep(20)
    t = dt.datetime.fromtimestamp(m["last_run_initialisation_time"], dt.timezone.utc)
    if t.hour % 12: t -= dt.timedelta(hours=6)
    avail = dt.datetime.fromtimestamp(m["last_run_availability_time"], dt.timezone.utc)
    return t.replace(minute=0, second=0, microsecond=0), avail


def run_daily(run):
    """{date: weighted daily mean} for one run, UTC days with >= MIN_HOURS hourly values in every city."""
    js = openmeteo(RUNS, [(a, b) for _, a, b in CITIES], hourly="temperature_2m", models=MODEL,
                   run=run.strftime("%Y-%m-%dT%H:%M"), forecast_days=16, timezone="GMT")
    per = []
    for j in js:
        acc = {}
        for t, v in zip(j["hourly"]["time"], j["hourly"]["temperature_2m"]):
            if v is not None: acc.setdefault(t[:10], []).append(v)
        per.append({d: sum(v) / len(v) for d, v in acc.items() if len(v) >= MIN_HOURS})
    tot = sum(WEIGHTS.values()); out = {}
    for d in set.intersection(*(set(p) for p in per)):
        out[d] = round(sum(p[d] * WEIGHTS[c] for p, (c, _, _) in zip(per, CITIES)) / tot, 2)
    return out


def build(run):
    prev = run - dt.timedelta(hours=12)
    day1 = (run + dt.timedelta(hours=12)).date()
    dates = [(day1 + dt.timedelta(i)).isoformat() for i in range(NDAYS)]
    L, P = run_daily(run), run_daily(prev)
    if not all(d in L for d in dates):
        raise ValueError(f"run {iso(run)} incomplete: {sum(d in L for d in dates)}/{NDAYS} days")
    nm = json.load(open(NORM))["normal"]
    days = []
    for d in dates:
        lt, pt, no = L[d], P.get(d), nm[d[5:]]
        days.append({"date": d, "t": lt, "t_prev": pt, "t_norm": no, "hdd": hdd(lt), "hdd_prev": hdd(pt), "hdd_norm": hdd(no)})
    win = {}
    for k, a, b in (("d1_7", 0, 7), ("d8_14", 7, 14), ("d1_14", 0, 14)):
        S = lambda f: None if any(x[f] is None for x in days[a:b]) else round(sum(x[f] for x in days[a:b]), 1)
        h, hp, hn = S("hdd"), S("hdd_prev"), S("hdd_norm")
        dp = None if hp is None else round(h - hp, 1); dn = round(h - hn, 1)
        win[k] = {"days": [a + 1, b], "hdd": h, "hdd_prev": hp, "hdd_norm": hn, "d_prev": dp, "d_norm": dn,
                  "twh_vs_prev": None if dp is None else round(dp * TWH_PER_HDD, 1), "twh_vs_norm": round(dn * TWH_PER_HDD, 1)}
    missing = [x["date"] for x in days if x["t_prev"] is None]
    return {"generated_utc": iso(dt.datetime.now(dt.timezone.utc)), "run_utc": iso(run), "prev_run_utc": iso(prev),
            "source": "ECMWF IFS 0.25 (open data) via Open-Meteo", "model": MODEL,
            "region": "NW Europe: population-weighted mean of " + ", ".join(c for c, _, _ in CITIES),
            "weights": WEIGHTS, "hdd_base": "18 - T when T <= 15 C (Eurostat), on the weighted daily mean, UTC days",
            "normal": "ERA5 1991-2020 (Open-Meteo archive), 7-day centred mean",
            "twh_per_hdd": TWH_PER_HDD, "twh_note": "indicative: EU27 gas demand ~0.72 TWh/d per 1 C colder (REKK Policy Brief 2026/01); NW Europe temperature used as a proxy",
            "days": days, "windows": win, "gaps": [f"previous run missing {d}" for d in missing]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--skip-if-same", default=None)
    a = ap.parse_args()
    run, avail = latest_full_run()
    if a.skip_if_same and os.path.exists(a.skip_if_same):
        try:
            if json.load(open(a.skip_if_same)).get("run_utc") == iso(run):
                print(f"run {iso(run)} already published; nothing to do"); sys.exit(3)
        except ValueError:
            pass
    try:
        o = build(run)
    except ValueError as e:   # latest run not complete on Open-Meteo yet: use the one before
        print("::warning::", e); run -= dt.timedelta(hours=12); o = build(run)
    write_json(a.out, o)
    w = o["windows"]
    print(f"run {o['run_utc']} (Open-Meteo last availability {iso(avail)}) vs {o['prev_run_utc']}: "
          + " | ".join(f"{k} HDD {v['hdd']} d_prev {v['d_prev']} d_norm {v['d_norm']} TWh {v['twh_vs_prev']}" for k, v in w.items()))


if __name__ == "__main__":
    main()

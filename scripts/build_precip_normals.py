"""One-off: 1991-2020 precipitation normals per region from the Open-Meteo historical weather API (ERA5).
Writes scripts/precip_normals.json (monthly normals in mm and a smoothed day-of-year daily normal). Run rarely; it
fetches 30 years per point, one point per request with pauses, to stay inside Open-Meteo's free-use limits."""
import json, os, sys, time
import pandas as pd, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from precip_regions import REGIONS
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "precip_normals.json")
API = "https://archive-api.open-meteo.com/v1/archive"

def point(lat, lon):
    for i in range(5):
        r = requests.get(API, params=dict(latitude=lat, longitude=lon, start_date="1991-01-01", end_date="2020-12-31",
                                          daily="precipitation_sum", models="era5", timezone="GMT"), timeout=180)
        if r.status_code == 429:
            time.sleep(90 * (i + 1)); continue
        r.raise_for_status(); d = r.json()["daily"]
        return pd.Series(d["precipitation_sum"], index=pd.to_datetime(d["time"]), dtype="float64")
    raise RuntimeError("rate limited")

out = dict(period="1991-2020", source="Open-Meteo historical weather API, ERA5 reanalysis", regions={})
for k, R in REGIONS.items():
    ss = []
    for lat, lon in R["pts"]:
        ss.append(point(lat, lon)); print(k, lat, lon, round(ss[-1].sum() / 30), "mm/yr", flush=True); time.sleep(75)
    s = pd.concat(ss, axis=1).mean(axis=1)
    mon = s.groupby([s.index.year, s.index.month]).sum().groupby(level=1).mean()
    doy = s.groupby(s.index.dayofyear).mean()
    doy = pd.concat([doy.iloc[-15:], doy, doy.iloc[:15]]).rolling(31, center=True).mean().iloc[15:-15]   # 31-day smoothing
    out["regions"][k] = dict(month=[round(float(mon[m]), 1) for m in range(1, 13)], doy=[round(float(v), 2) for v in doy.values], year=round(float(s.sum() / 30)))
json.dump(out, open(OUT, "w"), separators=(",", ":"))
print("wrote", OUT, os.path.getsize(OUT))

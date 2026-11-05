"""Finland wind power from Fingrid open data (data.fingrid.fi, CC BY 4.0), needs FINGRID_API_KEY.
Datasets: 75 wind generation (15 min, MW), 245 wind generation forecast (15 min, MW, updated every 15 min),
268 installed wind capacity (MW). Output data/fi_wind.json: hourly means, last 14 days actual + forecast to +48 h.
Retention: 14 days (rolling). The key is never printed or logged."""
import datetime as dt, json, os, sys, time
import requests
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "fi_wind.json")
API = "https://data.fingrid.fi/api/datasets/{}/data"
DAYS = 14

def fetch(key, ds, a, b):
    for i in range(4):
        r = requests.get(API.format(ds), headers={"x-api-key": key, "Accept": "application/json"}, timeout=40,
                         params=dict(startTime=a.strftime("%Y-%m-%dT%H:%M:%SZ"), endTime=b.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     format="json", pageSize=20000, sortOrder="asc"))
        if r.status_code == 429:
            time.sleep(8); continue
        if r.status_code in (401, 403):
            raise PermissionError(f"Fingrid {ds}: HTTP {r.status_code}")
        r.raise_for_status()
        return r.json().get("data") or []
    raise RuntimeError(f"Fingrid {ds}: rate limited")

def hourly(rows):
    acc = {}
    for x in rows:
        if x.get("value") is None: continue
        h = x["startTime"][:13]
        s = acc.setdefault(h, [0.0, 0]); s[0] += x["value"]; s[1] += 1
    return {h: v[0] / v[1] for h, v in acc.items() if v[1] >= 3}   # >= 3 of 4 quarter-hours

def main():
    keys = [k for k in ((os.environ.get(v) or "").strip() for v in ("FINGRID_API_KEY", "FINGRID_API_KEY_SECONDARY")) if k]
    key = keys[0] if keys else ""
    if not key:
        print("fi_wind: FINGRID_API_KEY not set, skipping"); return 0
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    t0 = now - dt.timedelta(days=DAYS); t1 = now + dt.timedelta(hours=48)
    try:
        try:
            raw = fetch(key, 75, t0, now + dt.timedelta(hours=1))
        except PermissionError:
            if len(keys) < 2: raise
            print("fi_wind: primary key rejected, using secondary"); key = keys[1]; time.sleep(7)
            raw = fetch(key, 75, t0, now + dt.timedelta(hours=1))
        act = hourly(raw); time.sleep(7)
        fc = hourly(fetch(key, 245, t0, t1)); time.sleep(7)
        cap = fetch(key, 268, now - dt.timedelta(days=3), now + dt.timedelta(hours=1))
    except Exception as e:
        print(f"fi_wind: {type(e).__name__}: {str(e)[:120]}"); return 0
    if not act:
        print("fi_wind: no data, keeping old file"); return 0
    hrs = [t0 + dt.timedelta(hours=i) for i in range(int((t1 - t0).total_seconds() // 3600))]
    k = lambda t: t.strftime("%Y-%m-%dT%H")
    a = [round(act[k(t)]) if k(t) in act else None for t in hrs]
    f = [round(fc[k(t)]) if k(t) in fc else None for t in hrs]
    while a and a[-1] is None and f and f[-1] is None: a.pop(); f.pop(); hrs.pop()
    last = max(i for i, v in enumerate(a) if v is not None)
    capv = cap[-1]["value"] if cap else None
    win = [v for v in a if v is not None]
    out = dict(generated_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), t0=hrs[0].strftime("%Y-%m-%dT%H:00:00Z"),
               n=len(hrs), act=a, fc=f, cap=round(capv) if capv else None,
               last=dict(t=hrs[last].strftime("%Y-%m-%dT%H:00:00Z"), mw=a[last]),
               avg14=round(sum(win) / len(win)), max14=max(win), min14=min(win),
               src="Fingrid open data (CC BY 4.0)", url="https://data.fingrid.fi/en/datasets/75",
               ds=dict(act=75, fc=245, cap=268))
    with open(OUT, "w") as fh: json.dump(out, fh, separators=(",", ":"))
    print(f"fi_wind: {len(hrs)} h, last {a[last]} MW, cap {out['cap']} MW, {os.path.getsize(OUT)} B")
    return 0

if __name__ == "__main__":
    sys.exit(main())

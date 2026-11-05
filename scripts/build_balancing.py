"""German balancing-capacity auctions (regelleistung.net, keyless): FCR and aFRR capacity-market results.

Per delivery day, the public results-overview export (one small XLSX per product and day) gives:
  FCR  : Germany settlement capacity price per 4-hour product, EUR/MW  -> stored as the day's sum (EUR/MW per day) and the 6 blocks
  aFRR : Germany volume-weighted average capacity price per 4-hour product, POS and NEG, (EUR/MW)/h -> stored as the day's mean
Keeps 90 days in data/balancing.json; each run fetches only days not yet stored (first run back-fills, then ~2 small requests a day).
"""
import datetime as dt, io, json, os, sys, time
import requests, openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "balancing.json")
API = "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders/resultsoverview"
KEEP = 90
H = {"User-Agent": "Mozilla/5.0 (markets-monitor; public data)"}


def rows(day, pt):
    r = requests.get(API, params=dict(date=day, exportFormat="xlsx", market="CAPACITY", productTypes=pt), headers=H, timeout=60)
    r.raise_for_status()
    ws = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True).active
    it = ws.iter_rows(values_only=True)
    head = next(it)
    return [dict(zip(head, x)) for x in it if x and x[0] is not None]


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def day_rec(day):
    f = rows(day, "FCR")
    fb = [num(x.get("GERMANY_SETTLEMENTCAPACITY_PRICE_[EUR/MW]")) for x in sorted(f, key=lambda x: str(x.get("PRODUCTNAME")))]
    a = rows(day, "aFRR")
    av = {"POS": [], "NEG": []}
    for x in sorted(a, key=lambda x: str(x.get("PRODUCT"))):
        p = str(x.get("PRODUCT") or "")
        v = num(x.get("GERMANY_AVERAGE_CAPACITY_PRICE_[(EUR/MW)/h]"))
        if p[:3] in av and v is not None:
            av[p[:3]].append(v)
    fb = [v for v in fb if v is not None]
    if len(fb) < 6 or not av["POS"] or not av["NEG"]:
        return None
    return dict(fcr=round(sum(fb), 2), fcr_b=[round(v, 2) for v in fb],
                pos=round(sum(av["POS"]) / len(av["POS"]), 3), neg=round(sum(av["NEG"]) / len(av["NEG"]), 3))


def main():
    t0 = time.time()
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT))
        except Exception:  # noqa: BLE001
            prev = {}
    days = dict(prev.get("days") or {})
    today = dt.date.today()
    want = [(today + dt.timedelta(days=1) - dt.timedelta(days=i)).isoformat() for i in range(KEEP)]  # D+1 is auctioned on D
    errs = []
    for d in sorted(want):
        if d in days:
            continue
        try:
            rec = day_rec(d)
            if rec:
                days[d] = rec
        except Exception as e:  # noqa: BLE001
            if d <= today.isoformat():
                errs.append(f"{d}: {str(e)[:60]}")
        time.sleep(0.2)
    days = {k: v for k, v in days.items() if k in want}
    out = dict(generated_utc=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), source="regelleistung.net (Datacenter, results overview, capacity market)",
               url="https://www.regelleistung.net/apps/datacenter/tenders/",
               units=dict(fcr="EUR/MW per delivery day (sum of six 4-hour products, Germany settlement price)",
                          pos="(EUR/MW)/h, mean of six 4-hour products, Germany volume-weighted average", neg="(EUR/MW)/h, same, negative aFRR"),
               days=dict(sorted(days.items())), errors=errs[-10:], build_s=round(time.time() - t0))
    if not days:
        print("no balancing data; keeping previous file", errs[:3]); sys.exit(0)
    open(OUT, "w").write(json.dumps(out, separators=(",", ":")))
    print(f"balancing.json: {len(days)} days, {os.path.getsize(OUT)} B, {out['build_s']} s, {len(errs)} errors")


if __name__ == "__main__":
    main()

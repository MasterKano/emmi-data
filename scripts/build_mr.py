"""Power tab 'MR' block (price development, futures, monthly table, commodities) and the seasonal
Jan-Dec charts in 'Supply & weather': writes data/mr.json (~150 KB, loaded on demand).
Sources (public, official):
  * Nord Pool day-ahead AggregatePrices (data.nordpoolgroup.com): SYS and area daily / monthly averages, 2024 on
  * Nord Pool published yearly SYS averages 2011-2023 (Nord Pool annual press releases / yearly statistics)
  * ENTSO-E Transparency A44 day-ahead prices: zone monthly averages 2015-2023 (ENTSOE_API_TOKEN, never printed)
  * EEX public daily settlements (Nordic system + zonal base futures, TTF, EUA; API2 coal from ICE), from the
    scrape repo's master file (master/eex_master.csv)
  * Ember monthly electricity data (CC BY 4.0): Nordic demand, generation by type, net exports
  * ERA5 reanalysis (Copernicus C3S) monthly wind / precipitation / temperature, area 55-71N 4-32E
Definitions:
  * 30-day MA = trailing mean of the last 30 daily averages (Nord Pool, EUR/MWh)
  * quarters of the current year = simple mean of the three monthly averages; a month in progress = hour-weighted
    actual days to date + the remaining days at the month future; later months = month future (fallback quarter, year)
  * current-year full year = simple mean of the four quarters; next year = mean of its four quarter futures
    (fallback Cal); the year after = Cal future
Incremental: completed years are reused from the previous data/mr.json; Ember is refetched weekly."""
import datetime as dt, io, json, math, os, sys
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import write_json  # noqa: E402
import build_power as bp       # noqa: E402  (HTTP helpers, token redaction)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "mr.json")
TZ = ZoneInfo("Europe/Stockholm")  # CET/CEST delivery days
AREAS = ["SYS", "SE1", "SE2", "SE3", "SE4", "FI", "NO1", "NO2", "NO3", "NO4", "NO5", "DK1", "DK2", "DE-LU"]
NP_CODE = {"DE-LU": "GER"}
FUT = {"SYS": "Nordic", "DE-LU": "DE"}
NP_FIRST, EN_FIRST = 2024, 2015
NP_URL = bp.NP_API + "AggregatePrices"
# Nord Pool yearly system price, EUR/MWh (published; the public API has no data before 2024)
SYS_YEARLY = {2011: 47.05, 2012: 31.20, 2013: 38.10, 2014: 29.61, 2015: 20.98, 2016: 26.91, 2017: 29.41,
              2018: 43.99, 2019: 38.94, 2020: 10.93, 2021: 62.31, 2022: 135.86, 2023: 56.44}
EMBER_CSV = "https://files.ember-energy.org/public-downloads/generation/outputs/release_generation_monthly_lower.csv"
EMBER_SRC = {"demand": "Demand", "gen": "Total generation", "netimp": "Net imports", "hydro": "Hydro",
             "nuclear": "Nuclear", "wind": "Wind", "solar": "Solar", "fossil": "Fossil", "bio": "Bioenergy"}
EMBER_C = {"NO": "Norway", "SE": "Sweden", "FI": "Finland", "DK": "Denmark"}
WX_HIST = "https://raw.githubusercontent.com/MasterKano/mr-climate/main/data/weather_history.csv"
WX_FEED = "https://raw.githubusercontent.com/MasterKano/mr-climate/main/data/weather_excel_feed.csv"
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
gaps = []


def log(*a):
    print("[mr]", *a, flush=True)


def r2(v, n=2):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), n)


def hours(d):
    """Length of the CET/CEST delivery day (23/24/25 h)."""
    a = dt.datetime(d.year, d.month, d.day, tzinfo=TZ)
    b = a + dt.timedelta(days=1)
    b = dt.datetime(b.year, b.month, b.day, tzinfo=TZ)
    return round((b - a).total_seconds() / 3600)


def month_end(y, m):
    return (dt.date(y + m // 12, m % 12 + 1, 1) - dt.timedelta(days=1))


def mkeys(a, b):
    y, m, out = a.year, a.month, []
    while (y, m) <= (b.year, b.month):
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ---------------------------------------------------------------- Nord Pool
def nordpool(today, prev):
    """{date: {area: daily average}}; completed years are taken from the previous file."""
    daily = {}
    pd_ = (prev or {}).get("daily") or {}
    if pd_.get("start"):
        p0 = dt.date.fromisoformat(pd_["start"])
        for a, vals in (pd_.get("v") or {}).items():
            for i, v in enumerate(vals):
                if v is not None:
                    daily.setdefault(p0 + dt.timedelta(days=i), {})[a] = v
    for y in range(NP_FIRST, today.year + 1):
        full = all(a in (daily.get(dt.date(y, 12, 31)) or {}) for a in AREAS) and all(
            dt.date(y, 1, 1) + dt.timedelta(days=i) in daily for i in range(365))
        if full and y < today.year and not (y == today.year - 1 and today.month == 1):
            continue
        try:
            js = bp.get(NP_URL, tries=3, headers=bp.NP_HDR, params=dict(
                year=y, market="DayAhead", currency="EUR",
                deliveryArea=",".join(NP_CODE.get(a, a) for a in AREAS))).json()
            for e in js["multiAreaDailyAggregates"]:
                av = e.get("averagePerArea") or {}
                rec = daily.setdefault(dt.date.fromisoformat(e["deliveryStart"][:10]), {})
                for a in AREAS:
                    if av.get(NP_CODE.get(a, a)) is not None:
                        rec[a] = av[NP_CODE.get(a, a)]
        except Exception as e:  # noqa: BLE001
            gaps.append(f"Nord Pool {y}: {type(e).__name__}")
    return daily


def np_mean(daily, a, d0, d1):
    days = [d0 + dt.timedelta(days=i) for i in range((d1 - d0).days + 1)]
    vals = [(daily.get(d) or {}).get(a) for d in days]
    if not days or any(v is None for v in vals):
        return None
    h = [hours(d) for d in days]
    return sum(v * w for v, w in zip(vals, h)) / sum(h)


# ---------------------------------------------------------------- ENTSO-E monthly (2015-2023)
def entsoe_months(prev):
    """{area: {YYYY-MM: EUR/MWh}} time-weighted monthly A44 means; reuses the previous file's months."""
    have = {}
    if prev and prev.get("months"):
        for a, vals in (prev.get("monthly") or {}).items():
            have[a] = {k: v for k, v in zip(prev["months"], vals) if v is not None and k < f"{NP_FIRST}-01"}
    if not os.environ.get("ENTSOE_API_TOKEN"):
        if not any(have.values()):
            gaps.append("ENTSO-E token not set: zone history before 2024 missing")
        return have
    import build_nordic_monthly as nm
    for a in AREAS:
        if a == "SYS":
            continue
        z = have.setdefault(a, {})
        for y in range(EN_FIRST, NP_FIRST):
            if sum(1 for k in z if k.startswith(f"{y}-")) == 12 or (a == "DE-LU" and y < 2019):
                continue
            try:
                for k, v in nm.price_months(a, dt.date(y, 1, 1), dt.date(y + 1, 1, 1)).items():
                    if k.startswith(f"{y}-"):
                        z[k] = r2(v)
            except Exception as e:  # noqa: BLE001 (client redacts the token)
                gaps.append(f"ENTSO-E A44 {a} {y}: {type(e).__name__}")
    return have


# ---------------------------------------------------------------- EEX
def eex():
    raw = bp.get(bp.EEX_CSV).content
    df = pd.read_csv(io.BytesIO(raw), dtype={"tradeDate": str, "delivery": str}, low_memory=False)
    df = df[(df.status == "ok") & df.settlementPrice.notna()]
    return df.drop_duplicates(["marketGroup", "area", "product", "delivery", "tradeDate"], keep="last")


def curve(pw, area, td):
    x = pw[(pw.area == area) & (pw.tradeDate == td)]
    return {r.delivery: float(r.settlementPrice) for r in x.itertuples()}


def month_value(a, y, m, asof, daily, monthly, cv):
    """(value, kind) for month y-m seen on date asof: A = actual, B = actual-to-date + future, F = future."""
    yy = y % 100
    me = month_end(y, m)
    fut = cv.get(f"{MON[m-1]}-{yy:02d}")
    if fut is None:
        fut = cv.get(f"Q{(m-1)//3+1}-{yy:02d}")
    if fut is None:
        fut = cv.get(f"Cal-{yy:02d}")
    if me <= asof:
        return monthly(a, y, m), "A"
    d0 = dt.date(y, m, 1)
    if d0 <= asof:
        act = [(daily.get(d0 + dt.timedelta(days=i)) or {}).get(a) for i in range((asof - d0).days + 1)]
        if fut is None or any(v is None for v in act):
            return None, "B"
        days = [d0 + dt.timedelta(days=i) for i in range((me - d0).days + 1)]
        h = [hours(d) for d in days]
        k = len(act)
        return (sum(v * w for v, w in zip(act, h[:k])) + fut * sum(h[k:])) / sum(h), "B"
    return fut, "F"


def panel(a, y, asof, daily, monthly, cv):
    qs, kinds = [], []
    for q in range(4):
        vs = [month_value(a, y, 3 * q + k + 1, asof, daily, monthly, cv) for k in range(3)]
        ks = {k for _, k in vs}
        qs.append(sum(v for v, _ in vs) / 3 if all(v is not None for v, _ in vs) else None)
        kinds.append("A" if ks == {"A"} else "F")
    ny = (y + 1) % 100
    nq = [cv.get(f"Q{i}-{ny:02d}") for i in range(1, 5)]
    nq = [v if v is not None else cv.get(f"Cal-{ny:02d}") for v in nq]
    fy = sum(qs) / 4 if all(v is not None for v in qs) else None
    nfy = sum(nq) / 4 if all(v is not None for v in nq) else None
    return dict(fy=fy, q=qs, qk=kinds, ny=nfy, nq=nq, ny2=cv.get(f"Cal-{(y + 2) % 100:02d}"))


# ---------------------------------------------------------------- Ember / weather
def ember(prev, today):
    old = (prev or {}).get("ember")
    if old and old.get("fetched") and (today - dt.date.fromisoformat(old["fetched"])).days < 7 and "--force" not in sys.argv:
        return old
    try:
        raw = bp.get(EMBER_CSV).content
        d = pd.read_csv(io.BytesIO(raw), low_memory=False)
    except Exception as e:  # noqa: BLE001
        gaps.append(f"Ember: {type(e).__name__}")
        return old
    d = d[d.Area.isin(EMBER_C.values()) & d["Electricity source"].isin(EMBER_SRC.values())]
    d["m"] = d.Date.astype(str).str[:7]
    d = d[d.m >= "2015-01"]
    piv = d.pivot_table(index="m", columns=["Area", "Electricity source"], values="Generation (TWh)", aggfunc="sum")
    months = sorted(piv.index)
    out = dict(fetched=today.isoformat(), months=months, areas={})
    for code, name in list(EMBER_C.items()) + [("Nordic", None)]:
        rec = {}
        for k, src in EMBER_SRC.items():
            vals = []
            for m in months:
                if name:
                    v = piv.get((name, src), pd.Series(dtype=float)).get(m)
                    v = None if v is None or pd.isna(v) else float(v)
                else:   # Nordic = NO+SE+FI+DK when all four report demand that month; a missing source counts 0
                    dem = [piv.get((n, "Demand"), pd.Series(dtype=float)).get(m) for n in EMBER_C.values()]
                    if any(x is None or pd.isna(x) for x in dem):
                        v = None
                    else:
                        xs = [piv.get((n, src), pd.Series(dtype=float)).get(m) for n in EMBER_C.values()]
                        v = sum(0 if x is None or pd.isna(x) else float(x) for x in xs)
                if v is not None and k == "netimp":
                    v = -v
                vals.append(r2(v, 3))
            rec["netexp" if k == "netimp" else k] = vals
        out["areas"][code] = rec
    return out


def weather(prev):
    try:
        h = pd.read_csv(io.BytesIO(bp.get(WX_HIST).content))
        try:
            f = pd.read_csv(io.BytesIO(bp.get(WX_FEED).content))
            h = pd.concat([h, f])
        except Exception as e:  # noqa: BLE001
            gaps.append(f"weather feed: {type(e).__name__}")
        h["k"] = h.year.astype(int).astype(str) + "-" + h.month.astype(int).map("{:02d}".format)
        h = h.drop_duplicates(["k", "variable"], keep="last")
        months = sorted(h.k.unique())
        out = dict(months=months)
        for v in ("wind", "rain", "temp"):
            s = h[h.variable.str.strip().str.lower() == v].set_index("k").value
            out[v] = [r2(s.get(m), 3) for m in months]
        return out
    except Exception as e:  # noqa: BLE001
        gaps.append(f"weather: {type(e).__name__}")
        return (prev or {}).get("weather")


# ---------------------------------------------------------------- main
def main():
    now = dt.datetime.now(TZ)
    today = now.date()
    prev = None
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT))
        except Exception:  # noqa: BLE001
            prev = None
    daily = nordpool(today, prev)
    if not daily:
        raise SystemExit("no Nord Pool data")
    asof = max(d for d, r in daily.items() if r.get("SYS") is not None)

    def np_month(a, y, m):
        return np_mean(daily, a, dt.date(y, m, 1), month_end(y, m))

    en = entsoe_months(prev)
    months = mkeys(dt.date(EN_FIRST, 1, 1), asof)
    monthly = {}
    for a in AREAS:
        vals = []
        for k in months:
            y, m = int(k[:4]), int(k[5:])
            if y >= NP_FIRST:
                v = np_month(a, y, m) if month_end(y, m) <= asof else np_mean(daily, a, dt.date(y, m, 1), asof)
            else:
                v = (en.get(a) or {}).get(k)
            vals.append(r2(v))
        monthly[a] = vals
    mi = {k: i for i, k in enumerate(months)}

    def mval(a, y, m):
        i = mi.get(f"{y}-{m:02d}")
        return None if i is None else monthly[a][i]

    def mval_full(a, y, m):   # complete months only
        return mval(a, y, m) if month_end(y, m) <= asof else None

    # yearly averages (hour-weighted)
    yearly = {}
    for a in AREAS:
        ya = {}
        if a == "SYS":
            ya.update({str(k): v for k, v in SYS_YEARLY.items()})
        for y in range(EN_FIRST, asof.year):
            if a == "SYS" and y < NP_FIRST:
                continue
            if y >= NP_FIRST:
                v = np_mean(daily, a, dt.date(y, 1, 1), dt.date(y, 12, 31))
            else:
                ms = [mval(a, y, m) for m in range(1, 13)]
                hs = [sum(hours(dt.date(y, m, 1) + dt.timedelta(days=i)) for i in range(month_end(y, m).day)) for m in range(1, 13)]
                v = sum(x * h for x, h in zip(ms, hs)) / sum(hs) if all(x is not None for x in ms) else None
            if v is not None:
                ya[str(y)] = r2(v)
        yearly[a] = ya

    # futures
    df = eex()
    pw = df[(df.marketGroup == "POWER") & (df["product"] == "Base")]
    tds = sorted(pw.tradeDate.unique())
    td = tds[-1]
    tdd = dt.date.fromisoformat(td)
    td_prev = max((t for t in tds if dt.date.fromisoformat(t) <= tdd - dt.timedelta(days=28)), default=None)
    Y = asof.year
    fut, cmp_ = {}, {}
    for a in AREAS:
        cv = curve(pw, FUT.get(a, a), td)
        if not cv:
            continue
        p = panel(a, Y, asof, daily, mval_full, cv)
        fut[a] = {k: ([r2(x) for x in v] if isinstance(v, list) and k != "qk" else (r2(v) if isinstance(v, float) else v)) for k, v in p.items()}
        if td_prev:
            cvp = curve(pw, FUT.get(a, a), td_prev)
            pp = panel(a, Y, dt.date.fromisoformat(td_prev), daily, mval_full, cvp)
            cmp_[a] = dict(prev=[r2(pp["fy"])] + [r2(x) for x in pp["q"]] + [r2(pp["ny"])] + [r2(x) for x in pp["nq"]])
        cmp_.setdefault(a, {})["cur"] = [fut[a]["fy"]] + fut[a]["q"] + [fut[a]["ny"]] + fut[a]["nq"]

    # commodities: monthly averages of daily settlements (EEX history starts 1 Sep 2026) + DE-LU / SYS spot
    com = {"months": [], "gas": [], "coal": [], "co2": [], "de": [], "sys": [], "contracts": {}}
    # One fixed contract per commodity across all months, chosen on the latest trade date: the next TTF winter not yet
    # in delivery and the nearest December coal / EUA contract. Rolling the contract by month would compare different
    # delivery periods (e.g. Winter-26 in September with Winter-27 in October). Months without trades stay null.
    first = dt.date.fromisoformat(min(df.tradeDate))
    dec = f"Dec-{tdd.year % 100:02d}" if tdd.month < 12 else f"Dec-{(tdd.year + 1) % 100:02d}"
    win = f"Gas Winter-{(tdd.year + 1 if tdd.month >= 10 else tdd.year) % 100:02d}"
    com["contract"] = dict(coal="API2 " + dec, co2="EUA " + dec, gas="TTF " + win.replace("Gas ", ""))
    for k in mkeys(first.replace(day=1), asof):
        y, m = int(k[:4]), int(k[5:])
        sel = df[df.tradeDate.str.startswith(k)]
        g = sel[(sel.marketGroup == "GAS_FUTURES") & (sel.delivery == win) & (sel.settlementPrice > 0)].settlementPrice
        c = sel[(sel.marketGroup == "ICE_COAL_FUTURES") & (sel.delivery == "Coal " + dec)].settlementPrice
        e = sel[(sel.marketGroup == "EUA_FUTURES") & (sel.delivery == "EUA " + dec)].settlementPrice
        com["months"].append(k)
        com["gas"].append(r2(g.mean()) if len(g) else None)
        com["coal"].append(r2(c.mean()) if len(c) else None)
        com["co2"].append(r2(e.mean()) if len(e) else None)
        com["de"].append(mval("DE-LU", y, m))
        com["sys"].append(mval("SYS", y, m))
        com["contracts"][k] = dict(coal="API2 " + dec, co2="EUA " + dec, gas="TTF " + win.replace("Gas ", ""))
    com["mtd_to"] = asof.isoformat()
    com["last_td"] = td

    # daily arrays from 1 Jan 2024
    d0 = dt.date(NP_FIRST, 1, 1)
    n = (asof - d0).days + 1
    dly = {a: [r2((daily.get(d0 + dt.timedelta(days=i)) or {}).get(a)) for i in range(n)] for a in AREAS}

    out = dict(generated_utc=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), asof=asof.isoformat(),
               year=Y, eex_td=td, eex_td_prev=td_prev, areas=AREAS,
               daily=dict(start=d0.isoformat(), v=dly), months=months, monthly=monthly, np_first=f"{NP_FIRST}-01",
               yearly=yearly, hist_avg={"label": "’11–’19", "SYS": r2(sum(SYS_YEARLY[y] for y in range(2011, 2020)) / 9)},
               fut=fut, cmp=cmp_, com=com, ember=ember(prev, today), weather=weather(prev),
               src=dict(np="https://data.nordpoolgroup.com", entsoe="https://transparency.entsoe.eu",
                        eex="https://www.eex.com/en/market-data", ember="https://ember-energy.org/data/",
                        era5="https://cds.climate.copernicus.eu"),
               gaps=gaps)
    write_json(OUT, out)
    log(f"asof {asof}, EEX {td} (prev {td_prev}), {len(months)} months, {os.path.getsize(OUT) // 1024} KB, gaps {len(gaps)}")


if __name__ == "__main__":
    main()

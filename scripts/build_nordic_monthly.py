"""Nordic monthly overview: what drove Nordic power last month. Monthly; skips if data/nordic_monthly.json is < 20 days
old and already covers the last complete month (use --force to rebuild).
Sources (official, ENTSO-E needs ENTSOE_API_TOKEN, never printed):
  * ENTSO-E Transparency A75 actual generation per production type, countries NO, SE, FI, DK (monthly TWh, CET months)
  * ENTSO-E Transparency A44 day-ahead prices, bidding zones (monthly time-weighted mean, EUR/MWh)
  * Hydro energy inflow (derived): change in reservoir energy (NVE magasinstatistikk for Norway, ENTSO-E A72 for Sweden)
    plus hydro generation (A75) in the month. Normal = average of the same month over AVG_YEARS.
Writes data/nordic_monthly.json (~15 KB)."""
import datetime as dt, json, os, sys, time
import pandas as pd
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from common import age_days, write_json  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_power as bp            # HTTP + ENTSO-E helpers (token redacted in all messages)
import build_hydro_nordic as hn     # NVE + A72 weekly reservoir series

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "nordic_monthly.json")
CTRY = {"NO": "10YNO-0--------C", "SE": "10YSE-1--------K", "FI": "10YFI-1--------U", "DK": "10Y1001A1001A65H"}
TYPES = ["hydro", "nuclear", "wind", "solar", "thermal"]           # thermal = gas, coal, oil, biomass, waste, other
PZ = ["NO1", "NO2", "SE3", "SE4", "FI", "DK1"]
Y0 = 2021                                                       # first year fetched; 5-year normal 2021-2025
CET = bp.CET


def prev_month(d):
    f = d.replace(day=1) - dt.timedelta(days=1)
    return f.replace(day=1)


def mkey(t):
    return t.tz_convert(CET).strftime("%Y-%m")


def raw_periods(params):
    root = bp._strip_ns(bp.entsoe_raw(params).text)
    bp._ack(root)
    for ts in root.iter("TimeSeries"):
        is_in = ts.find("inBiddingZone_Domain.mRID") is not None or ts.find("outBiddingZone_Domain.mRID") is None
        psr = ts.findtext("MktPSRType/psrType")
        for step, s in bp._periods(ts):
            yield psr, is_in, step, s


def gen_year(eic, y, end):
    """{month: {type: TWh}} for calendar year y (CET), up to `end` (date, exclusive)."""
    a, b = dt.date(y, 1, 1), min(dt.date(y + 1, 1, 1), end)
    out = {}
    for psr, is_in, step, s in raw_periods(dict(documentType="A75", processType="A16", in_Domain=eic, **bp._period(a, b))):
        if not is_in:
            continue
        k = bp.PSR.get(psr, "other")
        k = k if k in TYPES else "thermal"
        e = (s.fillna(0) * step / 60.0).groupby(s.index.tz_convert(CET).strftime("%Y-%m")).sum()   # MWh
        for m, v in e.items():
            out.setdefault(m, {}).setdefault(k, 0.0)
            out[m][k] += v / 1e6
    return out


def price_months(zone, a, b):
    num, den = {}, {}
    for _, _, step, s in raw_periods(dict(documentType="A44", in_Domain=bp.ENTSOE_EIC[zone], out_Domain=bp.ENTSOE_EIC[zone],
                                           **bp._period(a, b))):
        s = s.dropna()
        g = s.index.tz_convert(CET).strftime("%Y-%m")
        for m, v in (s * step).groupby(g).sum().items():
            num[m] = num.get(m, 0) + v
        for m, v in pd.Series(step, index=s.index).groupby(g).sum().items():
            den[m] = den.get(m, 0) + v
    return {m: num[m] / den[m] for m in num if den.get(m)}


def storage_monthend(wk):
    """{(iso_year, iso_week): TWh} -> {YYYY-MM: TWh at month end}, linear between week-end (Sunday) readings."""
    pts = sorted((dt.date.fromisocalendar(y, w, 7), v) for (y, w), v in wk.items() if v is not None)
    if len(pts) < 10:
        return {}
    s = pd.Series([v for _, v in pts], index=pd.to_datetime([d for d, _ in pts]))
    s = s[~s.index.duplicated()]
    daily = s.resample("D").mean().interpolate(limit=14)
    me = daily[daily.index.is_month_end].dropna()
    return {t.strftime("%Y-%m"): float(v) for t, v in me.items()}


def r2(v):
    return None if v is None else round(v, 2)


def main():
    today = dt.date.today()
    last = prev_month(today)                         # last complete month (first day)
    lm = last.strftime("%Y-%m")
    if os.path.exists(OUT) and "--force" not in sys.argv:
        try:
            old = json.load(open(OUT))
            if old.get("last") == lm and (age_days(OUT) or 99) < 20:
                print("nordic_monthly.json fresh; monthly refresh skipped"); return
        except Exception:
            pass
    if not bp._token():
        print("nordic_monthly: ENTSOE_API_TOKEN not set; skipped"); return
    t0 = time.time(); end = today.replace(day=1); gaps = []
    months = pd.period_range(f"{Y0}-01", lm, freq="M").strftime("%Y-%m").tolist()
    gen = {}
    for c, eic in CTRY.items():
        acc = {}
        for y in range(Y0, last.year + 1):
            try:
                acc.update(gen_year(eic, y, end))
            except Exception as e:  # noqa: BLE001
                gaps.append(f"A75 {c} {y}: {bp._redact(e)[:80]}"); print(gaps[-1])
        gen[c] = {k: [r2(acc.get(m, {}).get(k)) if m in acc else None for m in months] for k in TYPES}
    gen["Nordic"] = {k: [r2(sum(gen[c][k][i] or 0 for c in CTRY)) if all(m_ok(gen[c], i) for c in CTRY) else None
                         for i in range(len(months))] for k in TYPES}
    # prices: last 25 months
    pa = (pd.Period(lm, "M") - 24).start_time.date()
    prices = {}
    for z in PZ:
        try:
            p = {}
            a = pa
            while a < end:
                b = min(dt.date(a.year + 1, a.month, 1), end)
                p.update(price_months(z, a, b)); a = b
            prices[z] = [r2(p.get(m)) for m in months[-25:]]
        except Exception as e:  # noqa: BLE001
            gaps.append(f"A44 {z}: {bp._redact(e)[:80]}"); print(gaps[-1])
    # inflow (derived) NO, SE
    infl = {}
    try:
        wk_no, _, _ = hn.series_no()
        stor = {"NO": storage_monthend(wk_no)}
        se = hn.series_se(today)
        if se:
            stor["SE"] = storage_monthend(se)
        for c, st in stor.items():
            v = []
            for i, m in enumerate(months):
                pm = (pd.Period(m, "M") - 1).strftime("%Y-%m")
                h = gen[c]["hydro"][i]
                v.append(r2(st[m] - st[pm] + h) if m in st and pm in st and h is not None else None)
            infl[c] = v
        if "NO" in infl and "SE" in infl:
            infl["NOSE"] = [r2(a + b) if a is not None and b is not None else None for a, b in zip(infl["NO"], infl["SE"])]
    except Exception as e:  # noqa: BLE001
        gaps.append(f"inflow: {bp._redact(e)[:80]}"); print(gaps[-1])
    out = dict(generated_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), last=lm, months=months,
               types=TYPES, avg_years=[Y0, Y0 + 4], gen=gen, price_zones=PZ, price_months=months[-25:], prices=prices,
               inflow=infl, gaps=gaps, build_s=round(time.time() - t0),
               src=dict(gen="https://transparency.entsoe.eu/generation/r2/actualGenerationPerProductionType/show",
                        price="https://transparency.entsoe.eu/transmission-domain/r2/dayAheadPrices/show",
                        nve="https://www.nve.no/energi/analyser-og-statistikk/magasinstatistikk/",
                        a72="https://transparency.entsoe.eu/generation/r2/waterReservoirsAndHydroStoragePlants/show"))
    if not any(any(v is not None for v in gen["NO"][k]) for k in TYPES):
        print("nordic_monthly: no generation data; keeping previous file"); return
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"nordic_monthly: {len(months)} months to {lm}, {os.path.getsize(OUT)} B, {out['build_s']} s, gaps {len(gaps)}")


def m_ok(g, i):
    return any(g[k][i] is not None for k in TYPES)


if __name__ == "__main__":
    main()

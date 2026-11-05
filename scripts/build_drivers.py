"""Nordic Power tab: daily "What moved alongside Nordic prices" panel. No network: reads files this build already wrote.
Inputs: data/mr.json (Nord Pool daily day-ahead averages, system price and zones), data/nordic_dash.json (ENTSO-E hourly:
A65 actual load, A69 day-ahead wind forecast, A75 actual generation; Open-Meteo daily temperature), data/power.json
(Nord Pool UMM nuclear availability, NVE / ENTSO-E A72 hydro balance vs normal, EEX gas and carbon settlements).
Every figure is a measured or published value with its own date. Daily values from hourly data are means of the hours in
the CET delivery day; a day missing more than one hour is left out (never filled). Reference = the 28 days before the
value's date (four whole weeks, so weekdays and weekends are balanced), averaged over the complete days found; the number
of days used is stored. The browser only formats these numbers. Writes data/nordic_drivers.json."""
import datetime as dt, json, os, sys
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import write_json  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "nordic_drivers.json")
CET = ZoneInfo("Europe/Stockholm")
ZC = {"SE": ["SE1", "SE2", "SE3", "SE4"], "NO": ["NO1", "NO2", "NO3", "NO4", "NO5"], "FI": ["FI"], "DK": ["DK1", "DK2"]}
ALLZ = [z for c in ("SE", "NO", "FI", "DK") for z in ZC[c]]
CTRY = {z: c for c, zs in ZC.items() for z in zs}
REF = 28          # reference window in days
MIN_REF = 20      # fewer complete days than this: no reference
D = dt.date.fromisoformat


def load(name):
    try:
        return json.load(open(os.path.join(ROOT, "data", name)))
    except Exception:  # noqa: BLE001
        return None


def rnd(v, n=2):
    return None if v is None else round(v, n)


def mean(a):
    a = [x for x in a if x is not None]
    return sum(a) / len(a) if a else None


def ref_avg(series, day):
    """series: {iso date: value}; mean of the REF days before `day` that have a value -> (avg, n days)."""
    vals = [series.get((day - dt.timedelta(days=i)).isoformat()) for i in range(1, REF + 1)]
    vals = [v for v in vals if v is not None]
    return (mean(vals), len(vals)) if len(vals) >= MIN_REF else (None, len(vals))


# ---------------------------------------------------------------- hourly dashboard -> daily means per CET day
def dash_daily(dash):
    sc = dash["enc"]["scale"]

    def dec(a, s):
        out, acc = [], 0
        for e in a:
            if e is None:
                out.append(None)
            else:
                acc += e
                out.append(acc / s)
        return out
    t0 = dt.datetime.fromisoformat(dash["t0"].replace("Z", "+00:00"))
    days = [(t0 + dt.timedelta(hours=i)).astimezone(CET).date().isoformat() for i in range(dash["n"])]
    nh = {}
    for d in days:
        nh[d] = nh.get(d, 0) + 1
    # hours in each CET day (23/24/25); the first and last day of the file may be cut, so count from the calendar
    def hours_in(d):
        a = dt.datetime.combine(D(d), dt.time(), CET)
        b = dt.datetime.combine(D(d) + dt.timedelta(days=1), dt.time(), CET)
        return round((b - a).total_seconds() / 3600)

    def daily(vals):
        acc = {}
        for d, v in zip(days, vals):
            if v is not None:
                acc.setdefault(d, []).append(v)
        return {d: sum(a) / len(a) for d, a in acc.items() if len(a) >= hours_in(d) - 1}
    Z = {}
    for z, zz in dash["zones"].items():
        Z[z] = {k: daily(dec(zz[k], sc[k])) for k in ("l", "wf") if k in zz}
        Z[z]["g"] = {k: daily(dec(v, sc["g"])) for k, v in (zz.get("g") or {}).items()}
    return Z


def sum_zones(Z, zs, get):
    """daily sum over zones; a day counts only when every zone that has the series has a value."""
    ser = [get(Z[z]) for z in zs if z in Z]
    ser = [s for s in ser if s is not None]
    if not ser:
        return None
    common = set(ser[0])
    for s in ser[1:]:
        common &= set(s)
    return {d: sum(s[d] for s in ser) for d in common}


def sum_latest(Z, zs, get, upto):
    """like sum_zones, but when some zones lag (e.g. a late ENTSO-E upload) the total uses the zones that have the latest
    day, and names the ones left out, instead of falling back to an old day for everyone."""
    have = {z: get(Z[z]) for z in zs if z in Z and get(Z[z])}
    if not have:
        return None, []
    last = {z: last_day(s, upto) for z, s in have.items()}
    L = max((d for d in last.values() if d), default=None)
    if not L:
        return None, []
    inc = [z for z in have if last[z] == L]
    return sum_zones({z: Z[z] for z in inc}, inc, get), [z for z in zs if z not in inc]


def daily_line(series, day, kind, unit="MW"):
    """value on `day`, the day before and the reference mean; None when the day has no value."""
    if not series or day not in series:
        return None
    dd = D(day)
    prev = series.get((dd - dt.timedelta(days=1)).isoformat())
    avg, n = ref_avg(series, dd)
    return dict(date=day, v=rnd(series[day], 1), prev=rnd(prev, 1), avg=rnd(avg, 1), n=n, kind=kind, unit=unit)


def last_day(series, upto):
    ds = [d for d in (series or {}) if d <= upto]
    return max(ds) if ds else None


def main():
    gaps = []
    mr, dash, P = load("mr.json"), load("nordic_dash.json"), load("power.json")
    today = dt.datetime.now(CET).date().isoformat()
    out = dict(generated_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), today=today, ref_days=REF,
               prices=None, areas={}, fuels={}, src={}, gaps=gaps)
    # ---- day-ahead prices (Nord Pool daily averages)
    day = None
    if mr and mr.get("daily"):
        st = D(mr["daily"]["start"])
        V = mr["daily"]["v"]
        ser = {k: {(st + dt.timedelta(days=i)).isoformat(): x for i, x in enumerate(a) if x is not None} for k, a in V.items()}
        day = max(ser.get("SYS") or {"": 0}) or None
        if day:
            pr = {}
            for k in ["SYS"] + ALLZ:
                s = ser.get(k) or {}
                if day not in s:
                    continue
                avg, n = ref_avg(s, D(day))
                pv = s.get((D(day) - dt.timedelta(days=1)).isoformat())
                spr = {d: s[d] - ser["SYS"][d] for d in s if d in ser["SYS"]} if k != "SYS" else None
                savg, _ = ref_avg(spr, D(day)) if spr else (None, 0)
                pr[k] = dict(v=s[day], prev=pv, avg=rnd(avg), n=n,
                             spr=rnd(spr.get(day)) if spr else None, spr_prev=rnd(spr.get((D(day) - dt.timedelta(days=1)).isoformat())) if spr else None,
                             spr_avg=rnd(savg) if spr else None)
            out["prices"] = dict(date=day, prev_date=(D(day) - dt.timedelta(days=1)).isoformat(), zones=pr,
                                 src="Nord Pool day-ahead, daily average (" + mr.get("src", {}).get("np", "") + ")")
    else:
        gaps.append("mr.json: no daily prices")
    if not day:
        write_json(OUT, out)
        print("drivers: no price day")
        return
    # ---- hourly-derived daily series
    Z = dash_daily(dash) if dash else {}
    if not dash:
        gaps.append("nordic_dash.json missing")
    temp = {}
    if dash and dash.get("temp"):
        for c, a in dash["temp"].items():
            temp[c] = {d: v for d, v in zip(dash["days"], a) if v is not None}
    nuc = (P or {}).get("nuclear") or {}
    hb = (P or {}).get("hydro_bal") or {}
    areas = ["NOR"] + ALLZ
    for a in areas:
        zs = ALLZ if a == "NOR" else [a]
        c = None if a == "NOR" else CTRY[a]
        A = {}
        # wind: day-ahead forecast for the price day (A69), reference = the forecasts for the 28 days before
        w = sum_zones(Z, zs, lambda x: x.get("wf"))
        if w and any(v > 0.5 for v in w.values()):
            A["wind"] = daily_line(w, day, "forecast")
            if A["wind"] is None:
                ld = last_day(w, day)
                A["wind"] = daily_line(w, ld, "forecast") if ld else None
            # actual wind on the latest complete day, for context
            wa = sum_zones(Z, zs, lambda x: (x.get("g") or {}).get("wind"))
            la = last_day(wa, day) if wa else None
            if la:
                A["wind_act"] = daily_line(wa, la, "actual")
        # demand: latest complete day of actual load (A65); no load forecast in the build
        ld_, ex = sum_latest(Z, zs, lambda x: x.get("l"), day)
        la = last_day(ld_, day) if ld_ else None
        if la:
            A["load"] = daily_line(ld_, la, "actual")
            if ex:
                A["load"]["excl"] = ex
        # temperature: country mean (Nordic = mean of the four countries), Open-Meteo; forecast on/after today
        tc = ["NO", "SE", "FI", "DK"] if a == "NOR" else [c]
        tser = {}
        for d in set().union(*[set(temp.get(x, {})) for x in tc]) if temp else []:
            vs = [temp[x].get(d) for x in tc if x in temp]
            if all(v is not None for v in vs) and vs:
                tser[d] = sum(vs) / len(vs)
        if tser:
            td = day if day in tser else last_day(tser, day)
            if td:
                A["temp"] = daily_line(tser, td, "forecast" if td >= today else "observed", "°C")
                A["temp"]["scope"] = "Nordic (NO, SE, FI, DK mean)" if a == "NOR" else c
        # nuclear: UMM available capacity for the price day vs installed; reference = actual output, 28 days before
        sc = ["SE", "FI"] if a == "NOR" or c in ("NO", "DK") else ([c] if c in ("SE", "FI") else [])
        if sc and nuc.get("cap"):
            cap = sum(nuc["cap"].get(x) or 0 for x in sc)
            row = next((r for r in nuc.get("days") or [] if r[0] == day), None)
            av, adate, akind = None, None, None
            if row:
                av = sum(row[1 + ["SE", "FI"].index(x)] for x in sc)
                adate, akind = day, "daily mean of UMM availability"
            elif nuc.get("now"):
                av = sum(nuc["now"].get(x) or 0 for x in sc)
                adate, akind = nuc.get("asof", "")[:10], "at build time"
            nz = [z for z in (ALLZ if a == "NOR" else ZC.get(c, [])) if z in Z and (Z[z].get("g") or {}).get("nuclear")]
            if a != "NOR" and c in ("NO", "DK"):
                nz = ["SE3", "FI"]
            out_s = sum_zones(Z, nz, lambda x: (x.get("g") or {}).get("nuclear")) if nz else None
            avg, n = ref_avg(out_s, D(day)) if out_s else (None, 0)
            if av is not None and cap:
                A["nuc"] = dict(date=adate, avail=rnd(av, 0), cap=rnd(cap, 0), pct=rnd(av / cap * 100, 1), basis=akind,
                                out_avg=rnd(avg, 0), n=n, scope="+".join(sc), asof=nuc.get("asof"))
        # hydro balance (weekly): country, or Nordic total for the Nordic view and DK
        hk = c if c in ("SE", "NO", "FI") else None
        H = (hb.get("areas") or {}).get(hk) if hk else hb.get("nordic")
        if H and H.get("twh") is not None:
            A["hydro"] = dict(week=H.get("w"), year=H.get("y"), date=H.get("date"), twh=H.get("twh"), norm=H.get("norm"), dev=H.get("dev"),
                              pct=H.get("pct"), years=H.get("nyears"),
                              scope=hk or "Nordic (NO + SE + FI)", src=H.get("src") or hb.get("source"))
        out["areas"][a] = {k: v for k, v in A.items() if v}
    # ---- gas and carbon settlements (EEX), latest on or before the price day
    for x in ((P or {}).get("eex") or {}).get("drivers") or []:
        if x.get("key") not in ("ttf_da", "eua"):
            continue
        s = {d: v for d, v in zip(x.get("dates") or [], x.get("hist") or []) if v is not None}
        ld = last_day(s, day)
        if not ld:
            continue
        prev_d = max((d for d in s if d < ld), default=None)
        win = [s[d] for d in s if D(ld) - dt.timedelta(days=REF) <= D(d) < D(ld)]
        avg, n = (mean(win), len(win)) if len(win) >= 10 else (None, len(win))
        out["fuels"]["gas" if x["key"] == "ttf_da" else "co2"] = dict(name=x["name"], unit=x["unit"], date=ld, v=s[ld], prev=s.get(prev_d) if prev_d else None,
                                                                       prev_date=prev_d, avg=rnd(avg, 3), n=n)
    out["src"] = dict(prices="Nord Pool day-ahead", dash="ENTSO-E Transparency (A65 load, A69 wind forecast, A75 generation); Open-Meteo temperature",
                      nuclear=nuc.get("source"), hydro=hb.get("source"), fuels="EEX settlements")
    write_json(OUT, out)
    print(f"drivers: price day {day}, {len(out['areas'])} areas, fuels {list(out['fuels'])}, gaps {gaps}")


if __name__ == "__main__":
    main()

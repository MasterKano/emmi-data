"""Nordic hydro reservoirs (Norway + Sweden): weekly stored energy for the current and previous year vs the
min / median / max of the same ISO week over the previous 10 years, plus Norwegian price-area filling for the map.
Sources: NVE magasinstatistikk (biapi.nve.no, keyless) for Norway; ENTSO-E Transparency A72 (aggregated filling
of water reservoirs, country Sweden) for Sweden (needs ENTSOE_API_TOKEN; Norway-only if missing).
Writes data/hydro_nordic.json (a few KB). Weekly: skips if the file is < 6.5 days old (both sources publish weekly)."""
import datetime as dt, json, os, statistics, sys, time
import pandas as pd
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from common import is_fresh, write_json  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_power as bp  # reuse HTTP + ENTSO-E helpers (token is never printed)

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "hydro_nordic.json")
NVE = "https://biapi.nve.no/magasinstatistikk/api/Magasinstatistikk/"
SE_EIC = "10YSE-1--------K"
NY = 10


def r1(v):
    return None if v is None else round(v, 1)


def series_no():
    a = bp.get(NVE + "HentOffentligData").json()
    wk = {(x["iso_aar"], x["iso_uke"]): x["fylling_TWh"] for x in a if x["omrType"] == "NO" and x["omrnr"] == 0}
    cap = max((x for x in a if x["omrType"] == "NO" and x["omrnr"] == 0), key=lambda x: x["dato_Id"])["kapasitet_TWh"]
    zones = {}
    mm = bp.get(NVE + "HentOffentligDataMinMaxMedian").json()
    for x in bp.get(NVE + "HentOffentligDataSisteUke").json():
        if x["omrType"] == "EL":
            med = next((m["medianFyllingsGrad"] for m in mm if m["omrType"] == "EL" and m["omrnr"] == x["omrnr"] and m["iso_uke"] == x["iso_uke"]), None)
            zones[f"NO{x['omrnr']}"] = dict(fill=r1(x["fyllingsgrad"] * 100), med=r1(med * 100) if med is not None else None,
                                            twh=r1(x["fylling_TWh"]), week=x["iso_uke"], date=x["dato_Id"])
    return wk, cap, zones


def series_se(today):
    if not bp._token():
        return None
    parts, a = [], dt.date(today.year - NY - 1, 1, 1)
    while a <= today:
        b = min(dt.date(a.year + 2, 1, 1), today + dt.timedelta(days=1))
        try:
            parts += [it["s"] for it in bp.entsoe_get(dict(documentType="A72", processType="A16", in_Domain=SE_EIC, **bp._period(a, b)))]
        except LookupError:
            pass
        a = b
    if not parts:
        return None
    s = pd.concat(parts).sort_index()
    s = s[~s.index.duplicated(keep="last")].dropna()
    return {bp._iso(t): v / 1e6 for t, v in s.items()}  # MWh -> TWh


def block(wk, y):
    out = {"cur": [], "prev": [], "band": []}
    for w in range(1, 54):
        c, p = wk.get((y, w)), wk.get((y - 1, w))
        h = [wk[(yy, w)] for yy in range(y - NY, y) if (yy, w) in wk]
        out["cur"].append(r1(c)); out["prev"].append(r1(p))
        out["band"].append([r1(min(h)), r1(statistics.median(h)), r1(max(h))] if len(h) >= NY - 2 else None)
    while out["cur"] and out["cur"][-1] is None:
        out["cur"].pop()
    return out


def main():
    if is_fresh(OUT, 6.5):
        print("hydro_nordic.json fresh; weekly refresh skipped"); return
    t0 = time.time(); today = dt.date.today(); y = today.isocalendar()[0]
    try:
        no, cap, zones = series_no()
    except Exception as e:  # noqa: BLE001
        print("NVE failed; keeping previous file:", str(e)[:120]); return
    se = None
    try:
        se = series_se(today)
    except Exception as e:  # noqa: BLE001
        print("ENTSO-E A72 Sweden failed:", bp._redact(e)[:160])
    out = dict(generated_utc=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), year=y, hist=f"{y - NY}-{y - 1}",
               src=dict(no="NVE magasinstatistikk", no_url="https://www.nve.no/energi/analyser-og-statistikk/magasinstatistikk/",
                        se="ENTSO-E Transparency, aggregated filling rate of water reservoirs (A72)", se_url="https://transparency.entsoe.eu/"),
               no_cap_twh=r1(cap), no=block(no, y), zones=zones)
    if se:
        out["se"] = block(se, y)
        both = {k: v + se[k] for k, v in no.items() if k in se}
        out["nose"] = block(both, y)
    open(OUT, "w").write(json.dumps(out, separators=(",", ":")))
    print(f"hydro_nordic.json {os.path.getsize(OUT)} B in {round(time.time() - t0)} s; SE {'ok' if se else 'missing'}")


if __name__ == "__main__":
    main()

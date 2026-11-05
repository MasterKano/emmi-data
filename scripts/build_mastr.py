"""Germany solar & battery pipeline from the Marktstammdatenregister (MaStR, BNetzA), keyless public JSON
behind the 'Erweiterte Oeffentliche Einheitenuebersicht'. Fetches only utility-scale subsets
(ground-mounted PV and battery storage, net rated power > 1 MW; in operation and in planning) and stores
aggregates only (data/mastr_de.json, a few KB). Raw records are never written. Weekly: skips if the file is < 6.5 days old.
"""
import datetime as dt, json, os, re, sys, time
from collections import defaultdict
import requests
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from common import is_fresh, write_json  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "mastr_de.json")
API = "https://www.marktstammdatenregister.de/MaStR/Einheit/EinheitJson/GetErweiterteOeffentlicheEinheitStromerzeugung"
WEB = "https://www.marktstammdatenregister.de/MaStR/Einheit/Einheiten/ErweiterteOeffentlicheEinheitenuebersicht"
H = {"User-Agent": "Mozilla/5.0 (markets-monitor; public data)"}
PS = 2000
PV = "Energieträger~eq~'2495'~and~Art der Solaranlage~eq~'852'"
BAT = "Energieträger~eq~'2496'~and~Speichertechnologie~eq~'524'"
BIG = "~and~Nettonennleistung der Einheit~gt~'1000'"
ST = {"op": "~and~Betriebs-Status~eq~'35'", "pl": "~and~Betriebs-Status~eq~'31'"}


def get(filt, page, size):
    for k in range(3):
        try:
            r = requests.get(API, params=dict(page=page, pageSize=size, filter=filt), headers=H, timeout=120)
            r.raise_for_status()
            return r.json()
        except Exception:  # noqa: BLE001
            if k == 2:
                raise
            time.sleep(5)


def fetch(filt):
    out, page = [], 1
    while True:
        j = get(filt, page, PS)
        d = j.get("Data") or []
        out += [(x.get("Nettonennleistung") or 0, x.get("Bundesland") or "?", x.get("InbetriebnahmeDatum"), x.get("GeplantesInbetriebsnahmeDatum")) for x in d]
        if len(d) < PS or len(out) >= (j.get("Total") or 0):
            return out, j.get("Total") or len(out)
        page += 1
        time.sleep(0.5)


def ym(s):
    m = re.search(r"-?\d+", s or "")
    return dt.datetime.fromtimestamp(int(m.group()) / 1000, dt.timezone.utc).strftime("%Y-%m") if m else None


def main():
    if is_fresh(OUT, 6.5):
        try:
            g = json.load(open(OUT)).get("generated_utc", "")
            if g and (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.datetime.strptime(g, "%Y-%m-%dT%H:%MZ")).days < 6.5:
                print("mastr_de.json is fresh; weekly refresh skipped"); return
        except Exception:  # noqa: BLE001
            pass
    t0 = time.time()
    now = dt.date.today()
    months = []
    y, m = now.year, now.month
    for _ in range(12):
        months.append(f"{y}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months.reverse()
    res = {}
    try:
        for tech, base in (("pv", PV), ("bat", BAT)):
            t = dict(op_mw=0, op_n=0, pl_mw=0, pl_n=0, new=defaultdict(float), new_n=defaultdict(int), by_state=defaultdict(lambda: [0, 0, 0, 0]))
            for s in ("op", "pl"):
                recs, tot = fetch(base + ST[s] + BIG)
                for kw, bl, ib, plan in recs:
                    mw = kw / 1000
                    t[s + "_mw"] += mw; t[s + "_n"] += 1
                    i = 0 if s == "op" else 2
                    t["by_state"][bl][i] += mw; t["by_state"][bl][i + 1] += 1
                    if s == "op" and ym(ib) in months:
                        t["new"][ym(ib)] += mw; t["new_n"][ym(ib)] += 1
            # all-size unit counts (home batteries etc.), counts only
            t["all_op_n"] = get(base + ST["op"], 1, 1).get("Total")
            t["all_pl_n"] = get(base + ST["pl"], 1, 1).get("Total")
            res[tech] = dict(op_mw=round(t["op_mw"]), op_n=t["op_n"], pl_mw=round(t["pl_mw"]), pl_n=t["pl_n"],
                             all_op_n=t["all_op_n"], all_pl_n=t["all_pl_n"],
                             new=[round(t["new"].get(k, 0), 1) for k in months], new_n=[t["new_n"].get(k, 0) for k in months],
                             by_state={k: [round(v[0]), v[1], round(v[2]), v[3]] for k, v in sorted(t["by_state"].items())})
    except Exception as e:  # noqa: BLE001
        print("MaStR fetch failed; keeping previous file:", str(e)[:120]); return
    out = dict(generated_utc=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), source="Marktstammdatenregister (Bundesnetzagentur)", url=WEB,
               scope="Units with net rated power > 1 MW. Solar: ground-mounted (Freiflächen) only. Battery: storage units with battery technology. Status as registered (In Betrieb / In Planung). MW = net rated power (Nettonennleistung).",
               months=months, **res, build_s=round(time.time() - t0))
    open(OUT, "w").write(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"mastr_de.json {os.path.getsize(OUT)} B in {out['build_s']} s; pv op {res['pv']['op_mw']} MW, bat op {res['bat']['op_mw']} MW")


if __name__ == "__main__":
    main()

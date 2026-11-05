"""US equity valuations, then vs now (monthly). Small aggregates only -> data/us_valuation.json.
Sources (all public, keyless):
  * Robert Shiller, shillerdata.com ie_data.xls: S&P composite price, dividends, earnings, GS10, CAPE, excess CAPE yield (monthly)
  * Aswath Damodaran (NYU Stern) histimpl.xls: yearly S&P 500 earnings yield, dividend yield, dividends + buybacks
  * FRED (St. Louis Fed) fredgraph.csv: CP (corporate profits after tax, IVA/CCAdj), GDP, CNCF (corporate net cash flow, IVA)
Current US 10Y: data/market.json yields10 (US Treasury). Skips when the file is < 25 days old (pass --force)."""
import datetime as dt, io, json, os, re, sys, time
import pandas as pd
import requests
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from common import is_fresh, write_json  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "us_valuation.json")
UA = {"User-Agent": "Mozilla/5.0 (markets-monitor; public data)"}
SHILLER_PAGE = "https://shillerdata.com/"
DAMO = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/histimpl.xls"
DAMO_PAGE = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/implpr.html"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="
T0 = 1990


def r(v, d=2):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v != v else round(v, d)


def get(url, **kw):
    x = requests.get(url, headers=UA, timeout=120, **kw)
    x.raise_for_status()
    return x


def shiller():
    page = get(SHILLER_PAGE).text
    m = re.search(r'(//img1\.wsimg\.com/[^"\']*ie_data\.xls[^"\']*)', page)
    url = "https:" + m.group(1) if m else None
    if not url:
        raise LookupError("ie_data.xls link not found")
    v = pd.read_excel(io.BytesIO(get(url).content), sheet_name="Data", header=None, skiprows=8)
    v = v[pd.to_numeric(v[0], errors="coerce").notna()]
    rows = []
    for _, x in v.iterrows():
        f = float(x[0]); y = int(f); mth = int(round((f - y) * 100))
        if not 1 <= mth <= 12:
            continue
        p, d, e = r(x[1], 4), r(x[2], 4), r(x[3], 4)
        rows.append(dict(m=f"{y}-{mth:02d}", p=p, dy=r(d / p * 100) if d and p else None, ey=r(e / p * 100) if e and p else None,
                         gs10=r(x[6]), cape=r(x[12]), ecy=r(x[16] * 100) if r(x[16], 6) is not None else None))
    allc = [x["cape"] for x in rows if x["cape"] is not None]
    last = next(x for x in reversed(rows) if x["cape"] is not None)
    pct = round(sum(c <= last["cape"] for c in allc) / len(allc) * 100, 1)
    return rows, dict(url=SHILLER_PAGE, file=url.split("?")[0], cape_pct=pct, cape_n=len(allc), cape_from=next(x["m"] for x in rows if x["cape"] is not None))


def damodaran():
    v = pd.read_excel(io.BytesIO(get(DAMO).content), header=None)
    hdr = next(i for i in range(15) if str(v.iloc[i, 0]).strip() == "Year")
    out = []
    for i in range(hdr + 1, len(v)):
        y = pd.to_numeric(v.iloc[i, 0], errors="coerce")
        if y != y:
            break
        lvl, cash = r(v.iloc[i, 3], 4), r(v.iloc[i, 6], 4)
        out.append(dict(y=int(y), ey=r(v.iloc[i, 1] * 100), dy=r(v.iloc[i, 2] * 100),
                        sy=r(cash / lvl * 100) if cash and lvl else None))
    return out


def fred(sid):
    d = pd.read_csv(io.StringIO(get(FRED + sid).text))
    d.columns = ["d", "v"]
    d["v"] = pd.to_numeric(d["v"], errors="coerce")
    return {k: v for k, v in zip(d["d"], d["v"]) if v == v}


def us10():
    try:
        y = json.load(open(os.path.join(ROOT, "data", "market.json")))["yields10"]
        s = y["series"]["US"]
        i = max(i for i, v in enumerate(s) if v is not None)
        return dict(v=s[i], d=y["dates"][i], src=y["src"]["US"])
    except Exception:  # noqa: BLE001
        return None


def main():
    if is_fresh(OUT, 25):
        print("us_valuation.json fresh; monthly refresh skipped"); return
    t0 = time.time(); out = dict(generated_utc=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), gaps=[])
    prev = json.load(open(OUT)) if os.path.exists(OUT) else {}
    try:
        rows, meta = shiller()
        k = [x for x in rows if int(x["m"][:4]) >= T0]
        out["shiller"] = dict(meta, m=[x["m"] for x in k], cape=[x["cape"] for x in k], ecy=[x["ecy"] for x in k],
                              ey=[x["ey"] for x in k], dy=[x["dy"] for x in k], gs10=[x["gs10"] for x in k])
    except Exception as e:  # noqa: BLE001
        print("Shiller failed:", str(e)[:120]); out["gaps"].append("Shiller data not reachable in this build")
        if prev.get("shiller"): out["shiller"] = dict(prev["shiller"], stale=True)
    try:
        dm = [x for x in damodaran() if x["y"] >= T0]
        out["damodaran"] = dict(url=DAMO_PAGE, y=[x["y"] for x in dm], ey=[x["ey"] for x in dm], dy=[x["dy"] for x in dm], sy=[x["sy"] for x in dm])
    except Exception as e:  # noqa: BLE001
        print("Damodaran failed:", str(e)[:120]); out["gaps"].append("Damodaran data not reachable in this build")
        if prev.get("damodaran"): out["damodaran"] = dict(prev["damodaran"], stale=True)
    try:
        cp, gdp, cf = fred("CP"), fred("GDP"), fred("CNCF")
        q = [d for d in sorted(cp) if d >= "2000-01-01" and d in gdp and d in cf]
        out["fred"] = dict(url="https://fred.stlouisfed.org/series/CP", q=q, cp=[r(cp[d], 0) for d in q],
                           cp_gdp=[r(cp[d] / gdp[d] * 100) for d in q], cf_gdp=[r(cf[d] / gdp[d] * 100) for d in q], cf=[r(cf[d], 0) for d in q])
    except Exception as e:  # noqa: BLE001
        print("FRED failed:", str(e)[:120]); out["gaps"].append("FRED not reachable in this build")
        if prev.get("fred"): out["fred"] = dict(prev["fred"], stale=True)
    out["us10"] = us10()
    if not any(k in out for k in ("shiller", "damodaran", "fred")):
        print("no valuation data; keeping previous file"); return
    out["build_s"] = round(time.time() - t0)
    open(OUT, "w").write(json.dumps(out, separators=(",", ":")))
    print(f"us_valuation.json {os.path.getsize(OUT)} B in {out['build_s']} s; gaps: {out['gaps']}")


if __name__ == "__main__":
    main()

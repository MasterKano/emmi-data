"""Metals page and trader positioning files for EMMi (all free, public sources; no keys).

Writes two files (both published to the live-data branch by .github/workflows/metals.yml):
  data/cot.json     Commitments of Traders, managed money / producer / swap positions and open interest, weekly,
                    about three years per market. CFTC disaggregated futures-only report (Socrata API,
                    publicreporting.cftc.gov, dataset 72hh-3qpy) for the US contracts; ICE Futures Europe's own
                    COT files (COTHist<year>.csv, same layout) for Brent, gasoil and the London softs.
  data/metals.json  COMEX warehouse stocks (CME Group daily depository reports, .xls), gold and silver ETF holdings
                    (SPDR GLD historical archive; iShares IAU and SLV fund pages), official gold reserves by country
                    (IMF International Liquidity dataset, gold volume in fine troy ounces, monthly), and the latest
                    published supply/demand balances (static, cited: scripts/metals_balances.json).

Each source is fetched independently. A source that fails keeps the section from --base (the last published file),
marked stale in "errors"; it never writes zeros. Histories that the sources do not provide (COMEX stocks, iShares
tonnes) are built up one observation per run from --base.

Usage: python scripts/build_metals.py --out DIR [--base DIR] [--only cot,stocks,etf,reserves]
"""
import argparse, csv, datetime as dt, html, io, json, math, os, re, sys

import requests

sys.path.insert(0, os.path.dirname(__file__))
from common import write_json  # noqa: E402

try:
    from curl_cffi import requests as creq
except Exception:  # noqa: BLE001
    creq = None

OZ_PER_T = 32150.7466
UA = {"User-Agent": "Mozilla/5.0 (EMMi data build; +https://github.com/MasterKano/emmi-data)"}
NOW = dt.datetime.now(dt.timezone.utc)

# ---------------------------------------------------------------- CoT
CFTC = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
# id, label, group, Yahoo symbol shown elsewhere on EMMi (None = no price tile), CFTC code, unit of one contract
US = [
    ("wti", "WTI crude", "energy", "CL=F", "067651", "1,000 bbl"),
    ("hh", "Henry Hub gas", "energy", "NG=F", "023651", "10,000 MMBtu"),
    ("ulsd", "NY Harbor ULSD", "energy", "HO=F", "022651", "42,000 gal"),
    ("rbob", "RBOB gasoline", "energy", None, "111659", "42,000 gal"),
    ("gold", "Gold", "precious", "GC=F", "088691", "100 oz"),
    ("silver", "Silver", "precious", "SI=F", "084691", "5,000 oz"),
    ("platinum", "Platinum", "precious", "PL=F", "076651", "50 oz"),
    ("palladium", "Palladium", "precious", "PA=F", "075651", "100 oz"),
    ("copper", "Copper", "base", "HG=F", "085692", "25,000 lb"),
    ("coffee", "Coffee C", "softs", "KC=F", "083731", "37,500 lb"),
    ("cocoa", "Cocoa (NY)", "softs", "CC=F", "073732", "10 t"),
    ("sugar", "Sugar No. 11", "softs", "SB=F", "080732", "112,000 lb"),
    ("cotton", "Cotton No. 2", "softs", "CT=F", "033661", "50,000 lb"),
    ("oj", "Orange juice", "softs", "OJ=F", "040701", "15,000 lb"),
    ("wheat", "Wheat (SRW)", "grains", "ZW=F", "001602", "5,000 bu"),
    ("corn", "Corn", "grains", "ZC=F", "002602", "5,000 bu"),
    ("soybeans", "Soybeans", "grains", "ZS=F", "005602", "5,000 bu"),
    ("soyoil", "Soybean oil", "grains", "ZL=F", "007601", "60,000 lb"),
    ("soymeal", "Soybean meal", "grains", "ZM=F", "026603", "100 short t"),
    ("oats", "Oats", "grains", "ZO=F", "004603", "5,000 bu"),
    ("rice", "Rough rice", "grains", "ZR=F", "039601", "2,000 cwt"),
    ("hogs", "Lean hogs", "livestock", "HE=F", "054642", "40,000 lb"),
    ("cattle", "Live cattle", "livestock", "LE=F", "057642", "40,000 lb"),
    ("lumber", "Lumber", "softs", "LBR=F", "058644", "27,500 bd ft"),
]
ICE = [
    ("brent", "Brent crude", "energy", "BZ=F", "ICE Brent Crude Futures - ICE Futures Europe", "1,000 bbl"),
    ("gasoil", "ICE gasoil", "energy", None, "ICE Gasoil Futures - ICE Futures Europe", "100 t"),
    ("cocoa_ldn", "Cocoa (London)", "softs", None, "ICE Cocoa Futures - ICE Futures Europe", "10 t"),
    ("robusta", "Robusta coffee", "softs", None, "ICE Robusta Coffee Futures - ICE Futures Europe", "10 t"),
    ("white_sugar", "White sugar", "softs", None, "ICE White Sugar Futures - ICE Futures Europe", "50 t"),
]
FIELDS = ["open_interest_all", "m_money_positions_long_all", "m_money_positions_short_all",
          "prod_merc_positions_long", "prod_merc_positions_short", "swap_positions_long_all", "swap__positions_short_all"]
ICE_FIELDS = ["Open_Interest_All", "M_Money_Positions_Long_All", "M_Money_Positions_Short_All",
              "Prod_Merc_Positions_Long_All", "Prod_Merc_Positions_Short_All", "Swap_Positions_Long_All", "Swap_Positions_Short_All"]
WEEKS = 160  # about three years of weekly reports


def _num(v):
    try:
        x = float(str(v).replace(",", ""))
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def pct_rank(vals, v):
    """Share of the window at or below v, 0-100 (v itself included)."""
    xs = [x for x in vals if x is not None]
    if len(xs) < 10 or v is None:
        return None
    return round(100.0 * sum(1 for x in xs if x <= v) / len(xs))


def stats(w):
    """w rows: [date, oi, mml, mms, pml, pms, sl, ss] oldest first."""
    if len(w) < 2:
        return None
    net = [None if r[2] is None or r[3] is None else r[2] - r[3] for r in w]
    last, prev = w[-1], w[-2]
    n, p = net[-1], net[-2]
    yr = net[-52:]
    return {"date": last[0], "oi": last[1], "oi_wow": None if last[1] is None or prev[1] is None else last[1] - prev[1],
            "long": last[2], "short": last[3], "net": n, "net_wow": None if n is None or p is None else n - p,
            "long_wow": None if last[2] is None or prev[2] is None else last[2] - prev[2],
            "short_wow": None if last[3] is None or prev[3] is None else last[3] - prev[3],
            "net_pct_oi": None if n is None or not last[1] else round(100.0 * n / last[1], 1),
            "pct_3y": pct_rank(net, n), "pct_1y": pct_rank(yr, n),
            "hi_3y": max(x for x in net if x is not None), "lo_3y": min(x for x in net if x is not None),
            "prod_net": None if last[4] is None or last[5] is None else last[4] - last[5],
            "swap_net": None if last[6] is None or last[7] is None else last[6] - last[7]}


def cot_cftc():
    since = (NOW - dt.timedelta(weeks=WEEKS + 2)).strftime("%Y-%m-%dT00:00:00.000")
    codes = ",".join("'%s'" % m[4] for m in US)
    q = {"$select": "cftc_contract_market_code,report_date_as_yyyy_mm_dd," + ",".join(FIELDS),
         "$where": "cftc_contract_market_code in(%s) AND report_date_as_yyyy_mm_dd >= '%s'" % (codes, since),
         "$order": "report_date_as_yyyy_mm_dd", "$limit": "50000"}
    r = requests.get(CFTC, params=q, timeout=90, headers=UA)
    r.raise_for_status()
    rows = r.json()
    by = {}
    for x in rows:
        d = x["report_date_as_yyyy_mm_dd"][:10]
        by.setdefault(x["cftc_contract_market_code"].strip(), {})[d] = [d] + [_num(x.get(f)) for f in FIELDS]
    out = []
    for mid, label, grp, sym, code, unit in US:
        w = [by.get(code, {})[d] for d in sorted(by.get(code, {}))][-WEEKS:]
        if len(w) < 10:
            continue
        out.append({"id": mid, "name": label, "group": grp, "sym": sym, "exch": "CFTC", "code": code, "unit": unit,
                    "src": "CFTC disaggregated, futures only", "url": "https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm",
                    "w": w, "s": stats(w)})
    if len(out) < len(US) - 3:
        raise RuntimeError("CFTC returned only %d of %d markets" % (len(out), len(US)))
    return out


def cot_ice():
    y = NOW.year
    frames = []
    for yy in (y - 3, y - 2, y - 1, y):
        try:
            r = requests.get("https://www.ice.com/publicdocs/futures/COTHist%d.csv" % yy, timeout=90, headers=UA)
            r.raise_for_status()
            frames.append(list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig", "replace")))))
        except Exception:  # noqa: BLE001
            if yy == y:
                raise
    rows = [x for f in frames for x in f]
    out = []
    for mid, label, grp, sym, name, unit in ICE:
        by = {}
        for x in rows:
            if (x.get("Market_and_Exchange_Names") or "").strip() != name:
                continue
            try:
                d = dt.datetime.strptime(x["As_of_Date_Form_MM/DD/YYYY"].strip(), "%m/%d/%Y").date().isoformat()
            except (KeyError, ValueError):
                continue
            by[d] = [d] + [_num(x.get(f)) for f in ICE_FIELDS]
        w = [by[d] for d in sorted(by)][-WEEKS:]
        if len(w) < 10:
            continue
        out.append({"id": mid, "name": label, "group": grp, "sym": sym, "exch": "ICE Futures Europe", "unit": unit,
                    "src": "ICE Futures Europe Commitments of Traders, futures only", "url": "https://www.ice.com/report/122",
                    "w": w, "s": stats(w)})
    return out


# ---------------------------------------------------------------- COMEX warehouse stocks
CME = [("gold", "Gold", "https://www.cmegroup.com/delivery_reports/Gold_Stocks.xls", None),
       ("silver", "Silver", "https://www.cmegroup.com/delivery_reports/Silver_stocks.xls", None),
       ("copper", "Copper", "https://www.cmegroup.com/delivery_reports/Copper_Stocks.xls", None),
       ("platinum", "Platinum", "https://www.cmegroup.com/delivery_reports/PA-PL_Stck_Rprt.xls", "PLATINUM"),
       ("palladium", "Palladium", "https://www.cmegroup.com/delivery_reports/PA-PL_Stck_Rprt.xls", "PALLADIUM"),
       ("aluminum", "Aluminium", "https://www.cmegroup.com/delivery_reports/Aluminum_Stocks.xls", None)]


def _get(url, binary=False, timeout=60):
    if creq is not None:
        r = creq.get(url, impersonate="chrome", timeout=timeout)
    else:
        r = requests.get(url, timeout=timeout, headers=UA)
    if r.status_code != 200:
        raise RuntimeError("%s HTTP %s" % (url, r.status_code))
    return r.content if binary else r.text


def _mdy(s):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat() if m else None


def parse_cme(content, section=None):
    import xlrd
    sh = xlrd.open_workbook(file_contents=content).sheet_by_index(0)
    rows = [[str(c.value).strip() for c in sh.row(i)] for i in range(sh.nrows)]
    start = 0
    if section:
        start = next(i for i, r in enumerate(rows) if r and r[0].upper() == section)
    res = {}
    for i in range(start, len(rows)):
        r = rows[i]
        j = " ".join(r)
        if "Report Date" in j and "report_date" not in res:
            res["report_date"] = _mdy(j)
            if i + 1 < len(rows):
                res["unit"] = rows[i + 1][0]
                res["activity_date"] = _mdy(" ".join(rows[i + 1]))
        lab = (r[0] if r else "").upper()
        vals = [_num(x) for x in r[2:8]]  # prev, received, withdrawn, net change, adjustment, total today
        if lab.startswith("TOTAL REGISTERED"):
            res["registered"], res["registered_prev"] = vals[5], vals[0]
        elif lab.startswith("TOTAL PLEDGED"):
            res["pledged"] = vals[5]
        elif lab.startswith("TOTAL ELIGIBLE"):
            res["eligible"], res["eligible_prev"] = vals[5], vals[0]
        elif lab.startswith("COMBINED TOTAL") or (lab.startswith("TOTAL ") and lab[6:] in ("COPPER", "ALUMINUM")):
            res["total"], res["total_prev"] = vals[5], vals[0]
            res["received"], res["withdrawn"] = vals[1], vals[2]
            break
    if res.get("total") is None or not res.get("report_date"):
        raise RuntimeError("CME report layout not recognised")
    return res


def stocks(base):
    cache, out, hist = {}, {}, (base or {}).get("stocks_hist", {})
    for mid, label, url, sec in CME:
        if url not in cache:
            cache[url] = _get(url, binary=True)
        r = parse_cme(cache[url], sec)
        r.update(name=label, url=url)
        out[mid] = r
        h = {x[0]: x for x in hist.get(mid, [])}
        if r.get("activity_date"):
            # the report itself carries the previous day's total, so the first run already has a change
            h[r["activity_date"]] = [r["activity_date"], r.get("total"), r.get("registered")]
        hist[mid] = [h[d] for d in sorted(h)][-400:]
    return out, hist


# ---------------------------------------------------------------- ETFs
def etf_gld():
    import openpyxl
    b = _get("https://api.spdrgoldshares.com/api/v1/historical-archive?product=gld&exchange=NYSE&lang=en", binary=True, timeout=90)
    wb = openpyxl.load_workbook(io.BytesIO(b), read_only=True, data_only=True)
    ws = next(wb[n] for n in wb.sheetnames if "Archive" in n)
    rows = list(ws.iter_rows(values_only=True))
    head = [str(c or "") for c in rows[0]]
    it = head.index("Tonnes of Gold")
    ser = []
    for r in rows[1:]:
        if not r or r[0] is None:
            continue
        d = r[0]
        if isinstance(d, str):
            try:
                d = dt.datetime.strptime(d.strip(), "%d-%b-%Y")
            except ValueError:
                continue
        t = _num(r[it])
        if t:
            ser.append([d.date().isoformat() if hasattr(d, "date") else str(d)[:10], round(t, 2)])
    ser = [x for x in ser if x[0] >= (NOW.date() - dt.timedelta(days=3 * 366)).isoformat()]
    if len(ser) < 100:
        raise RuntimeError("GLD archive too short")
    return {"name": "SPDR Gold Shares (GLD)", "metal": "gold", "unit": "t", "src": "SPDR Gold Shares historical archive",
            "url": "https://www.spdrgoldshares.com/usa/gld/", "asof": ser[-1][0], "t": ser[-1][1], "series": ser}


def etf_ishares(pid, slug, name, metal):
    url = "https://www.ishares.com/us/products/%s/%s" % (pid, slug)
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(_get(url))))
    m = re.search(r"Tonnes in Trust ([\d,.]+) as of (\w{3} \d{1,2}, \d{4})", t)
    if not m:
        raise RuntimeError("%s: tonnes not found" % name)
    d = dt.datetime.strptime(m.group(2), "%b %d, %Y").date().isoformat()
    return {"name": name, "metal": metal, "unit": "t", "src": "iShares fund page (Tonnes in Trust)", "url": url,
            "asof": d, "t": _num(m.group(1))}


def etfs(base):
    old = (base or {}).get("etf", {})
    out, errs = {}, []
    for key, fn in (("GLD", etf_gld),
                    ("IAU", lambda: etf_ishares("239561", "ishares-gold-trust-fund", "iShares Gold Trust (IAU)", "gold")),
                    ("SLV", lambda: etf_ishares("239855", "ishares-silver-trust-fund", "iShares Silver Trust (SLV)", "silver"))):
        try:
            e = fn()
            if "series" not in e:  # build the history one observation per run
                h = {x[0]: x for x in (old.get(key) or {}).get("series", [])}
                h[e["asof"]] = [e["asof"], e["t"]]
                e["series"] = [h[d] for d in sorted(h)][-800:]
            out[key] = e
        except Exception as ex:  # noqa: BLE001
            errs.append("%s: %s" % (key, ex))
            if key in old:
                out[key] = dict(old[key], stale=True)
    return out, errs


# ---------------------------------------------------------------- official gold reserves
IMF = "https://api.imf.org/external/sdmx/2.1/data/IMF.STA,IL/.RGV_REVS..M"


def imf_names():
    try:
        r = requests.get("https://api.imf.org/external/sdmx/2.1/codelist/IMF.STA/CL_IL_COUNTRY/latest", timeout=60, headers=UA)
        r.raise_for_status()
        out = {}
        for m in re.finditer(r'<str:Code id="([^"]+)"(.*?)</str:Code>', r.text, re.S):
            en = re.search(r'<com:Name xml:lang="en">([^<]*)<', m.group(2))
            if en:
                out[m.group(1)] = html.unescape(en.group(1))
        return out
    except Exception:  # noqa: BLE001
        return {}


SHORT = {"CHN": "China", "RUS": "Russia", "USA": "United States", "GBR": "United Kingdom", "TUR": "Türkiye",
         "KOR": "South Korea", "TWN": "Taiwan", "EZB": "ECB", "NLD": "Netherlands", "CZE": "Czech Republic",
         "IRN": "Iran", "VEN": "Venezuela", "SAU": "Saudi Arabia", "ARE": "UAE", "EGY": "Egypt"}


def reserves():
    start = (NOW.date().replace(day=1) - dt.timedelta(days=800)).strftime("%Y-%m")
    text = None
    for i in range(4):  # the IMF API sometimes drops the connection mid-body
        try:
            r = requests.get(IMF, params={"startPeriod": start}, timeout=120,
                             headers=dict(UA, Accept="application/vnd.sdmx.data+csv;version=1.0.0"))
            r.raise_for_status()
            text = r.text
            break
        except Exception:  # noqa: BLE001
            if i == 3:
                raise
            import time; time.sleep(5 * (i + 1))
    by = {}
    for x in csv.DictReader(io.StringIO(text)):
        c, p, v = x.get("COUNTRY"), x.get("TIME_PERIOD", ""), _num(x.get("OBS_VALUE"))
        if not c or v is None or not re.fullmatch(r"[A-Z]{3}", c) or c in ("IMF", "BIS"):
            continue  # country codes only: drops IMF/BIS and the G### regional aggregates
        m = re.fullmatch(r"(\d{4})-M(\d{2})", p)
        if m:
            by.setdefault(c, {})["%s-%s" % m.groups()] = v / OZ_PER_T
    names = imf_names()
    latest_all = max(max(v) for v in by.values())
    rows = []
    for c, ser in by.items():
        last = max(ser)
        # stale reporters (more than 6 months behind the newest month in the file) are listed but flagged
        ks = sorted(ser)

        def back(n):
            y, m = map(int, last.split("-"))
            m -= n
            while m <= 0:
                m += 12; y -= 1
            return ser.get("%04d-%02d" % (y, m))
        t = ser[last]
        dec = ser.get("%s-12" % (int(last[:4]) - 1))
        stale = last < _mback(latest_all, 3)
        rows.append({"stale": stale, "iso": c, "name": SHORT.get(c) or names.get(c, c).split(",")[0], "month": last, "t": round(t, 1),
                     "m1": None if back(1) is None else round(t - back(1), 1),
                     "m12": None if back(12) is None else round(t - back(12), 1),
                     "ytd": None if dec is None else round(t - dec, 1),
                     "spark": [round(ser[k], 1) for k in ks[-25:]]})
    rows.sort(key=lambda x: -x["t"])
    if len(rows) < 50:
        raise RuntimeError("IMF returned only %d countries" % len(rows))
    recent = [x for x in rows if x["month"] >= _mback(latest_all, 3)]
    buyers = sorted([x for x in recent if (x["ytd"] or 0) > 0.5], key=lambda x: -(x["ytd"] or 0))[:12]
    sellers = sorted([x for x in recent if (x["ytd"] or 0) < -0.5], key=lambda x: x["ytd"] or 0)[:6]
    return {"src": "IMF International Liquidity (IL), gold reserves volume (RGV), fine troy ounces converted to tonnes",
            "url": "https://data.imf.org/en/datasets/IMF.STA:IL", "latest_month": latest_all, "n": len(rows),
            "top": rows[:20], "buyers": buyers, "sellers": sellers}


def _mback(ym, n):
    y, m = map(int, ym.split("-"))
    m -= n
    while m <= 0:
        m += 12; y -= 1
    return "%04d-%02d" % (y, m)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default=None, help="directory holding the last published cot.json / metals.json")
    ap.add_argument("--only", default="cot,stocks,etf,reserves")
    a = ap.parse_args()
    only = set(a.only.split(","))
    os.makedirs(a.out, exist_ok=True)

    def load(name):
        p = os.path.join(a.base, name) if a.base else None
        try:
            return json.load(open(p)) if p and os.path.exists(p) else {}
        except Exception:  # noqa: BLE001
            return {}
    bcot, bmet = load("cot.json"), load("metals.json")
    stamp = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    cot = dict(bcot) if bcot else {}
    if "cot" in only:
        errs, mk = [], []
        for name, fn in (("CFTC", cot_cftc), ("ICE", cot_ice)):
            try:
                mk += fn()
            except Exception as ex:  # noqa: BLE001
                errs.append("%s: %s" % (name, ex))
                keep = [m for m in bcot.get("markets", []) if (m.get("exch") == "CFTC") == (name == "CFTC")]
                mk += [dict(m, stale=True) for m in keep]
        if not mk:
            raise SystemExit("no CoT data from any source and no base copy")
        order = {m[0]: i for i, m in enumerate(US + ICE)}
        mk.sort(key=lambda m: order.get(m["id"], 99))
        cot = {"generated_utc": stamp, "markets": mk, "errors": errs,
               "report_date": max(m["w"][-1][0] for m in mk if m.get("exch") == "CFTC") if any(m.get("exch") == "CFTC" for m in mk) else None,
               "cols": ["date", "open_interest", "managed_money_long", "managed_money_short", "producer_long", "producer_short", "swap_long", "swap_short"],
               "note": "Positions in contracts. CFTC reports positions as of Tuesday and publishes them on Friday at 15:30 New York time; ICE Futures Europe publishes its report for the same Tuesday."}
        write_json(os.path.join(a.out, "cot.json"), cot)

    met = dict(bmet) if bmet else {}
    met["generated_utc"] = stamp
    errs = []
    if "stocks" in only:
        try:
            met["stocks"], met["stocks_hist"] = stocks(bmet)
            met["stocks_src"] = {"src": "CME Group, COMEX/NYMEX metal depository statistics (daily)", "url": "https://www.cmegroup.com/clearing/operations-and-deliveries/nymex-delivery-notices.html"}
        except Exception as ex:  # noqa: BLE001
            errs.append("stocks: %s" % ex)
    if "etf" in only:
        e, ee = etfs(bmet)
        if e:
            met["etf"] = e
        errs += ee
    if "reserves" in only:
        try:
            met["reserves"] = reserves()
        except Exception as ex:  # noqa: BLE001
            errs.append("reserves: %s" % ex)
    bal = os.path.join(os.path.dirname(__file__), "metals_balances.json")
    if os.path.exists(bal):
        met["balances"] = json.load(open(bal))
    met["errors"] = errs
    write_json(os.path.join(a.out, "metals.json"), met)
    for e in errs + cot.get("errors", []):
        print("WARN", e)
    print("cot markets:", len(cot.get("markets", [])), "report", cot.get("report_date"),
          "| stocks:", sorted((met.get("stocks") or {}).keys()), "| etf:", sorted((met.get("etf") or {}).keys()),
          "| reserves month:", (met.get("reserves") or {}).get("latest_month"))


if __name__ == "__main__":
    main()

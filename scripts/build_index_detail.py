"""Index detail for the Live Pulse Markets tiles (t1405u): sector performance and the largest members of each index.

For each equity index: the largest holdings and sector weights of a fund that tracks it (Yahoo Finance topHoldings, the
fund's latest reported holdings), each holding's 1D/1M price change (Yahoo chart), and sector performance where a free
daily sector series exists:
  S&P 500 -> the 11 Select Sector SPDR ETFs; STOXX Europe 600 -> iShares STOXX Europe 600 sector ETFs (Xetra);
  CSI 300 -> the CSI 300 sector indices (China Securities Index Co., csindex.com.cn).
Other indices show sector weights only (no free daily sector series), and the page says so. Nothing is estimated.
Usage: python scripts/build_index_detail.py [--out data/index_detail.json]
"""
import argparse, datetime as dt, json, os, re, time

import pandas as pd

import build_data as B

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPDR = [("Technology", "XLK"), ("Financials", "XLF"), ("Health care", "XLV"), ("Consumer discretionary", "XLY"),
        ("Communication", "XLC"), ("Industrials", "XLI"), ("Consumer staples", "XLP"), ("Energy", "XLE"),
        ("Utilities", "XLU"), ("Real estate", "XLRE"), ("Materials", "XLB")]
STOXX = [("Banks", "EXV1.DE"), ("Industrial goods", "EXH4.DE"), ("Health care", "EXV4.DE"), ("Technology", "EXV3.DE"),
         ("Insurance", "EXH5.DE"), ("Food & beverage", "EXH3.DE"), ("Personal & household", "EXH7.DE"), ("Oil & gas", "EXH1.DE"),
         ("Utilities", "EXH9.DE"), ("Financial services", "EXH2.DE"), ("Telecoms", "EXV2.DE"), ("Construction", "EXV8.DE"),
         ("Chemicals", "EXV7.DE"), ("Basic resources", "EXV6.DE"), ("Autos & parts", "EXV5.DE"), ("Media", "EXH6.DE"),
         ("Travel & leisure", "EXV9.DE"), ("Retail", "EXH8.DE"), ("Real estate", "EXI5.DE")]
CSI = [("Financials & real estate", "000914"), ("Info technology", "000915"), ("Industrials", "000910"),
       ("Consumer staples", "000912"), ("Consumer discretionary", "000911"), ("Materials", "000909"), ("Health care", "000913"),
       ("Utilities", "000917"), ("Energy", "000908"), ("Communication", "000916")]
INDICES = {
    "^GSPC": dict(etf="SPY", sectors=("yahoo", SPDR, "Select Sector SPDR ETFs", "https://www.sectorspdrs.com/")),
    "^IXIC": dict(etf="QQQ", note="QQQ tracks the Nasdaq-100, the largest 100 non-financial Nasdaq stocks."),
    "^DJI": dict(etf="DIA"),
    "^STOXX": dict(etf="EXSA.DE", sectors=("yahoo", STOXX, "iShares STOXX Europe 600 sector ETFs (Xetra)", "https://www.ishares.com/de")),
    "^STOXX50E": dict(etf="EXW1.DE"),
    "^GDAXI": dict(etf="EXS1.DE"),
    "^FTSE": dict(etf="ISF.L"),
    "^SSMI": dict(etf="CSSMI.SW"),
    "^N225": dict(etf="1321.T"),
    "^HSI": dict(etf="2800.HK"),
    "000300.SS": dict(etf="ASHR", sectors=("csindex", CSI, "CSI 300 sector indices (China Securities Index Co.)", "https://www.csindex.com.cn/en/indices/index-detail/000300"),
                      note="ASHR (Xtrackers Harvest CSI 300) holdings stand in for the index weights."),
}
SECTOR_LAB = dict(technology="Technology", financial_services="Financials", healthcare="Health care", consumer_cyclical="Consumer discretionary",
                  communication_services="Communication", industrials="Industrials", consumer_defensive="Consumer staples", energy="Energy",
                  utilities="Utilities", realestate="Real estate", basic_materials="Materials")


def changes(df):
    c = df["close"].dropna()
    if len(c) < 2:
        return None
    last, d = float(c.iloc[-1]), c.index[-1]
    m = c[c.index <= d - pd.DateOffset(months=1)]
    return dict(px=round(last, 4), d1=round((last / float(c.iloc[-2]) - 1) * 100, 2),
                m1=round((last / float(m.iloc[-1]) - 1) * 100, 2) if len(m) else None, asof=d.strftime("%Y-%m-%d"))


def yahoo(sym):
    df, meta = B.fetch_chart(sym, years=1, tries=3)
    x = changes(B.clean(df))
    if x:
        x["ccy"] = (meta or {}).get("currency")
    return x


def csindex(code):
    r = B.SESSION.get("https://www.csindex.com.cn/csindex-home/perf/index-perf",
                      params=dict(indexCode=code, startDate=(dt.date.today() - dt.timedelta(days=60)).strftime("%Y%m%d"),
                                  endDate=dt.date.today().strftime("%Y%m%d")), timeout=40)
    rows = [(pd.Timestamp(dt.datetime.strptime(x["tradeDate"], "%Y%m%d")), x["close"]) for x in r.json().get("data") or []]
    rows = [(d, v) for d, v in rows if d.weekday() < 5 and v]
    df = pd.DataFrame({"close": [v for _, v in rows]}, index=pd.DatetimeIndex([d for d, _ in rows]))
    return changes(df)


def top_holdings(etf, crumb):
    r = B.SESSION.get(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{etf}",
                      params=dict(modules="topHoldings,price", crumb=crumb), timeout=30).json()
    res = r["quoteSummary"]["result"][0]
    th = res.get("topHoldings") or {}
    hold = [dict(sym=h.get("symbol"), name=h.get("holdingName"), w=round(h["holdingPercent"]["raw"] * 100, 2))
            for h in th.get("holdings") or [] if (h.get("holdingPercent") or {}).get("raw")]
    sw = []
    for d in th.get("sectorWeightings") or []:
        for k, v in d.items():
            if (v or {}).get("raw"):
                sw.append(dict(k=SECTOR_LAB.get(k, k), w=round(v["raw"] * 100, 1)))
    sw.sort(key=lambda x: -x["w"])
    name = ((res.get("price") or {}).get("longName")) or etf
    return hold, sw, name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "index_detail.json"))
    a = ap.parse_args()
    crumb = B.yahoo_crumb(B.SESSION)
    out, errs = {}, []
    for sym, cfg in INDICES.items():
        e = dict(etf=cfg["etf"], note=cfg.get("note"))
        try:
            hold, sw, name = top_holdings(cfg["etf"], crumb)
            e.update(etf_name=name, sector_weights=sw, holdings=[],
                     holdings_src=dict(name=f"{name} top holdings via Yahoo Finance (fund's latest reported)",
                                       url=f"https://finance.yahoo.com/quote/{cfg['etf']}/holdings/"))
            for h in hold:
                if re.match(r"^\d{4,5}$", h["sym"] or "") and cfg["etf"].endswith(".HK"):   # HK listings come back as bare codes
                    h["sym"] = str(int(h["sym"])).zfill(4) + ".HK"
                try:
                    h.update(yahoo(h["sym"]) or {})
                except Exception as ex:
                    h["error"] = str(ex)[:80]
                e["holdings"].append(h)
                time.sleep(0.15)
        except Exception as ex:
            errs.append(f"{sym} holdings: {ex}")
        if cfg.get("sectors"):
            kind, lst, src, url = cfg["sectors"]
            rows = []
            for lab, s in lst:
                try:
                    x = (csindex(s) if kind == "csindex" else yahoo(s)) or {}
                    rows.append(dict(name=lab, sym=s, **{k: x.get(k) for k in ("d1", "m1", "asof")}))
                except Exception as ex:
                    rows.append(dict(name=lab, sym=s, error=str(ex)[:80]))
                time.sleep(0.15)
            e["sectors"] = dict(src=src, url=url, rows=rows)
        out[sym] = e
        print(sym, len(e.get("holdings", [])), "holdings,", len((e.get("sectors") or {}).get("rows", [])), "sector rows", flush=True)
    doc = dict(schema=1, generated_utc=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), indices=out, errors=errs)
    with open(a.out, "w") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote", a.out, "errors:", errs)
    return 0 if len(out) and len(errs) < len(INDICES) else 1


if __name__ == "__main__":
    raise SystemExit(main())

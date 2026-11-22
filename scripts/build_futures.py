#!/usr/bin/env python3
"""Per-commodity futures chains and per-contract daily history for the EMMi commodity sheet (Futures section).

Standalone: Python stdlib only. Free, public, delayed sources only (no login, no API key):
  chain    TradingView public futures scanner: every listed month with last, change, open/high/low, volume,
           open interest and expiry (~10 minutes delayed; after the close it is the session's last/settle).
  history  Yahoo Finance per-contract daily bars (CLF27.NYM, KCH27.NYB, ...). Brent uses NYMEX BZ (the Brent
           last-day financial contract, which settles on ICE Brent: same month, same price). TTF uses Yahoo's
           TTF contracts (same prices as ICE Endex TFM). EU carbon and lumber have no usable Yahoo contract
           history, so their history is the daily TradingView bar archived here from the first run on.

Output (emmi-data main, history kept):
  futures/index.json                      what is available, per EMMi tile symbol
  futures/<id>.json                       chain (listed contracts) + a 3-month closes matrix for curve compares
  futures/<id>/<CONTRACT>.csv             date,open,high,low,close,volume,oi,source  (one row per session)

Rules: a missing value stays blank (never zero, never interpolated, never carried forward). Rows are only
replaced by a bar for the same date from a source of equal or higher rank (yahoo > tradingview).

  python scripts/build_futures.py --out futures [--backfill] [--only brent,wti]
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
MONTH_CODES = "FGHJKMNQUVXZ"
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
TV_URL = "https://scanner.tradingview.com/futures/scan"
TV_COLS = ["name", "close", "change_abs", "open", "high", "low", "volume", "open_interest", "expiration", "exchange"]
YH_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{t}?range={r}&interval=1d"
HFIELDS = ["date", "open", "high", "low", "close", "volume", "oi", "source"]
RANK = {"yahoo": 2, "tradingview": 1}
MAX_CONTRACTS = 18      # listed months kept in the chain (nearest first)
MATRIX_SESSIONS = 70    # closes matrix for today vs 1W / 1M / 3M curve compares

# id, EMMi tile symbol, title, exchange label, TradingView exchange + root, Yahoo root + suffix (None = no history
# source), unit, display decimals
SPECS = [
    ("brent", "BZ=F", "Brent crude", "ICE Futures Europe", "ICEEUR", "BRN", "BZ", ".NYM", "USD/bbl", 2),
    ("wti", "CL=F", "WTI crude", "NYMEX", "NYMEX", "CL", "CL", ".NYM", "USD/bbl", 2),
    ("ulsd", "HO=F", "NY Harbor diesel (ULSD)", "NYMEX", "NYMEX", "HO", "HO", ".NYM", "USD/gal", 4),
    ("rbob", "RB=F", "RBOB gasoline", "NYMEX", "NYMEX", "RB", "RB", ".NYM", "USD/gal", 4),
    ("hh", "NG=F", "Henry Hub gas", "NYMEX", "NYMEX", "NG", "NG", ".NYM", "USD/MMBtu", 3),
    ("ttf", "TTF=F", "Dutch TTF gas", "ICE Endex", "ICEENDEX", "TFM", "TTF", ".NYM", "EUR/MWh", 3),
    ("eua", "CO2.L", "EU carbon (EUA)", "ICE Endex", "ICEENDEX", "ECF", None, None, "EUR/t", 2),
    ("gold", "GC=F", "Gold", "COMEX", "COMEX", "GC", "GC", ".CMX", "USD/oz", 1),
    ("silver", "SI=F", "Silver", "COMEX", "COMEX", "SI", "SI", ".CMX", "USD/oz", 3),
    ("platinum", "PL=F", "Platinum", "NYMEX", "NYMEX", "PL", "PL", ".NYM", "USD/oz", 1),
    ("palladium", "PA=F", "Palladium", "NYMEX", "NYMEX", "PA", "PA", ".NYM", "USD/oz", 1),
    ("copper", "HG=F", "Copper", "COMEX", "COMEX", "HG", "HG", ".CMX", "USD/lb", 4),
    ("aluminium", "ALI=F", "Aluminium", "COMEX", "COMEX", "ALI", "ALI", ".CMX", "USD/t", 0),
    ("coffee", "KC=F", "Coffee (Arabica)", "ICE Futures US", "ICEUS", "KC", "KC", ".NYB", "USc/lb", 2),
    ("cocoa", "CC=F", "Cocoa NY", "ICE Futures US", "ICEUS", "CC", "CC", ".NYB", "USD/t", 0),
    ("sugar", "SB=F", "Sugar No.11", "ICE Futures US", "ICEUS", "SB", "SB", ".NYB", "USc/lb", 2),
    ("cotton", "CT=F", "Cotton", "ICE Futures US", "ICEUS", "CT", "CT", ".NYB", "USc/lb", 2),
    ("oj", "OJ=F", "Orange juice", "ICE Futures US", "ICEUS", "OJ", "OJ", ".NYB", "USc/lb", 2),
    ("lumber", "LBR=F", "Lumber", "CME", "CME", "LBR", None, None, "USD/1000 bd ft", 1),
    ("wheat", "ZW=F", "Wheat Chicago", "CBOT", "CBOT", "ZW", "ZW", ".CBT", "USc/bu", 2),
    ("corn", "ZC=F", "Corn", "CBOT", "CBOT", "ZC", "ZC", ".CBT", "USc/bu", 2),
    ("soybeans", "ZS=F", "Soybeans", "CBOT", "CBOT", "ZS", "ZS", ".CBT", "USc/bu", 2),
    ("soyoil", "ZL=F", "Soybean oil", "CBOT", "CBOT", "ZL", "ZL", ".CBT", "USc/lb", 2),
    ("soymeal", "ZM=F", "Soybean meal", "CBOT", "CBOT", "ZM", "ZM", ".CBT", "USD/short ton", 1),
    ("oats", "ZO=F", "Oats", "CBOT", "CBOT", "ZO", "ZO", ".CBT", "USc/bu", 2),
    ("rice", "ZR=F", "Rough rice", "CBOT", "CBOT", "ZR", "ZR", ".CBT", "USD/cwt", 3),
    ("cattle", "LE=F", "Live cattle", "CME", "CME", "LE", "LE", ".CME", "USc/lb", 3),
    ("feeders", "GF=F", "Feeder cattle", "CME", "CME", "GF", "GF", ".CME", "USc/lb", 3),
    ("hogs", "HE=F", "Lean hogs", "CME", "CME", "HE", "HE", ".CME", "USc/lb", 3),
]


def log(m):
    print(m, flush=True)


def http(url, data=None, headers=None, timeout=40, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": UA},
                                         method="POST" if data is not None else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                raise
            time.sleep(2 + 4 * i)
        except Exception as e:  # network hiccup
            last = e
            time.sleep(2 + 4 * i)
    raise last


def rnd(v, nd=6):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return round(f, nd)


def s(v):
    """number -> compact text; None -> blank"""
    if v is None:
        return ""
    t = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if t in ("-0", "") else t


def atomic(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


# ---- sources ------------------------------------------------------------------------------------------------
def tv_chain(exchange, root):
    body = {"filter": [{"left": "exchange", "operation": "equal", "right": exchange},
                       {"left": "name", "operation": "match", "right": f"^{root}[FGHJKMNQUVXZ][0-9]{{4}}$"}],
            "symbols": {"query": {"types": []}}, "columns": TV_COLS, "range": [0, 400]}
    raw = http(TV_URL, json.dumps(body).encode(), {
        "User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json",
        "Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"})
    out = []
    for item in json.loads(raw).get("data") or []:
        d = dict(zip(TV_COLS, item.get("d") or []))
        name = str(d.get("name") or "")
        if d.get("exchange") != exchange or len(name) != len(root) + 5:
            continue
        letter, year = name[len(root)], int(name[len(root) + 1:])
        month = MONTH_CODES.index(letter) + 1
        exp = d.get("expiration")
        expiry = None
        if exp:
            e = str(int(exp))
            expiry = f"{e[:4]}-{e[4:6]}-{e[6:8]}" if len(e) == 8 else None
        out.append({"code": f"{root}{letter}{year % 100:02d}", "letter": letter, "year": year, "month": month,
                    "expiry": expiry, "last": rnd(d.get("close")), "chg": rnd(d.get("change_abs")),
                    "open": rnd(d.get("open")), "high": rnd(d.get("high")), "low": rnd(d.get("low")),
                    "vol": rnd(d.get("volume")), "oi": rnd(d.get("open_interest"))})
    return out


def yahoo_bars(ticker, rng, nd):
    try:
        raw = http(YH_URL.format(t=ticker, r=rng), headers={"User-Agent": UA, "Accept": "application/json"})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise
    res = (json.loads(raw).get("chart") or {}).get("result") or []
    if not res:
        return []
    r = res[0]
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    # daily bars are stamped at the session's local midnight in New York or Chicago (04:00-06:00 UTC);
    # +12 h lands inside the same calendar day in both summer and winter time
    out = []
    for i, t in enumerate(ts):
        c = rnd((q.get("close") or [None] * len(ts))[i], nd)
        if c is None:
            continue
        dd = dt.datetime.fromtimestamp(t + 43200, dt.timezone.utc).date()
        if dd.weekday() >= 5:  # stray weekend stamp (holiday-shortened session): not a settlement day
            continue
        day = dd.isoformat()
        g = lambda k: rnd((q.get(k) or [None] * len(ts))[i], nd)
        o, h, l = g("open"), g("high"), g("low")
        v = rnd((q.get("volume") or [None] * len(ts))[i], 0)
        # Yahoo marks a no-trade day with open=high=low=close and zero volume: keep it as a close-only bar
        if v in (None, 0) and o == h == l == c:
            o = h = l = None
        out.append({"date": day, "open": o, "high": h, "low": l, "close": c, "volume": v, "oi": None, "source": "yahoo"})
    # one bar per date (the last one wins)
    by = {b["date"]: b for b in out}
    return [by[k] for k in sorted(by)]


def session_date(now_utc):
    """Session the TradingView snapshot belongs to: today (UTC) after 20:00 UTC on a weekday, else the
    previous weekday. Only used when Yahoo has no bar to date it."""
    d = now_utc.date()
    if d.weekday() < 5 and now_utc.hour >= 20:
        return d.isoformat()
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


# ---- history files --------------------------------------------------------------------------------------------
def read_hist(path):
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = {}
    for r in rows:
        out[r["date"]] = {"date": r["date"], "source": r.get("source") or "",
                          **{k: rnd(r.get(k)) if (r.get(k) or "") != "" else None for k in ("open", "high", "low", "close", "volume", "oi")}}
    return out


def merge_bar(hist, bar):
    old = hist.get(bar["date"])
    if old and RANK.get(old.get("source"), 0) > RANK.get(bar["source"], 0):
        # keep the higher-ranked bar, but take open interest when only the lower source has it
        if old.get("oi") is None and bar.get("oi") is not None:
            old["oi"] = bar["oi"]
        return
    if old and bar.get("oi") is None and old.get("oi") is not None:
        bar = dict(bar, oi=old["oi"])
    hist[bar["date"]] = bar


def write_hist(path, hist):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(HFIELDS)
    for k in sorted(hist):
        b = hist[k]
        if b.get("close") is None:
            continue
        w.writerow([k] + [s(b.get(f)) for f in ("open", "high", "low", "close", "volume", "oi")] + [b.get("source") or ""])
    text = buf.getvalue()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    atomic(path, text)
    return True


# ---- build ----------------------------------------------------------------------------------------------------
def build_one(spec, out, backfill, now_utc, today):
    cid, sym, title, exch_label, tv_ex, tv_root, y_root, y_suf, unit, dec = spec
    chain = [c for c in tv_chain(tv_ex, tv_root) if c["last"] is not None and c["expiry"] and c["expiry"] >= today]
    chain.sort(key=lambda c: (c["expiry"], c["code"]))
    # keep the nearest listed months; drop far months that have never traded (no open interest, no volume)
    live = [c for c in chain if (c["oi"] or 0) > 0 or (c["vol"] or 0) > 0]
    chain = (live if len(live) >= 3 else chain)[:MAX_CONTRACTS]
    if len(chain) < 2:
        raise RuntimeError(f"{cid}: only {len(chain)} priced contracts from TradingView")
    tv_day = session_date(now_utc)
    hist_src = "yahoo" if y_root else "tradingview"
    for c in chain:
        path = os.path.join(out, cid, c["code"] + ".csv")
        hist = read_hist(path)
        ybars = []
        if y_root:
            tick = f"{y_root}{c['letter']}{c['year'] % 100:02d}{y_suf}"
            try:
                ybars = yahoo_bars(tick, "1y" if (backfill or not hist) else "1mo", dec + 1)
            except Exception as e:
                log(f"  {cid} {tick}: yahoo failed ({e})")
            time.sleep(0.25)
            for b in ybars:
                merge_bar(hist, b)
        # the TradingView bar dates itself to Yahoo's latest bar when Yahoo has one for this contract
        day = ybars[-1]["date"] if ybars and abs(ybars[-1]["close"] - c["last"]) < 10 ** -(dec + 1) else tv_day
        if ybars and day != ybars[-1]["date"] and ybars[-1]["date"] > tv_day:
            day = ybars[-1]["date"]
        bar = {"date": day, "open": c["open"], "high": c["high"], "low": c["low"], "close": c["last"],
               "volume": c["vol"], "oi": c["oi"], "source": "tradingview"}
        prev = [hist[k] for k in sorted(hist) if k < day]
        same = prev and all(prev[-1].get(k) == bar[k] for k in ("open", "high", "low", "close", "volume"))
        if same and day not in hist:
            # an unchanged snapshot (exchange holiday, or no session yet): not a new session, so not archived
            log(f"  {cid} {c['code']}: snapshot unchanged since {prev[-1]['date']}, not dated {day}")
            day = prev[-1]["date"]
        else:
            merge_bar(hist, bar)
        write_hist(path, hist)
        c["hist"] = hist
        c["asof"] = day
    # closes matrix: union of recent session dates across the chain
    dates = sorted({d for c in chain for d in c["hist"]})[-MATRIX_SESSIONS:]
    closes = {c["code"]: [c["hist"][d]["close"] if d in c["hist"] else None for d in dates] for c in chain}
    asof = max(c["asof"] for c in chain)
    doc = {
        "id": cid, "sym": sym, "title": title, "exchange": exch_label, "unit": unit, "dec": dec,
        "asof": asof, "built_utc": now_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": {"chain": "TradingView public futures scanner (delayed about 10 minutes; after the close, the session last)",
                   "history": ("Yahoo Finance daily bars per contract" + (" (NYMEX BZ, the Brent last-day financial that settles on ICE Brent)" if cid == "brent" else "")
                               if y_root else "TradingView daily bar archived by EMMi from " + min(min(c["hist"]) for c in chain))},
        "history_source": hist_src,
        "contracts": [{
            "c": c["code"], "label": f"{MON[c['month'] - 1]} {c['year'] % 100:02d}", "exp": c["expiry"],
            "last": c["last"], "chg": c["chg"], "open": c["open"], "high": c["high"], "low": c["low"],
            "vol": c["vol"], "oi": c["oi"], "asof": c["asof"], "first": min(c["hist"]), "n": len(c["hist"]),
        } for c in chain],
        "dates": dates,
        "closes": closes,
    }
    atomic(os.path.join(out, cid + ".json"), json.dumps(doc, separators=(",", ":")))
    log(f"  {cid}: {len(chain)} contracts {chain[0]['code']}..{chain[-1]['code']}, as of {asof}, "
        f"history from {doc['contracts'][0]['first']} ({hist_src})")
    return {"id": cid, "sym": sym, "title": title, "asof": asof, "n": len(chain), "front": chain[0]["code"],
            "history": hist_src}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="futures")
    ap.add_argument("--backfill", action="store_true", help="fetch a year of Yahoo bars for every contract")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    idx_path = os.path.join(a.out, "index.json")
    index = {}
    if os.path.exists(idx_path):
        with open(idx_path, encoding="utf-8") as f:
            index = (json.load(f) or {}).get("by_sym") or {}
    fails = []
    for spec in SPECS:
        if only and spec[0] not in only:
            continue
        try:
            index[spec[1]] = build_one(spec, a.out, a.backfill, now, today)
        except Exception as e:
            fails.append(spec[0])
            log(f"  {spec[0]}: FAILED ({e}); last good file kept")
        time.sleep(0.5)
    atomic(idx_path, json.dumps({"built_utc": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                                 "by_sym": index}, indent=1, sort_keys=True))
    log(f"done: {len(index)} commodities in the index, failed this run: {', '.join(fails) or 'none'}")
    if len(fails) > len(SPECS) // 2:
        sys.exit(1)


if __name__ == "__main__":
    main()

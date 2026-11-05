#!/usr/bin/env python3
"""NY Harbor ULSD (NYMEX HO, heating oil) futures curve, all listed months, from public delayed sources
(no login or API key). Standalone: Python stdlib only, no repo imports.

Sources (auto tries tradingview, then yahoo, until one returns at least --min-months contracts; one source per
run, never mixed):
  tradingview  TradingView public futures scanner: one request, all listed months (~10 min delayed)
  yahoo        Yahoo per-contract charts (HOX26.NYM, ...): next ~24 months, one small request each, paced
  wsj          WSJ "HO00 contracts" page: one request, all listed months. Manual only (--source wsj): it works
               from a normal connection but WSJ answers GitHub Actions runners with HTTP 401, so not in auto.
The `source` column records which one was used. Rows without a price are dropped (never filled). If no source
returns enough months the script exits non-zero and writes nothing, so the last good file stays.

  python scripts/ho_curve.py --out data/ho_curve.csv [--history data/ho_curve_history.csv] [--source auto|tradingview|wsj|yahoo]
  python scripts/ho_curve.py --out data/ho_curve.csv --history data/ho_curve_history.csv --merge-only
  python scripts/ho_curve.py --probe     # one request per candidate site, prints what responds; writes nothing
"""
import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
HTML_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
MONTH_CODES = "FGHJKMNQUVXZ"  # Jan..Dec
FIELDS = ["contract", "month", "last", "change", "open", "high", "low", "volume", "prior_settle",
          "source", "fetched_at_utc"]
CHAIN = ["tradingview", "yahoo"]  # auto order; wsj is manual-only (401 from GitHub Actions)


def get(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or HTML_HEADERS,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def num(v, nd=4):
    """Number -> fixed-decimal string; anything else (None, '-', '', NaN) -> '' (blank, never invented)."""
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, str):
        v = v.strip().replace(",", "")
        if v.lower() == "unch":
            v = "0"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return f"{f:.{nd}f}" if f == f else ""


def intstr(v):
    s = num(v, 0)
    return s if s else ""


def row(code, year, last, change, open_, high, low, volume, prior, source, fetched_at):
    return {"contract": f"HO{code}{year % 100:02d}", "month": f"{year:04d}-{MONTH_CODES.index(code) + 1:02d}",
            "last": num(last), "change": num(change), "open": num(open_), "high": num(high), "low": num(low),
            "volume": intstr(volume), "prior_settle": num(prior), "source": source, "fetched_at_utc": fetched_at}


# ---- TradingView -------------------------------------------------------------------------------------------
TV_URL = "https://scanner.tradingview.com/futures/scan"
TV_COLS = ["name", "close", "change_abs", "open", "high", "low", "volume", "close[1]", "exchange"]
TV_NAME = re.compile(r"^HO([FGHJKMNQUVXZ])(\d{4})$")


def src_tradingview(fetched_at):
    body = {"filter": [{"left": "exchange", "operation": "equal", "right": "NYMEX"},
                       {"left": "name", "operation": "match", "right": "^HO[FGHJKMNQUVXZ][0-9]{4}$"}],
            "symbols": {"query": {"types": []}}, "columns": TV_COLS, "range": [0, 200]}
    payload = json.loads(get(TV_URL, json.dumps(body).encode(), {
        "User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json",
        "Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"}))
    out = []
    for item in payload.get("data") or []:
        d = dict(zip(TV_COLS, item.get("d") or []))
        m = TV_NAME.match(str(d.get("name") or ""))
        if m and d.get("exchange") == "NYMEX":
            out.append(row(m.group(1), int(m.group(2)), d.get("close"), d.get("change_abs"), d.get("open"),
                           d.get("high"), d.get("low"), d.get("volume"), d.get("close[1]"),
                           "tradingview_scanner", fetched_at))
    return out


# ---- WSJ ---------------------------------------------------------------------------------------------------
WSJ_URL = "https://www.wsj.com/market-data/quotes/futures/HO00/contracts"
WSJ_ROW = re.compile(r'<tr>\s*<td[^>]*>\s*<a href="[^"]*/quotes/futures/HO([FGHJKMNQUVXZ])(\d\d)"[^>]*>.*?</tr>', re.S)
WSJ_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def src_wsj(fetched_at):
    page = get(WSJ_URL)
    i = page.find('id="cr_contracts_table"')
    if i < 0:
        raise RuntimeError("contracts table not found (page layout changed or request blocked)")
    table = page[i:page.find("</table>", i)]
    head = [re.sub(r"<[^>]+>", "", h).strip().upper() for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
    want = ["MONTH", "LAST", "CHG", "OPEN", "HIGH", "LOW", "SETTLEMENT", "DATE", "VOLUME", "OPEN INT", "DATE", "TIME"]
    if head != want:
        raise RuntimeError(f"unexpected columns {head}")
    out = []
    for m in WSJ_ROW.finditer(table):
        c = [html.unescape(re.sub(r"<[^>]+>", "", x)).strip() for x in WSJ_CELL.findall(m.group(0))]
        if len(c) != len(want):
            continue
        code, yy = m.group(1), int(m.group(2))
        # SETTLEMENT is the latest settle (dated MM/DD); it is the *prior* settle only while today's has not
        # been published, i.e. its date differs from the quote date (MM/DD/YY). Otherwise leave it blank.
        settle_date, quote_date = c[7], c[10]
        prior = c[6] if settle_date and quote_date and not quote_date.startswith(settle_date + "/") else None
        out.append(row(code, 2000 + yy, c[1], c[2], c[3], c[4], c[5], c[8], prior, "wsj", fetched_at))
    return out


# ---- Yahoo -------------------------------------------------------------------------------------------------
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=1d&interval=1d"


def src_yahoo(fetched_at, months=24, pause=0.4):
    today = dt.date.today()
    out = []
    for k in range(months + 1):
        y, m0 = divmod(today.month - 1 + k, 12)
        year, code = today.year + y, MONTH_CODES[m0]
        sym = f"HO{code}{year % 100:02d}.NYM"
        if k:
            time.sleep(pause)
        try:
            res = json.loads(get(YAHOO_URL.format(sym), headers={"User-Agent": UA, "Accept": "application/json"}))
            res = (res.get("chart") or {}).get("result") or []
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue  # not listed / expired: skip, never fill
            raise
        if not res:
            continue
        meta, q = res[0].get("meta") or {}, ((res[0].get("indicators") or {}).get("quote") or [{}])[0]
        if meta.get("symbol") != sym or meta.get("instrumentType") != "FUTURE":
            continue
        ts = meta.get("regularMarketTime") or 0
        if time.time() - ts > 10 * 86400:
            continue  # stale (expired or not trading): skip
        last_of = lambda key: (q.get(key) or [None])[-1]
        out.append(row(code, year, meta.get("regularMarketPrice"), meta.get("fulldayChange"), last_of("open"),
                       meta.get("regularMarketDayHigh", last_of("high")),
                       meta.get("regularMarketDayLow", last_of("low")),
                       meta.get("regularMarketVolume", last_of("volume")), meta.get("chartPreviousClose"),
                       "yahoo", fetched_at))
    return out


SOURCES = {"tradingview": src_tradingview, "wsj": src_wsj, "yahoo": src_yahoo}


def clean(rows):
    seen, out = set(), []
    for r in rows:
        if r["last"] and r["contract"] not in seen:  # no price -> dropped
            seen.add(r["contract"])
            out.append(r)
    return sorted(out, key=lambda r: r["month"])


# ---- probe (testing only) ----------------------------------------------------------------------------------
PROBES = {
    "wsj": WSJ_URL,
    "investing": "https://www.investing.com/commodities/heating-oil-contracts",
    "barchart": "https://www.barchart.com/futures/quotes/HO*0/futures-prices",
    "globeandmail": "https://www.theglobeandmail.com/investing/markets/commodities/HO-FT/",
}


def probe():
    for name, url in PROBES.items():
        try:
            req = urllib.request.Request(url, headers=HTML_HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                code, body = r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            code, body = e.code, ""
        except Exception as e:
            print(f"{name}: error {type(e).__name__}: {e}")
            continue
        tickers = set(re.findall(r"quotes/futures/HO[FGHJKMNQUVXZ]\d\d\"|data-symbol=\"HO[FGHJKMNQUVXZ]\d\d\"|"
                                 r">HO[FGHJKMNQUVXZ]\d\d<|Heating Oil [A-Z][a-z]{2} 20\d\d", body))
        title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S)
        months = set(re.findall(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* ?'?(?:20)?2[6-9]\b", body))
        print(f"{name}: HTTP {code}, {len(body)} bytes, {len(tickers)} HO contract mentions, "
              f"{len(months)} distinct 'Mon YY' strings, {body.count('<tr')} table rows, "
              f"title={(title.group(1).strip()[:80] if title else '')!r}")
        time.sleep(1)


def write_atomic(path, rows):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def append_history(path, rows):
    """Append today's rows; a rerun on the same UTC day replaces that day's rows (no duplicates)."""
    day = rows[0]["fetched_at_utc"][:10]
    old = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            old = [r for r in csv.DictReader(f) if (r.get("fetched_at_utc") or "")[:10] != day]
    write_atomic(path, old + rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/ho_curve.csv", help="latest curve CSV (overwritten)")
    ap.add_argument("--history", help="history CSV to append to (optional)")
    ap.add_argument("--source", default="auto", choices=["auto", "tradingview", "yahoo", "wsj"],
                    help="auto = tradingview, then yahoo; or force one source (testing)")
    ap.add_argument("--min-months", type=int, default=6, help="a source counts only with at least this many months")
    ap.add_argument("--merge-only", action="store_true",
                    help="no fetch: append the existing --out CSV to --history (used by the publish retry loop)")
    ap.add_argument("--probe", action="store_true", help="test which candidate sites respond; writes nothing")
    a = ap.parse_args()

    if a.probe:
        probe()
        return 0
    if a.merge_only:
        if not a.history:
            ap.error("--merge-only needs --history")
        with open(a.out, newline="") as f:
            rows = list(csv.DictReader(f))
        if len(rows) < a.min_months:
            print(f"{a.out} has only {len(rows)} rows; history not touched", file=sys.stderr)
            return 3
        append_history(a.history, rows)
        print(f"merged {len(rows)} rows into {a.history}")
        return 0

    fetched_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for name in (CHAIN if a.source == "auto" else [a.source]):
        try:
            rows = clean(SOURCES[name](fetched_at))
        except Exception as e:  # network/HTTP/parse problems -> next source
            print(f"{name}: failed ({type(e).__name__}: {e})", file=sys.stderr)
            rows = []
            continue
        if len(rows) >= a.min_months:
            break
        print(f"{name}: only {len(rows)} months (< {a.min_months}); trying next source", file=sys.stderr)
        rows = []
    if not rows:
        print("no source returned enough months; nothing written", file=sys.stderr)
        return 3
    write_atomic(a.out, rows)
    if a.history:
        append_history(a.history, rows)
    print(f"{rows[0]['source']}: {len(rows)} months {rows[0]['month']}..{rows[-1]['month']} -> {a.out}"
          + (f" (+ {a.history})" if a.history else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

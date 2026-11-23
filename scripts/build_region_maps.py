"""Regional sector maps for the EMMi Markets Equities Monitor (Jerry 23 Nov 2026, t1592u-t1595u; Morning Pulse v1.10 maps).

Four treemaps, one per index, as structured data (the site draws them; no images):
  sp500      S&P 500 (500 names), box = market cap in USD (shares x price)          region us,   exchange NYSE
  eu80       Europe top 80 by market cap, box = market cap in EUR (FX from Yahoo)    region eu,   exchange Xetra clock
  nikkei225  Nikkei 225, box = market cap in JPY (shares x price)                    region asia, exchange Tokyo
  csi300     CSI 300, box = official CSI index weight drifted by the day's move      region asia, exchange Shanghai
Static constituents (sector, shares or weight) are in scripts/region_maps_universe.json, generated from the Morning Pulse
assets (rebuild after an index review). Prices: Yahoo Finance daily bars, last 5 sessions, fetched each intraday run.

Mode per map (same rule as the Pulse, lib/equity_regions.map_mode): the exchange is open now AND Yahoo has today's bar ->
'live' (or 'midday' in the Tokyo/Shanghai lunch break), each stock's latest price against its previous close; otherwise the
last settled session close-to-close ('close'). Never 'close' while the market is open with today's bar, never 'live' on
a stale bar. Label: 'Live 16:05 CET' / 'Midday 04:10 CET' / 'Close Mon 23 Nov' (+ ' · Tokyo shut' on a local holiday).
A map that cannot be priced keeps the previously published copy, flagged stale (never an empty or invented map).

Usage: python scripts/build_region_maps.py --out live/region_maps.json [--base data/region_maps.json] [--clock 2026-11-23T16:05]
Output: data/region_maps.json on the live-data branch (published by intraday.yml)."""
import argparse
import collections
import datetime as dt
import json
import os
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
UNIVERSE = os.path.join(HERE, "region_maps_universe.json")
ZRH = ZoneInfo("Europe/Zurich")
UTC = dt.timezone.utc

MAPS = {
    "sp500": dict(region="us", name="S&P 500", index="^GSPC", index_name="S&P 500", exch="nyse", size="Market cap, USD", ccy="USD"),
    "eu80": dict(region="eu", name="Europe top 80", index="^STOXX", index_name="Stoxx 600", exch="xetra", size="Market cap, EUR", ccy="EUR"),
    "nikkei225": dict(region="asia", name="Nikkei 225", index="^N225", index_name="Nikkei 225", exch="tse", size="Market cap, JPY", ccy="JPY"),
    "csi300": dict(region="asia", name="CSI 300", index="000300.SS", index_name="CSI 300", exch="sse", size="Index weight", ccy="CNY"),
}
ORDER = ("sp500", "eu80", "nikkei225", "csi300")
FXN = {"USD": "EURUSD=X", "GBp": "EURGBP=X", "GBP": "EURGBP=X", "CHF": "EURCHF=X", "DKK": "EURDKK=X", "SEK": "EURSEK=X", "NOK": "EURNOK=X"}
# exchange -> (tz, cash segments, holiday calendar key, place for 'shut' labels); same table as the Pulse
EXCHANGES = {
    "nyse": ("America/New_York", ((dt.time(9, 30), dt.time(16, 0)),), "NYSE", "New York"),
    "xetra": ("Europe/Berlin", ((dt.time(9, 0), dt.time(17, 30)),), "XETRA", "Frankfurt"),
    "tse": ("Asia/Tokyo", ((dt.time(9, 0), dt.time(11, 30)), (dt.time(12, 30), dt.time(15, 30))), "JPX", "Tokyo"),
    "sse": ("Asia/Shanghai", ((dt.time(9, 30), dt.time(11, 30)), (dt.time(13, 0), dt.time(15, 0))), "SSE", "Shanghai"),
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_universe(path=UNIVERSE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ session clock (pure; tested)
class Clock:
    def __init__(self, holidays):
        self.h = {k: set(v) for k, v in (holidays or {}).items()}

    def holiday(self, exch, day):
        return day.isoformat() in self.h.get(EXCHANGES[exch][2], set())

    def local_day(self, exch, now):
        return now.astimezone(ZoneInfo(EXCHANGES[exch][0])).date()

    def state(self, exch, now):
        tz, segs, _, _ = EXCHANGES[exch]
        loc = now.astimezone(ZoneInfo(tz))
        if loc.weekday() >= 5:
            return "weekend"
        if self.holiday(exch, loc.date()):
            return "holiday"
        t = loc.time()
        if t < segs[0][0]:
            return "preopen"
        for a, b in segs:
            if a <= t < b:
                return "live"
        return "close" if t >= segs[-1][1] else "midday"

    def mode(self, exch, last_bar, now):
        """('live'|'midday'|'close', session date or None for 'use the bar before')."""
        st, today = self.state(exch, now), self.local_day(exch, now)
        if st in ("live", "midday") and last_bar == today:
            return st, last_bar
        if last_bar == today and st == "preopen":
            return "close", None
        return "close", last_bar

    def label(self, exch, mode, session, now):
        z = now.astimezone(ZRH)
        tzn = z.strftime("%Z")
        if mode == "live":
            return f"Live {z:%H:%M} {tzn}"
        if mode == "midday":
            return f"Midday {z:%H:%M} {tzn}"
        lab = f"Close {session:%a} {session.day} {session:%b}" if session else "Close"
        today = self.local_day(exch, now)
        if today.weekday() < 5 and self.holiday(exch, today) and (session is None or session < today):
            lab += f" \u00b7 {EXCHANGES[exch][3]} shut"
        return lab


# ------------------------------------------------------------------ Yahoo daily bars
_TL = threading.local()


def _session():
    s = getattr(_TL, "s", None)
    if s is None:
        try:
            from curl_cffi import requests as http
            s = http.Session(impersonate="chrome")
        except Exception:  # pragma: no cover
            import requests as http
            s = http.Session()
            s.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
        _TL.s = s
    return s


def bars(sym, tries=3):
    """[(local date, close)] for the last ~5 sessions (today's partial bar included while open), plus meta."""
    err = None
    for i in range(tries):
        host = ("query1", "query2")[i % 2]
        try:
            r = _session().get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}",
                               params=dict(range="5d", interval="1d", includePrePost="false"), timeout=20)
            if r.status_code == 404:
                return None, None
            if r.status_code >= 400:
                raise IOError(f"HTTP {r.status_code}")
            res = ((r.json().get("chart") or {}).get("result") or [None])[0]
            if not res or not res.get("timestamp"):
                return None, None
            return series(res)
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.5 * (i + 1) + random.random())
    log(f"  {sym}: {err}")
    return None, None


def series(res):
    """Yahoo chart result -> ([(local date, close)], meta). Pure; tested."""
    meta = res.get("meta") or {}
    tz = ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")
    closes = res["indicators"]["quote"][0].get("close") or []
    out = collections.OrderedDict()
    for t, c in zip(res["timestamp"], closes):
        if c is not None:
            out[dt.datetime.fromtimestamp(t, tz).date()] = float(c)
    # Yahoo's 5d daily series can drop the latest session around local midnight (seen 24 Nov 2026 00:20 CET:
    # Xetra/Euronext/SIX names ended on Fri 20 Nov while meta had Monday's close). The meta's last regular
    # trade then fills that day.
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp:
        d = dt.datetime.fromtimestamp(rmt, tz).date()
        if not out or d > next(reversed(out)):
            out[d] = float(rmp)
    return list(out.items()), meta


def fetch_all(syms, workers=12):
    px, meta = {}, {}
    with ThreadPoolExecutor(workers) as ex:
        for s, (b, m) in zip(syms, ex.map(bars, syms)):
            if b:
                px[s], meta[s] = b, m or {}
    return px, meta


# ------------------------------------------------------------------ one map (pure given prices; tested)
def _at(b, d):
    for x, v in b or ():
        if x == d:
            return v
    return None


def _before(b, d):
    prev = [(x, v) for x, v in (b or ()) if x < d]
    return prev[-1] if prev else (None, None)


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def build_map(key, rows, px, meta, clock, now, sector_short=None, weight_date=None):
    cfg = MAPS[key]
    exch = cfg["exch"]
    ib = px.get(cfg["index"])
    if ib:
        last = ib[-1][0]
    else:
        lasts = [px[r[0]][-1][0] for r in rows if px.get(r[0])]
        if not lasts:
            raise RuntimeError(f"{cfg['name']}: nothing priced")
        last = collections.Counter(lasts).most_common(1)[0][0]
    mode, sd = clock.mode(exch, last, now)
    if sd is None:                                   # stray pre-open bar: the session before it
        ref = ib or next((px[r[0]] for r in rows if px.get(r[0]) and len(px[r[0]]) > 1), None)
        sd = _before(ref, last)[0] if ref else last
    fx = {}
    if key == "eu80":
        for c, s in FXN.items():
            b = px.get(s)
            v = None
            if b:
                cand = [y for x, y in b if x <= sd]
                v = cand[-1] if cand else b[-1][1]
            fx[c] = v * (100 if c == "GBp" else 1) if v else None
        fx["EUR"] = 1.0
    items, prevs = [], []
    for yf, label, full, sector, size, cur in rows:
        b = px.get(yf)
        end = _at(b, sd)
        pd_, start = _before(b, sd)
        if end is None or not start:
            continue
        prevs.append(pd_)
        chg = (end / start - 1) * 100
        if key == "csi300":
            cap = float(size) * (end / start)
        elif key == "eu80":
            f = fx.get(cur)
            if not f:
                continue
            cap = float(size) * end / f
        else:
            cap = float(size) * end
        m = meta.get(yf) or {}
        items.append(dict(yf=yf, t=label, n=m.get("longName") or m.get("shortName") or full, s=sector, c=chg, cap=cap, p=end, cur=cur))
    if len(items) < max(5, len(rows) // 3):
        raise RuntimeError(f"{cfg['name']}: only {len(items)}/{len(rows)} priced for {sd}")
    total = sum(x["cap"] for x in items)
    by = collections.defaultdict(list)
    for x in items:
        by[x["s"]].append(x)
    sectors = []
    for s, g in by.items():
        w0 = [x["cap"] / (1 + x["c"] / 100) for x in g]
        ch = sum(w * x["c"] for w, x in zip(w0, g)) / sum(w0)
        g.sort(key=lambda x: -x["cap"])
        cap = sum(x["cap"] for x in g)
        sh = (sector_short or {}).get(s) or [s, s]
        sectors.append(dict(name=s, short=sh[0], tiny=sh[1], chg=_r(ch), wt=_r(cap / total * 100, 1), n=len(g),
                            items=[[x["yf"], x["t"], x["n"], _r(x["c"]), _r(x["cap"] / total * 100, 3), _r(x["p"], 4),
                                    _r(x["cap"] / 1e9, 2) if key != "csi300" else None] for x in g]))
    sectors.sort(key=lambda s: -s["wt"])
    w0 = [x["cap"] / (1 + x["c"] / 100) for x in items]
    basket = sum(w * x["c"] for w, x in zip(w0, items)) / sum(w0)
    idx = None
    if ib:
        e, (_, s0) = _at(ib, sd), _before(ib, sd)
        if e is not None and not s0 and len(ib) == 1 and (meta.get(cfg["index"]) or {}).get("chartPreviousClose"):
            s0 = float(meta[cfg["index"]]["chartPreviousClose"])     # Yahoo's CSI 300 daily can carry only the latest bar
        idx = (e / s0 - 1) * 100 if e is not None and s0 else None
    prev = collections.Counter(prevs).most_common(1)[0][0] if prevs else None
    srt = sorted(items, key=lambda x: x["c"])
    up, dn = sum(1 for x in items if x["c"] > 0), sum(1 for x in items if x["c"] < 0)
    size_txt = cfg["size"] + (f" ({weight_date[6:8].lstrip('0')} {dt.date(int(weight_date[:4]), int(weight_date[4:6]), 1):%b} weights)"
                              if key == "csi300" and weight_date else "")
    return dict(key=key, region=cfg["region"], name=cfg["name"], index_sym=cfg["index"], index_name=cfg["index_name"], exch=exch,
                mode=mode, label=clock.label(exch, mode, sd, now), session=sd.isoformat(), prev_session=prev.isoformat() if prev else None,
                basis="vs previous close" if mode != "close" else (f"{prev:%-d %b} \u2192 {sd:%-d %b}" if prev else f"to {sd:%-d %b}"),
                size=size_txt, ccy=cfg["ccy"], n=len(items), N=len(rows), index_chg=_r(idx), basket_chg=_r(basket),
                adv=up, dec=dn, unch=len(items) - up - dn, built_utc=now.astimezone(UTC).isoformat(timespec="seconds"),
                best=[[x["t"], _r(x["c"], 1)] for x in srt[-3:][::-1]], worst=[[x["t"], _r(x["c"], 1)] for x in srt[:3]],
                missing=sorted({r[0] for r in rows} - {x["yf"] for x in items})[:30], sectors=sectors, stale=False)


def build(now=None, base=None, keys=ORDER, workers=12, universe=None):
    U = universe or load_universe()
    now = now or dt.datetime.now(UTC)
    clock = Clock(U.get("holidays"))
    syms = []
    for k in keys:
        syms += [r[0] for r in U["maps"][k]] + [MAPS[k]["index"]]
        if k == "eu80":
            syms += sorted(set(FXN.values()))
    syms = list(dict.fromkeys(syms))
    t0 = time.time()
    px, meta = fetch_all(syms, workers)
    log(f"region maps: {len(px)}/{len(syms)} symbols priced in {time.time() - t0:.1f}s")
    old = (base or {}).get("maps") or {}
    maps = {}
    for k in keys:
        try:
            maps[k] = build_map(k, U["maps"][k], px, meta, clock, now, U.get("sector_short"), U.get("csi300_weight_date"))
            log(f"  {k}: {maps[k]['label']} {maps[k]['n']}/{maps[k]['N']} idx {maps[k]['index_chg']}")
        except Exception as e:  # noqa: BLE001
            log(f"  {k} failed: {e}")
            if k in old:
                maps[k] = dict(old[k], stale=True)
    return dict(schema=1, generated_utc=now.astimezone(UTC).isoformat(timespec="seconds"),
                source="Yahoo Finance daily bars (prices about 15 minutes delayed); constituents and sectors from scripts/region_maps_universe.json",
                order=list(keys), maps=maps, fetch=dict(symbols=len(syms), priced=len(px), seconds=round(time.time() - t0, 1)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", help="previously published region_maps.json (a failed map keeps that copy, flagged stale)")
    ap.add_argument("--clock", help="Zurich wall clock YYYY-MM-DDTHH:MM (tests / re-issues)")
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()
    base = None
    if a.base and os.path.exists(a.base):
        try:
            base = json.load(open(a.base))
        except Exception:  # noqa: BLE001
            base = None
    now = dt.datetime.fromisoformat(a.clock).replace(tzinfo=ZRH) if a.clock else None
    out = build(now=now, base=base, workers=a.workers)
    if not out["maps"]:
        log("no map built and no previous copy: nothing written")
        sys.exit(1)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log(f"wrote {a.out} ({os.path.getsize(a.out) // 1024} KB): " + ", ".join(f"{k} {m['label']}{' (stale)' if m.get('stale') else ''}" for k, m in out["maps"].items()))


if __name__ == "__main__":
    main()

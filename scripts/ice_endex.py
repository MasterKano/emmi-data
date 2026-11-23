"""Dutch TTF gas futures from ICE Endex (Amsterdam), the main European market, via ICE's public product-page data.

These are the same keyless JSON endpoints the public page https://www.ice.com/products/27996665/Dutch-TTF-Natural-Gas-Futures/data
loads in a browser. Prices are ICE's delayed quotes (about 10 minutes behind), EUR/MWh.

  contract-data  every listed month: last price, volume, last trade time (GMT), % change vs the prior settlement
  historical     daily settlements of one month (about two years with span=3)
  current-day    10-minute prices for the current session (Sunday-evening start for Monday; times in GMT)

quote(label) returns one contract month ('Dec-26') with the last price, prior settlement, today's open/high/low from the
10-minute prices, and the daily settlement history. Plain stdlib (urllib), so the morning brief can import it as-is."""
import datetime as dt
import json
import urllib.request
from zoneinfo import ZoneInfo

PRODUCT_ID, HUB_ID = 4331, 7979          # ICE Endex Dutch TTF Natural Gas Futures (TFM)
BASE = "https://www.ice.com/marketdata/api/productguide/charting/"
PAGE = "https://www.ice.com/products/27996665/Dutch-TTF-Natural-Gas-Futures/data"
AMS = ZoneInfo("Europe/Amsterdam")
UTC = dt.timezone.utc
SESSION_END = dt.time(18, 0)             # ICE Endex TTF futures close 18:00 Amsterdam; settlement window ~16:00-16:15 London
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def _get(path, timeout=20):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": UA, "Accept": "application/json", "Referer": PAGE})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    if not body.lstrip().startswith(("[", "{")):
        raise IOError("ICE returned a non-JSON page (blocked or changed)")
    return json.loads(body)


def label_of(strip):
    """'Dec26' -> 'Dec-26' (the month label EMMi and the brief use)."""
    return strip[:3] + "-" + strip[3:]


def contracts():
    out = []
    for x in _get(f"contract-data?productId={PRODUCT_ID}&hubId={HUB_ID}"):
        t = dt.datetime.strptime(x["lastTime"].replace(" GMT", ""), "%m/%d/%Y %I:%M %p").replace(tzinfo=UTC) if x.get("lastTime") else None
        out.append(dict(market_id=x["marketId"], strip=x["marketStrip"], label=label_of(x["marketStrip"]),
                        last=x.get("lastPrice"), volume=x.get("volume"), last_time=t, change_pct=x.get("change")))
    return out


def _bars(js, fmt="%a %b %d %H:%M:%S %Y"):
    return [(dt.datetime.strptime(t, fmt).replace(tzinfo=UTC), float(p)) for t, p in (js.get("bars") or []) if p is not None]


def settlements(market_id, span=3):
    """[(date, settle)] oldest first. Bars are stamped 00:00 on the trade date."""
    return [(t.date(), p) for t, p in _bars(_get(f"data/historical?marketId={market_id}&historicalSpan={span}"))]


def intraday(market_id):
    """[(utc datetime, price)] 10-minute prices for the current session."""
    return _bars(_get(f"data/current-day?marketId={market_id}"))


def quote(label=None, span=3, now=None):
    """One TTF month from ICE Endex. label like 'Dec-26' (None = the first listed month). Raises on any failure."""
    now = now or dt.datetime.now(UTC)
    cs = contracts()
    if not cs:
        raise LookupError("ICE: no TTF contracts listed")
    c = next((x for x in cs if x["label"] == label), None) if label else cs[0]
    if c is None:
        raise LookupError(f"ICE: TTF {label} not listed (have {', '.join(x['label'] for x in cs[:3])})")
    if not isinstance(c["last"], (int, float)) or not c["last_time"]:
        raise LookupError(f"ICE: TTF {c['label']} has no last price")
    hist = settlements(c["market_id"], span)
    if len(hist) < 30:
        raise LookupError(f"ICE: only {len(hist)} TTF {c['label']} settlements")
    trade_date = c["last_time"].astimezone(AMS).date()
    prior = [(d, p) for d, p in hist if d < trade_date]
    today_settle = next((p for d, p in hist if d == trade_date), None)
    bars = []
    try:
        bars = [p for t, p in intraday(c["market_id"]) if t <= c["last_time"] + dt.timedelta(minutes=10)]
    except Exception:
        pass
    last = float(c["last"])
    close = today_settle if today_settle is not None else last
    o = bars[0] if bars else None
    hi = max(bars + [close]) if bars else None
    lo = min(bars + [close]) if bars else None
    end = dt.datetime.combine(trade_date, SESSION_END, AMS)
    state = "open" if today_settle is None and now < end and trade_date == now.astimezone(AMS).date() else "closed"
    return dict(
        label=c["label"], strip=c["strip"], market_id=c["market_id"],
        last=last, close=close, settled=today_settle is not None,
        last_time=c["last_time"], trade_date=trade_date, volume=c["volume"],
        prev_settle=prior[-1][1] if prior else None, prev_date=prior[-1][0] if prior else None,
        change_pct_ice=c["change_pct"], open=o, high=hi, low=lo, state=state, session_end=end,
        settlements=hist, exchange="ICE Endex", venue="ICE Endex (Amsterdam)", currency="EUR", unit="EUR/MWh",
        url=PAGE, src="ICE Endex delayed quote (ice.com product page data, about 10 minutes behind)")


if __name__ == "__main__":
    import sys
    q = quote(sys.argv[1] if len(sys.argv) > 1 else None)
    z = ZoneInfo("Europe/Zurich")
    print(f"TTF {q['label']} {q['last']:.3f} EUR/MWh at {q['last_time'].astimezone(z):%a %d %b %H:%M} Zurich "
          f"({q['state']}); prior settle {q['prev_settle']} on {q['prev_date']}; "
          f"{(q['close'] / q['prev_settle'] - 1) * 100:+.2f}%; O/H/L {q['open']}/{q['high']}/{q['low']}; vol {q['volume']}")

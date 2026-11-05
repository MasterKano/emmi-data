#!/usr/bin/env python3
"""Build data/market.json for the Table page.

Pulls ~10 years of daily bars per symbol from Yahoo Finance's public chart endpoint
(curl_cffi browser impersonation, query1/query2 hosts, retries with exponential backoff),
falling back to yfinance. Computes price returns, volume averages/ratios, moving-average
distances and a 1-year sparkline. Real data only: anything missing is written as null
(shown as n/a on the page) and listed in "errors".

Usage: python scripts/build_data.py [--out data/market.json] [--only SYM,SYM]
       python scripts/build_data.py --intraday --out live/market.json
  --intraday: prices only (no quoteSummary fundamentals). Mkt cap / EV are scaled from the last full build
  (data/market.json) by the price change, converted at fresh FX. Used by .github/workflows/intraday.yml.
"""
import argparse, csv, datetime as dt, io, json, re, math, os, random, sys, time
from zoneinfo import ZoneInfo

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from universe import GROUPS  # noqa: E402
import contracts  # noqa: E402

ROOT = os.path.dirname(HERE)
UTC = dt.timezone.utc
CET_TZ = ZoneInfo("Europe/Zurich")
NEWYORK = ZoneInfo("America/New_York")
LONG_CONT = ("2Y", "3Y", "5Y", "10Y")   # futures: only these horizons come from the continuous series (no single contract spans them)
HORIZONS = [("1W", dict(days=7)), ("1M", dict(months=1)), ("6M", dict(months=6)), ("1Y", dict(years=1)),
            ("2Y", dict(years=2)), ("3Y", dict(years=3)), ("5Y", dict(years=5)), ("10Y", dict(years=10))]
MAS = [10, 20, 50, 200]
VOLS = [5, 10, 20, 50]
MIN_OK_SHARE = 0.6   # refuse to overwrite the JSON if fewer than 60% of symbols fetched

try:
    from curl_cffi import requests as http
    SESSION = http.Session(impersonate="chrome")
except Exception:  # pragma: no cover
    import requests as http
    SESSION = http.Session()
    SESSION.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ fetching
def fetch_chart(sym, years=10, tries=6):
    """Yahoo v8 chart endpoint -> (DataFrame[close, volume] indexed by date, meta)."""
    p2 = int(time.time()) + 86400
    p1 = int((dt.datetime.now(UTC) - pd.DateOffset(years=years, days=25)).timestamp())
    last_err = None
    for i in range(tries):
        host = ("query1", "query2")[i % 2]
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}"
        try:
            r = SESSION.get(url, params=dict(period1=p1, period2=p2, interval="1d", includePrePost="false",
                                             events="split"), timeout=30)
            if r.status_code == 404:
                raise LookupError("404 not found")
            if r.status_code in (429, 500, 502, 503, 504) or r.status_code >= 400:
                raise IOError(f"HTTP {r.status_code}")
            js = r.json()
            res = (js.get("chart") or {}).get("result")
            if not res:
                raise LookupError(str((js.get("chart") or {}).get("error"))[:120])
            res = res[0]
            ts = res.get("timestamp") or []
            q = res["indicators"]["quote"][0]
            if not ts:
                raise LookupError("no timestamps")
            meta = res.get("meta", {})
            tz = meta.get("exchangeTimezoneName") or "UTC"
            idx = pd.to_datetime(ts, unit="s", utc=True).tz_convert(tz)
            df = pd.DataFrame({"close": q.get("close"), "volume": q.get("volume")}, index=idx)
            return df, meta
        except LookupError as e:
            last_err = e
            if i >= 1:   # one retry on the other host, then give up (symbol genuinely missing)
                break
        except Exception as e:
            last_err = e
        wait = min(60, 2 ** (i + 1)) + random.uniform(0, 1.5)
        log(f"  {sym}: {last_err} -> retry in {wait:.1f}s")
        time.sleep(wait)
    raise RuntimeError(f"chart endpoint failed: {last_err}")


def fetch_yf(sym, years=10):
    import yfinance as yf
    t = yf.Ticker(sym)
    h = t.history(period=f"{years}y", interval="1d", auto_adjust=False, actions=False)
    if h is None or h.empty:
        raise RuntimeError("yfinance returned no rows")
    df = pd.DataFrame({"close": h["Close"], "volume": h.get("Volume")}, index=h.index)
    meta = {}
    try:
        meta = t.history_metadata or {}
    except Exception:
        pass
    return df, meta


def session_state(meta, now_ts=None):
    """'open' / 'pre' / 'post' / 'closed' from the chart meta's currentTradingPeriod at fetch time."""
    now_ts = now_ts or time.time()
    try:
        p = meta["currentTradingPeriod"]
        for k, lab in (("regular", "open"), ("pre", "pre"), ("post", "post")):
            if p[k]["start"] <= now_ts < p[k]["end"]:
                return lab
        return "closed"
    except Exception:
        return None


def fill_last_close(df, meta):
    """Yahoo quirk: the chart API often leaves the latest daily close blank (NaN) for Nordic/LSE tickers while
    meta.regularMarketPrice already holds that session's price. Fill (or append) the bar for the quote's
    regularMarketTime date so the latest close and 1D % are not a day behind. Returns (df, filled?)."""
    px, ts = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    try:
        px = float(px)
    except Exception:
        return df, False
    if not ts or not (px > 0) or math.isnan(px):
        return df, False
    tz = meta.get("exchangeTimezoneName") or "UTC"
    t = pd.Timestamp(int(ts), unit="s", tz="UTC").tz_convert(tz)
    d = t.date()
    df = df.copy()
    if len(df) and df.index.tz is None:
        df.index = df.index.tz_localize(tz)
    pos = [i for i, x in enumerate(df.index) if x.tz_convert(tz).date() == d]
    if pos:
        p = pos[-1]
        c = df.iloc[p, df.columns.get_loc("close")]
        if session_state(meta) == "open":
            # session in progress: the quote is the latest print; the daily bar can lag it
            df.iloc[p, df.columns.get_loc("close")] = px
            v, qv = df.iloc[p, df.columns.get_loc("volume")], meta.get("regularMarketVolume")
            if qv and (v is None or pd.isna(v) or float(qv) > v):
                df.iloc[p, df.columns.get_loc("volume")] = float(qv)
            return df, bool(c is None or pd.isna(c))
        if c is None or pd.isna(c):
            df.iloc[p, df.columns.get_loc("close")] = px
            v = df.iloc[p, df.columns.get_loc("volume")]
            if (v is None or pd.isna(v)) and meta.get("regularMarketVolume"):
                df.iloc[p, df.columns.get_loc("volume")] = float(meta["regularMarketVolume"])
            return df, True
        return df, False
    if len(df) and df.index[-1].tz_convert(tz).date() > d:
        return df, False
    vol = meta.get("regularMarketVolume")
    row = pd.DataFrame({"close": [px], "volume": [float(vol) if vol else float("nan")]}, index=pd.DatetimeIndex([t]))
    return pd.concat([df, row]).sort_index(), True


CNBC_BARS = "https://ts-api.cnbc.com/harmony/app/bars/{c}/{t}/{a}/{b}/adjusted/EST5EDT.json"
CNBC_QUOTE = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"


def _cnbc_bars(code, typ, start, end):
    r = SESSION.get(CNBC_BARS.format(c=code, t=typ, a=start.strftime("%Y%m%d000000"), b=end.strftime("%Y%m%d000000")), timeout=30)
    if r.status_code != 200:
        raise IOError(f"CNBC bars HTTP {r.status_code}")
    pb = ((r.json() or {}).get("barData") or {}).get("priceBars") or []
    # a bar stamped at 00:00 New York time on day D holds the close of the session on D-1; weekend-mapped bars are dropped
    out = []
    for b in pb:
        if b.get("close") in (None, ""):
            continue
        d = pd.Timestamp(str(b["tradeTime"])[:8]) - pd.Timedelta(days=1)
        if typ == "1W" or d.weekday() < 5:
            out.append((d, float(b["close"])))
    return out


def fetch_cnbc(sym):
    """Government bond yields Yahoo does not carry (Bund, gilt, OAT, Swiss Confederation): CNBC's public quote and
    price-bar services (keyless, the same ones the self-drawn charts use). Daily closes reach back ~7 years; older
    history (for 10Y changes) comes from weekly closes. Yields in percent."""
    code = sym.split(":", 1)[1]
    now = dt.datetime.now(UTC)
    daily, d0 = [], None
    for yrs in (7, 5, 4, 3, 2):        # the service sometimes returns nothing for a long range: shorten and retry
        d0 = now - pd.DateOffset(years=yrs)
        try:
            daily = _cnbc_bars(code, "1D", d0, now + dt.timedelta(days=1))
        except Exception as e:
            log(f"  {sym}: {e}")
        if daily:
            break
        time.sleep(1.0)
    if not daily:
        raise LookupError("CNBC: no daily bars")
    d0 = daily[0][0]
    weekly = []
    try:
        weekly = [x for x in _cnbc_bars(code, "1W", now - pd.DateOffset(years=10, days=40), d0 + dt.timedelta(days=7)) if x[0] < daily[0][0]]
    except Exception as e:
        log(f"  {sym}: weekly history skipped ({e})")
    rows = weekly + daily
    df = pd.DataFrame({"close": [c for _, c in rows], "volume": [float("nan")] * len(rows)}, index=pd.DatetimeIndex([t for t, _ in rows]))
    meta = dict(currency=None, exchangeName="CNBC", exchangeTimezoneName="UTC")
    try:
        q = SESSION.get(CNBC_QUOTE, params=dict(symbols=code, requestMethod="itv", noform=1, partnerId=2, fund=1, exthrs=1, output="json"), timeout=20).json()
        q = q["FormattedQuoteResult"]["FormattedQuote"][0]
        last = float(str(q["last"]).rstrip("%"))
        t = pd.Timestamp(q["last_time"]).tz_convert("UTC")
        meta.update(regularMarketPrice=last, regularMarketTime=int(t.timestamp()), longName=q.get("name"))
        d = t.tz_convert("Europe/Zurich").tz_localize(None).normalize()
        while d.weekday() >= 5:            # weekend prints belong to Friday's session
            d -= pd.Timedelta(days=1)
        df = df[df.index <= d]
        if len(df) and df.index[-1] == d:
            df.iloc[-1, 0] = last
        else:
            df.loc[d] = [last, float("nan")]
        # bar dating differs between CNBC series; the quote's previous close anchors the 1D change
        prev = float(str(q.get("previous_day_closing") or "").rstrip("%") or "nan")
        if prev > 0 and len(df) >= 2:
            df.iloc[-2, 0] = prev
    except Exception as e:
        log(f"  {sym}: CNBC quote skipped ({e})")
    return df.sort_index(), meta


# ------------------------------------------------------------------ official 10-year government yields
# Daily closes, history, 1D/1M/1Y changes and spreads come from the official publishers below (keyless). CNBC supplies only
# the last intraday quote ("live"); when an official series fails or lags by more than OFFICIAL_MAX_LAG days, the row falls
# back to CNBC daily closes and is tagged "fallback" with the reason.
OFFICIAL_MAX_LAG = 10
BBK_KEY = "BBSIS.D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A"
OFFICIAL = {
    "^TNX": dict(cnbc="US10Y", id="ust", name="US Treasury", series="Daily Treasury Par Yield Curve, 10 Yr",
                 url="https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve&field_tdr_date_value={y}"),
    "CNBC:DE10Y-DE": dict(cnbc="DE10Y-DE", id="bbk", name="Deutsche Bundesbank", series="Federal securities yield curve (Svensson), residual maturity 10 years",
                          url="https://www.bundesbank.de/dynamic/action/en/statistics/time-series-databases/time-series-databases/759784/759784?tsId=" + BBK_KEY),
    "CNBC:GB10Y-GB": dict(cnbc="GB10Y-GB", id="boe", name="Bank of England", series="IADB IUDMNPY: 10-year nominal par yield (gilts)",
                          url="https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?SeriesCodes=IUDMNPY&UsingCodes=Y&html.x=1&Datefrom=01/Jan/{y}&Dateto=now"),
    "CNBC:FR10Y-FR": dict(cnbc="FR10Y-FR", id="bdf", name="Banque de France", series="TEC10 (10-year constant maturity rate)",
                          url="https://webstat.banque-france.fr/en/catalog/fm/FM.M.FR.EUR.FR2.BB.FRMOYTEC10.HSTA"),
    "CNBC:CH10Y-CH": dict(cnbc="CH10Y-CH", id="snb", name="Swiss National Bank", series="data.snb.ch cube rendoblid, Confederation spot rate 10 years",
                          url="https://data.snb.ch/en/topics/ziredev/cube/rendoblid"),
}


def _series(pairs):
    pairs = sorted((pd.Timestamp(d), float(v)) for d, v in pairs)
    return pd.DataFrame({"close": [v for _, v in pairs], "volume": [float("nan")] * len(pairs)},
                        index=pd.DatetimeIndex([d for d, _ in pairs]))


def off_ust(start):
    rows = []
    for y in range(start.year, dt.date.today().year + 1):
        r = SESSION.get("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
                        f"{y}/all", params=dict(type="daily_treasury_yield_curve", field_tdr_date_value=y, _format="csv"), timeout=40)
        if r.status_code != 200:
            raise IOError(f"Treasury {y}: HTTP {r.status_code}")
        rd = csv.DictReader(io.StringIO(r.text))
        for x in rd:
            v = (x.get("10 Yr") or "").strip()
            if v:
                rows.append((dt.datetime.strptime(x["Date"], "%m/%d/%Y"), v))
        time.sleep(0.3)
    return rows


def off_bbk(start):
    r = SESSION.get(f"https://api.statistiken.bundesbank.de/rest/data/BBSIS/{BBK_KEY.split('.', 1)[1]}",
                    params=dict(format="csv", startPeriod=start.strftime("%Y-%m-%d"), lang="en"), timeout=60)
    if r.status_code != 200:
        raise IOError(f"Bundesbank HTTP {r.status_code}")
    out = []
    for ln in r.text.splitlines():
        m = re.match(r"^(\d{4}-\d{2}-\d{2}),(-?\d+(?:\.\d+)?),", ln)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def off_boe(start):
    r = SESSION.get("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp",
                    params={"csv.x": "yes", "Datefrom": start.strftime("%d/%b/%Y"), "Dateto": "now", "SeriesCodes": "IUDMNPY",
                            "CSVF": "TN", "UsingCodes": "Y", "VPD": "Y", "VFD": "N"}, timeout=60)
    if r.status_code != 200 or "IUDMNPY" not in r.text[:200]:
        raise IOError(f"Bank of England HTTP {r.status_code}")
    return [(dt.datetime.strptime(a, "%d %b %Y"), b) for a, b in (ln.split(",")[:2] for ln in r.text.splitlines()[1:] if "," in ln) if b.strip()]


def off_bdf(start):
    # Webstat's observation API needs a registered key, and the TEC10 terms do not allow redistribution, so there is no
    # keyless official daily series to publish. The ECB's monthly long-term rate for France is the closest open reference.
    raise LookupError("no keyless official daily series (Banque de France Webstat API needs a key)")


def off_snb(start):
    r = SESSION.get("https://data.snb.ch/api/cube/rendoblid/data/csv/en",
                    params={"dimSel": "D0(10J0)", "fromDate": start.strftime("%Y-%m-%d")}, timeout=60)
    if r.status_code != 200:
        raise IOError(f"SNB HTTP {r.status_code}")
    out = []
    for ln in r.text.splitlines():
        m = re.match(r'^"(\d{4}-\d{2}-\d{2})";"10J0";"(-?[\d.]+)"', ln)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


OFF_FETCH = dict(ust=off_ust, bbk=off_bbk, boe=off_boe, bdf=off_bdf, snb=off_snb)


def ecb_fr_monthly():
    """ECB Data Portal: long-term interest rate for convergence purposes, France (10-year, monthly average)."""
    try:
        r = SESSION.get("https://data-api.ecb.europa.eu/service/data/IRS/M.FR.L.L40.CI.0000.EUR.N.Z",
                        params=dict(lastNObservations=1, format="csvdata"), timeout=30)
        x = list(csv.DictReader(io.StringIO(r.text)))[-1]
        return dict(name="ECB Data Portal", series="IRS.M.FR.L.L40.CI.0000.EUR.N.Z (monthly average)", month=x["TIME_PERIOD"],
                    v=float(x["OBS_VALUE"]), url="https://data.ecb.europa.eu/data/datasets/IRS/IRS.M.FR.L.L40.CI.0000.EUR.N.Z")
    except Exception as e:
        log(f"  ECB FR monthly: {e}")
        return None


def cnbc_live(code):
    """CNBC last quote for the intraday move only (not used for closes, changes or spreads of official rows)."""
    try:
        q = SESSION.get(CNBC_QUOTE, params=dict(symbols=code, requestMethod="itv", noform=1, partnerId=2, fund=1, exthrs=1, output="json"),
                        timeout=20).json()["FormattedQuoteResult"]["FormattedQuote"][0]
        last, prev = float(str(q["last"]).rstrip("%")), float(str(q["previous_day_closing"]).rstrip("%"))
        return dict(px=round(last, 4), bp=round((last - prev) * 100, 1), t=pd.Timestamp(q["last_time"]).tz_convert("UTC").isoformat(),
                    src="CNBC", url="https://www.cnbc.com/quotes/" + code)
    except Exception as e:
        log(f"  CNBC live {code}: {e}")
        return None


def fetch_official(sym):
    """-> (df, meta, src, extra). Official closes; CNBC daily closes (tagged fallback) if the official series fails or lags."""
    o = OFFICIAL[sym]
    start = (dt.datetime.now(UTC) - pd.DateOffset(years=10, days=40)).date()
    y = dt.date.today().year
    extra = dict(official=dict(id=o["id"], name=o["name"], series=o["series"], url=o["url"].format(y=y)), live=cnbc_live(o["cnbc"]), fallback=None)
    if o["id"] == "bdf":
        extra["official_ref"] = ecb_fr_monthly()
    try:
        rows = OFF_FETCH[o["id"]](start)
        if not rows:
            raise LookupError("no observations")
        df = _series(rows)
        lag = (pd.Timestamp(dt.date.today()) - df.index[-1]).days
        extra["official"]["asof"] = df.index[-1].strftime("%Y-%m-%d")
        if lag > OFFICIAL_MAX_LAG:
            raise LookupError(f"latest official close {df.index[-1]:%Y-%m-%d} is {lag} days old")
        return clean(df), dict(currency=None, exchangeName=o["name"], exchangeTimezoneName="UTC"), o["id"], extra
    except Exception as e:
        reason = str(e)[:160]
        log(f"  {sym}: official {o['id']} unavailable ({reason}); CNBC fallback")
        df, meta = fetch_cnbc("CNBC:" + o["cnbc"])
        extra["fallback"] = reason
        return clean(df), meta, "cnbc", extra


def fetch(sym):
    if sym.startswith("CNBC:"):
        df, meta = fetch_cnbc(sym)
        return clean(df), meta, "cnbc"
    try:
        df, meta = fetch_chart(sym)
        src = "yahoo-chart"
    except Exception as e1:
        log(f"  {sym}: chart failed ({e1}); trying yfinance")
        try:
            df, meta = fetch_yf(sym)
            src = "yfinance"
        except Exception as e2:
            raise RuntimeError(f"{e1} | yfinance: {e2}")
    df, filled = fill_last_close(df, meta or {})
    if filled:
        meta = dict(meta, _last_from_quote=True)
    return clean(df), meta, src


def clean(df):
    df = df.copy()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df[df["close"].notna() & (df["close"] > 0)]
    # one row per local trading date (Yahoo sometimes repeats the live bar)
    df["d"] = [t.date() for t in df.index]
    df = df.groupby("d").tail(1)
    df.index = pd.to_datetime(df.pop("d"))
    # drop isolated bad prints (e.g. GBp/GBP 100x glitches): >4x away from both neighbours' median
    if len(df) > 10:
        med = df["close"].rolling(7, center=True, min_periods=3).median()
        bad = (df["close"] / med > 4) | (df["close"] / med < 0.25)
        df = df[~bad]
    return df




# ------------------------------------------------------------------ fundamentals (mcap / EV) + FX -> EUR
MAJOR = {"GBp": "GBP", "ILA": "ILS", "ZAc": "ZAR", "KWF": "KWD"}  # Yahoo minor units -> FX major
FX_CCYS = ("USD", "NOK", "SEK", "DKK", "GBP", "CAD", "CHF", "JPY", "HKD", "AUD", "SGD", "NZD", "PLN", "CZK", "HUF", "TRY", "MXN", "BRL", "INR", "KRW", "TWD", "CNY", "ZAR", "ILS")


def yahoo_crumb(session):
    """Cookie + crumb for Yahoo quoteSummary (chart API does not need it)."""
    try:
        session.get("https://fc.yahoo.com", timeout=15)
        r = session.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15)
        if r.status_code == 200 and r.text and " " not in r.text and "<" not in r.text:
            return r.text.strip()
    except Exception as e:
        log(f"crumb: {e}")
    return None


def _raw(d, key):
    if not d:
        return None
    v = d.get(key)
    if isinstance(v, dict):
        v = v.get("raw")
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def minor_unit_cap(mcap, ccy, px, shares):
    """Yahoo reports marketCap in the major unit (GBP) even when the quote is in a minor unit (GBp pence).
    Guard against the occasional small-cap record where the cap was computed from the pence price (100x too
    high): if cap / (shares x price in major units) is ~100, scale it back. -> (mcap, fixed?)"""
    if mcap is None or ccy not in MAJOR or not px or not shares:
        return mcap, False
    r = mcap / (shares * px / 100.0)
    if 50 < r < 200:
        return mcap / 100.0, True
    return mcap, False


def fetch_fundamentals(sym, crumb, tries=4):
    """Yahoo quoteSummary -> (marketCap, enterpriseValue, currency) or raise."""
    last_err = None
    for i in range(tries):
        host = ("query1", "query2")[i % 2]
        url = f"https://{host}.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
        try:
            params = dict(modules="price,defaultKeyStatistics,summaryDetail,financialData")
            if crumb:
                params["crumb"] = crumb
            r = SESSION.get(url, params=params, timeout=25)
            if r.status_code in (401, 403) and i == 0:
                # refresh crumb once
                crumb = yahoo_crumb(SESSION)
                params["crumb"] = crumb
                r = SESSION.get(url, params=params, timeout=25)
            if r.status_code in (429, 500, 502, 503, 504) or r.status_code >= 400:
                raise IOError(f"HTTP {r.status_code}")
            res = ((r.json().get("quoteSummary") or {}).get("result") or [None])[0]
            if not res:
                raise LookupError(str((r.json().get("quoteSummary") or {}).get("error"))[:120])
            price = res.get("price") or {}
            ks = res.get("defaultKeyStatistics") or {}
            sd = res.get("summaryDetail") or {}
            mcap = _raw(price, "marketCap") or _raw(sd, "marketCap")
            ev = _raw(ks, "enterpriseValue")
            ccy = price.get("currency") or sd.get("currency")
            fccy = (res.get("financialData") or {}).get("financialCurrency")
            mcap, fixed = minor_unit_cap(mcap, ccy, _raw(price, "regularMarketPrice"),
                                         _raw(ks, "sharesOutstanding") or _raw(ks, "impliedSharesOutstanding"))
            if fixed:
                log(f"  {sym}: marketCap looked like {ccy} (minor unit) x shares; scaled /100")
                ev = None          # Yahoo EV shares the same basis; rebuild from components instead
            return mcap, ev, ccy, fccy
        except LookupError as e:
            last_err = e
            break
        except Exception as e:
            last_err = e
            time.sleep(min(20, 2 ** (i + 1)) + random.uniform(0, 0.8))
    raise RuntimeError(f"fundamentals failed: {last_err}")


BAL_FIELDS = ("TotalDebt", "CashCashEquivalentsAndShortTermInvestments", "CashAndCashEquivalents", "NetDebt",
              "MinorityInterest")


def parse_timeseries(js):
    """fundamentals-timeseries JSON -> ({type: {asOfDate: value}}, dominant currency)."""
    out, ccys = {}, {}
    for it in ((js or {}).get("timeseries") or {}).get("result") or []:
        t = ((it.get("meta") or {}).get("type") or [None])[0]
        pts = [x for x in (it.get(t) or []) if x and x.get("asOfDate") and _raw(x, "reportedValue") is not None]
        if not t or not pts:
            continue
        out[t] = {x["asOfDate"]: _raw(x, "reportedValue") for x in pts}
        for x in pts:
            if x.get("currencyCode"):
                ccys[x["currencyCode"]] = ccys.get(x["currencyCode"], 0) + 1
    return out, (max(ccys, key=ccys.get) if ccys else None)


def ts_latest(ts, field):
    """Most recent reported point across quarterly and annual series -> (value, date)."""
    best = (None, None)
    for p in ("annual", "quarterly"):          # quarterly wins ties
        s = ts.get(p + field) or {}
        if s:
            d = max(s)
            if best[1] is None or d >= best[1]:
                best = (s[d], d)
    return best


def balance_from_ts(ts):
    """Latest net debt (total debt - cash & ST investments; Yahoo NetDebt fallback) and minority interest."""
    debt, d = ts_latest(ts, "TotalDebt")
    cash, _ = ts_latest(ts, "CashCashEquivalentsAndShortTermInvestments")
    if cash is None:
        cash, _ = ts_latest(ts, "CashAndCashEquivalents")
    nd = (debt - cash) if debt is not None and cash is not None else ts_latest(ts, "NetDebt")[0]
    if nd is None and debt is None and cash is not None:
        # no debt line at all (Yahoo omits TotalDebt for debt-free small caps): net cash = -cash
        nd, d = -cash, ts_latest(ts, "CashCashEquivalentsAndShortTermInvestments")[1] or ts_latest(ts, "CashAndCashEquivalents")[1]
    mi, _ = ts_latest(ts, "MinorityInterest")
    return dict(nd=nd, mi=mi, asof=d)


def fetch_balance(sym, crumb, tries=3):
    """Balance-sheet components for EV from Yahoo fundamentals-timeseries -> (dict, currency)."""
    types = ",".join(p + f for p in ("annual", "quarterly") for f in BAL_FIELDS)
    last_err = None
    for i in range(tries):
        host = ("query2", "query1")[i % 2]
        try:
            params = dict(type=types, period1=int(time.time()) - 3 * 365 * 86400, period2=int(time.time()) + 86400)
            if crumb:
                params["crumb"] = crumb
            r = SESSION.get(f"https://{host}.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{sym}",
                            params=params, timeout=25)
            if r.status_code >= 400:
                raise IOError(f"HTTP {r.status_code}")
            ts, ccy = parse_timeseries(r.json())
            return balance_from_ts(ts), ccy
        except Exception as e:
            last_err = e
            time.sleep(min(10, 2 ** (i + 1)) + random.uniform(0, 0.5))
    raise RuntimeError(f"balance failed: {last_err}")


def fx_conv(v, src, dst, fx_table):
    """Convert v between currencies (minor units mapped to major) via the EUR-cross table."""
    if v is None or not src or not dst:
        return None
    src, dst = MAJOR.get(src, src), MAJOR.get(dst, dst)
    if src == dst:
        return v
    a, b = (fx_table.get(src) or {}).get("rate"), (fx_table.get(dst) or {}).get("rate")
    return v / a * b if a and b else None


def calc_ev(mcap, pccy, bal, fccy, yahoo_ev, fx_table):
    """EV in trading currency = market cap + net debt (+ minorities), the reporting-currency balance sheet converted
    at the current FX cross. Yahoo's enterpriseValue mixes currencies for cross-currency reporters, so it is used
    only when the components are missing and listing and reporting currency agree. -> (ev, source)."""
    pmaj = MAJOR.get(pccy, pccy)
    fccy = fccy or pmaj
    if mcap is not None and bal and bal.get("nd") is not None:
        adj = fx_conv(bal["nd"] + (bal.get("mi") or 0), fccy, pmaj, fx_table)
        if adj is not None:
            return mcap + adj, "calc"
    if yahoo_ev is not None and MAJOR.get(fccy, fccy) == pmaj:
        return yahoo_ev, "yahoo"
    return None, None


def fetch_fx_yahoo(ccy):
    """Units of `ccy` per 1 EUR via Yahoo EUR{CCY}=X. Returns (rate, asof_iso) or None."""
    if ccy == "EUR":
        return 1.0, dt.datetime.now(UTC).date().isoformat()
    pair = f"EUR{ccy}=X"
    try:
        df, meta = fetch_chart(pair, years=1, tries=3)
        px = meta.get("regularMarketPrice")
        if px is None and not df.empty:
            px = float(df["close"].iloc[-1])
        if px is None or not (px > 0):
            return None
        ts = meta.get("regularMarketTime")
        asof = (dt.datetime.fromtimestamp(ts, UTC).date().isoformat() if ts
                else (df.index[-1].strftime("%Y-%m-%d") if len(df) else None))
        return float(px), asof
    except Exception as e:
        log(f"  FX {pair}: Yahoo failed ({e})")
        return None


def fetch_fx_ecb(ccy):
    """ECB SDW mid: units of `ccy` per 1 EUR. Returns (rate, asof_iso) or None."""
    if ccy == "EUR":
        return 1.0, dt.datetime.now(UTC).date().isoformat()
    url = f"https://data-api.ecb.europa.eu/service/data/EXR/D.{ccy}.EUR.SP00.A"
    try:
        r = SESSION.get(url, params={"lastNObservations": 5, "format": "jsondata"},
                        timeout=20, headers={"Accept": "application/json"})
        if r.status_code >= 400:
            raise IOError(f"HTTP {r.status_code}")
        js = r.json()
        obs = js["dataSets"][0]["series"]["0:0:0:0:0"]["observations"]
        dims = js["structure"]["dimensions"]["observation"][0]["values"]
        # pick latest non-null
        best = None
        for k, v in obs.items():
            if v and v[0] is not None:
                best = (float(v[0]), dims[int(k)]["id"])
        return best
    except Exception as e:
        log(f"  FX {ccy}: ECB failed ({e})")
        return None


def build_fx_table(needed):
    """Map ISO currency -> {rate, asof, src} where rate = units of ccy per 1 EUR."""
    out = {"EUR": dict(rate=1.0, asof=dt.datetime.now(UTC).date().isoformat(), src="identity")}
    for ccy in sorted(needed):
        if ccy == "EUR":
            continue
        got = fetch_fx_yahoo(ccy)
        if got:
            out[ccy] = dict(rate=rnd(got[0], 6), asof=got[1], src="yahoo")
            continue
        got = fetch_fx_ecb(ccy)
        if got:
            out[ccy] = dict(rate=rnd(got[0], 6), asof=got[1], src="ecb")
        else:
            log(f"  FX {ccy}: unavailable")
        time.sleep(0.15)
    return out


def to_eur(val, ccy, fx_table):
    if val is None:
        return None
    major = MAJOR.get(ccy, ccy) or "EUR"
    info = fx_table.get(major)
    if not info or not info.get("rate"):
        return None
    return rnd(float(val) / float(info["rate"]), 0)  # whole euros


# ------------------------------------------------------------------ analytics
def rnd(x, n=4):
    if x is None:
        return None
    try:
        x = float(x)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, n)


def sig(x, s=5):
    if x is None or x == 0:
        return x
    return float(f"{x:.{s}g}")


def base_on_or_before(c, when):
    s = c[c.index <= when]
    return None if s.empty else float(s.iloc[-1])


def analyse(spec, df, meta, src, now, fund=None, fx_table=None):
    c, v = df["close"], df["volume"]
    last_date = c.index[-1]
    px = float(c.iloc[-1])
    out = dict(sym=spec["sym"], name=spec["name"], tv=spec.get("tv"), kind=spec.get("kind", "equity"),
               note=spec.get("note"), ccy=meta.get("currency"), exch=meta.get("exchangeName"),
               src=src, bar_date=last_date.strftime("%Y-%m-%d"), sessions=int(len(c)),
               first_date=c.index[0].strftime("%Y-%m-%d"))
    # live / partial session: fetched while the instrument's regular session was open
    try:
        reg = meta["currentTradingPeriod"]["regular"]
        today_ex = now.astimezone(ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")).date()
        out["partial"] = bool(reg["start"] <= now.timestamp() < reg["end"]) and last_date.date() == today_ex
    except Exception:
        out["partial"] = None
    out["state"] = session_state(meta, now.timestamp())   # open / pre / post / closed at fetch time
    out["market_time"] = (dt.datetime.fromtimestamp(meta["regularMarketTime"], UTC).isoformat()
                          if meta.get("regularMarketTime") else None)
    out["px"] = rnd(px, 6)
    prev = float(c.iloc[-2]) if len(c) >= 2 else None
    # equities/ETFs: previous close per Yahoo's quote (regularMarketChangePercent) guards against a blank prior
    # daily bar (Nordic/LSE quirk). FX/futures keep the daily-bar previous close (Yahoo's quote uses other cut-offs).
    rmp, rcp = meta.get("regularMarketPrice"), meta.get("regularMarketChangePercent")
    if spec.get("kind", "equity") in ("equity", "etf") and isinstance(rmp, (int, float)) and isinstance(rcp, (int, float)) and rcp > -100 and abs(px - rmp) <= 1e-6 * abs(rmp):
        q_prev = rmp / (1 + rcp / 100)
        if prev is None or abs(q_prev / prev - 1) > 0.002:
            prev = q_prev
    out["prev_close"] = rnd(prev, 6)
    out["chg"] = rnd(px - prev, 6) if prev else None
    pct = {"1D": rnd((px / prev - 1) * 100, 3) if prev else None}
    for key, off in HORIZONS:
        target = last_date - pd.DateOffset(**off)
        if c.index[0] > target:
            pct[key] = None            # not enough history: never extrapolate
            continue
        b = base_on_or_before(c, target)
        pct[key] = rnd((px / b - 1) * 100, 2) if b else None
    ye = pd.Timestamp(year=last_date.year - 1, month=12, day=31)
    b = base_on_or_before(c, ye) if c.index[0] <= ye else None
    pct["YTD"] = rnd((px / b - 1) * 100, 2) if b else None
    out["pct"] = pct
    if spec.get("kind") == "yield":
        # yields: also express changes in basis points (Yahoo quotes ^TNX etc. in percent)
        bp = {"1D": rnd((px - prev) * 100, 1) if prev else None}
        for key, off in HORIZONS:
            target = last_date - pd.DateOffset(**off)
            b = base_on_or_before(c, target) if c.index[0] <= target else None
            bp[key] = rnd((px - b) * 100, 1) if b else None
        b = base_on_or_before(c, ye) if c.index[0] <= ye else None
        bp["YTD"] = rnd((px - b) * 100, 1) if b else None
        out["bp"] = bp

    # volume: last bar vs average of the N sessions BEFORE it
    vol = {}
    vv = v.fillna(0)
    has_vol = bool((vv.tail(60) > 0).sum() >= 10)
    if has_vol and vv.iloc[-1] > 0:
        last_v = float(vv.iloc[-1])
        vol["last"] = int(last_v)
        prior = vv.iloc[:-1]
        prior = prior[prior > 0]
        for n in VOLS:
            if len(prior) >= n:
                a = float(prior.tail(n).mean())
                vol[f"a{n}"] = int(round(a))
                vol[f"r{n}"] = rnd(last_v / a, 3) if a > 0 else None
            else:
                vol[f"a{n}"] = vol[f"r{n}"] = None
    else:
        vol = dict(last=None, **{f"a{n}": None for n in VOLS}, **{f"r{n}": None for n in VOLS})
        if has_vol and vv.iloc[-1] == 0:
            vol["note"] = "no volume on latest bar yet"
    out["vol"] = vol

    ma = {}
    for n in MAS:
        if len(c) >= n:
            m = float(c.tail(n).mean())
            ma[str(n)] = dict(v=rnd(m, 6), pct=rnd((px / m - 1) * 100, 3))
            if spec.get("kind") == "yield":
                ma[str(n)]["bp"] = rnd((px - m) * 100, 1)
        else:
            ma[str(n)] = None
    out["ma"] = ma

    # 1-year sparkline: starts at the same base close as pct["1Y"] (last close on/before one year ago) and keeps
    # both the first and latest points exactly, so the compare chart's 1Y move matches the table.
    y1 = last_date - pd.DateOffset(years=1)
    prior = c.index[c.index <= y1]
    s = c[c.index >= prior[-1]] if len(prior) else c[c.index > y1]
    step = max(1, len(s) // 90)
    idx = list(range(len(s) - 1, -1, -step))[::-1]
    if idx and idx[0] != 0:
        idx = [0] + idx
    # 4 significant digits in between (invisible on a sparkline, ~10 KB less); first and latest points at 5
    out["spark"] = [sig(float(s.iloc[i]), 5 if k in (0, len(idx) - 1) else 4) for k, i in enumerate(idx)]
    out["spark_from"] = s.index[0].strftime("%Y-%m-%d") if len(s) else None
    out["roll"] = spec.get("kind") == "future"

    # Market cap / enterprise value (equities & ETFs), converted to EUR when FX is known
    kind = spec.get("kind", "equity")
    out["mcap"] = out["mcap_eur"] = out["ev"] = out["ev_eur"] = None
    out["mcap_ccy"] = None
    if meta.get("_last_from_quote"):
        out["last_from_quote"] = True     # latest close taken from the quote (chart bar was blank / missing)
    if fund and kind in ("equity", "etf"):
        mcap, ev_y, fcy, fin_ccy, bal = (list(fund) + [None, None])[:5]
        ccy = fcy or out.get("ccy")
        ev, ev_src = ev_y, ("yahoo" if ev_y is not None else None)
        if kind == "equity" and fx_table:
            ev, ev_src = calc_ev(mcap, ccy, bal, fin_ccy, ev_y, fx_table)
        out["mcap"] = sig(mcap, 6) if mcap is not None else None
        out["ev"] = sig(ev, 6) if ev is not None else None
        out["ev_src"] = ev_src
        out["mcap_ccy"] = ccy
        if fx_table:
            out["mcap_eur"] = to_eur(mcap, ccy, fx_table)
            out["ev_eur"] = to_eur(ev, ccy, fx_table)
    return out


# ------------------------------------------------------------------ futures: explicit contract months
def fetch_contract(sym, df_c, meta_c, src_c, now, futx):
    """Swap Yahoo's continuous series (BZ=F ...) for the explicit active contract (BZZ26.NYM ...): its own daily bars
    give a same-month 1D..1Y. Falls back to the continuous series (changes then suppressed in apply_contract)."""
    csym, label, exp = contracts.active(sym, now.astimezone(NEWYORK).date())
    info = dict(cont=(df_c, meta_c, src_c), sym=csym, month=label, expiry=exp.isoformat())
    try:
        df, meta, src = fetch(csym)
        if df.empty:
            raise RuntimeError("no valid closes")
        lag = (df_c.index[-1] - df.index[-1]).days
        if lag > 4:
            raise RuntimeError(f"contract bars end {df.index[-1].date()}, {lag} days behind {sym}")
        futx[sym] = info
        log(f"  {sym}: using {csym} ({label}, expires {exp})")
        return df, meta, src
    except Exception as e:
        info.update(sym=None, why=f"{csym} unavailable: {e}"[:200])
        futx[sym] = info
        log(f"  {sym}: {info['why']} -> continuous level only, changes suppressed")
        return df_c, meta_c, src_c


def apply_contract(row, spec, info, now):
    df_c, meta_c, src_c = info["cont"]
    cont = analyse(spec, df_c, meta_c, src_c, now)
    for k in LONG_CONT:
        row["pct"][k] = cont["pct"].get(k)
    row["pct_cont"] = list(LONG_CONT)
    if info.get("sym"):
        row["contract"] = dict(sym=info["sym"], month=info["month"], expiry=info["expiry"], cont=spec["sym"])
        row["name"] = f'{spec["name"]} {info["month"]}'
    else:   # continuous level only: a change could straddle a roll, so none is shown
        row["contract"] = dict(sym=None, cont=spec["sym"], why=info.get("why"))
        row["prev_close"] = row["chg"] = None
        row["pct"] = {k: (v if k in LONG_CONT else None) for k, v in row["pct"].items()}
        row["ma"] = {k: None for k in row.get("ma", {})}
        row["chg_suppressed"] = True


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "market.json"))
    ap.add_argument("--only", default="")
    ap.add_argument("--sleep", type=float, default=0.35)
    ap.add_argument("--intraday", action="store_true", help="prices only; scale mcap/EV from data/market.json")
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    now = dt.datetime.now(UTC)

    specs = {}
    for g in GROUPS:
        for sec in g["sections"]:
            for r in sec["rows"]:
                specs.setdefault(r["sym"], r)
    syms = [s for s in specs if not only or s in only]
    cache, errors = {}, []
    futx = {}            # futures: explicit contract used (or why not) + the continuous series for 2Y-10Y
    offx = {}            # official 10Y yields: extra row fields (official source, CNBC live quote, fallback reason)
    base0 = {}
    if a.intraday:       # official closes change once a day: intraday runs keep the last full build's rows, refresh the live quote
        try:
            with open(os.path.join(ROOT, "data", "market.json")) as f:
                b0 = json.load(f)
            base0 = {r["sym"]: r for g in b0["groups"] for sec in g["sections"] for r in sec["rows"]}
            base0["__y10"] = b0.get("yields10")
        except Exception as e:
            log(f"intraday: no base market.json for official yields ({e})")
    t0 = time.time()
    for i, sym in enumerate(syms):
        try:
            if sym in OFFICIAL:
                b = base0.get(sym)
                if a.intraday and b and not b.get("error") and b.get("official"):
                    offx[sym] = dict(carry=dict(b, live=cnbc_live(OFFICIAL[sym]["cnbc"]) or b.get("live")))
                    log(f"[{i+1}/{len(syms)}] {sym}: official row kept from the full build, live quote refreshed")
                    continue
                df, meta, src, offx[sym] = fetch_official(sym)
                cache[sym] = (df, meta, src)
                log(f"[{i+1}/{len(syms)}] {sym}: {len(df)} closes to {df.index[-1].date()} via {src}")
                continue
            df, meta, src = fetch(sym)
            if df.empty:
                raise RuntimeError("no valid closes")
            if sym in contracts.ROOTS:
                df, meta, src = fetch_contract(sym, df, meta, src, now, futx)
            cache[sym] = (df, meta, src)
            log(f"[{i+1}/{len(syms)}] {sym}: {len(df)} bars to {df.index[-1].date()} via {src}")
        except Exception as e:
            errors.append(dict(sym=sym, error=str(e)[:300]))
            log(f"[{i+1}/{len(syms)}] {sym}: FAILED {e}")
        time.sleep(a.sleep + random.uniform(0, 0.25))

    ok_share = (len(cache) + sum(1 for x in offx.values() if "carry" in x)) / max(1, len(syms))
    if ok_share < MIN_OK_SHARE:
        log(f"Only {len(cache)}/{len(syms)} symbols fetched - not overwriting {a.out}")
        return 2

    # Fundamentals (mcap / EV) for equities & ETFs (intraday: reuse the last full build instead)
    base_rows, base_meta = {}, {}
    if a.intraday:
        try:
            with open(os.path.join(ROOT, "data", "market.json")) as f:
                base = json.load(f)
            base_meta = dict(utc=base.get("fundamentals_utc") or base.get("generated_utc"))
            base_rows = {r["sym"]: r for g in base["groups"] for sec in g["sections"] for r in sec["rows"]}
        except Exception as e:
            log(f"intraday: no base market.json ({e})")
    crumb = None if a.intraday else yahoo_crumb(SESSION)
    funds = {}
    fund_kinds = {"equity", "etf"}
    fund_syms = [s for s in syms if specs[s].get("kind", "equity") in fund_kinds and s in cache and not a.intraday]
    for i, sym in enumerate(fund_syms):
        try:
            mcap, ev, ccy, fccy = fetch_fundamentals(sym, crumb)
            bal = None
            if specs[sym].get("kind", "equity") == "equity":
                try:
                    bal, ts_ccy = fetch_balance(sym, crumb)
                    fccy = ts_ccy or fccy
                except Exception as e:
                    log(f"  balance {sym}: {e}")
            funds[sym] = (mcap, ev, ccy, fccy, bal)
            log(f"  fund [{i+1}/{len(fund_syms)}] {sym}: mcap={mcap} ev_yahoo={ev} {ccy}/{fccy} nd={bal and bal.get('nd')}")
        except Exception as e:
            log(f"  fund [{i+1}/{len(fund_syms)}] {sym}: {e}")
        time.sleep(a.sleep * 0.5 + random.uniform(0, 0.15))

    # FX -> EUR (Yahoo, ECB mid fallback)
    needed = {MAJOR.get(r.get("mcap_ccy"), r.get("mcap_ccy")) for r in base_rows.values() if r.get("mcap_ccy")}
    for sym, trip in funds.items():
        ccy = trip[2] or (cache[sym][1] or {}).get("currency")
        for c in (ccy, trip[3]):
            if c:
                needed.add(MAJOR.get(c, c))
    for sym, (df, meta, src) in cache.items():
        ccy = meta.get("currency")
        if ccy:
            needed.add(MAJOR.get(ccy, ccy))
    needed.discard(None)
    fx_table = build_fx_table(needed)
    log(f"FX table: { {k: v['rate'] for k, v in fx_table.items()} }")

    groups = []
    for g in GROUPS:
        gs = dict(id=g["id"], label=g["label"], sections=[])
        for sec in g["sections"]:
            rows = []
            for spec in sec["rows"]:
                if only and spec["sym"] not in only:
                    continue
                if "carry" in offx.get(spec["sym"], {}):
                    rows.append(offx[spec["sym"]]["carry"])
                    continue
                if spec["sym"] in cache:
                    df, meta, src = cache[spec["sym"]]
                    try:
                        row = analyse(spec, df, meta, src, now, fund=funds.get(spec['sym']), fx_table=fx_table)
                        if spec["sym"] in futx:
                            apply_contract(row, spec, futx[spec["sym"]], now)
                        if spec["sym"] in offx:
                            row.update(offx[spec["sym"]])
                        b = base_rows.get(spec["sym"]) or {}
                        if a.intraday and b.get("px") and row.get("px") and b.get("ccy") == row.get("ccy"):
                            # mcap scales with price; EV = mcap + (unchanged) net debt
                            k = row["px"] / b["px"]
                            for f in ("mcap_ccy", "ev_src"):
                                row[f] = b.get(f)
                            if b.get("mcap") is not None:
                                row["mcap"] = sig(b["mcap"] * k, 6)
                                row["mcap_eur"] = to_eur(row["mcap"], row["mcap_ccy"], fx_table)
                                if b.get("ev") is not None:
                                    row["ev"] = sig(b["ev"] + b["mcap"] * (k - 1), 6)
                                    row["ev_eur"] = to_eur(row["ev"], row["mcap_ccy"], fx_table)
                    except Exception as e:
                        row = dict(sym=spec["sym"], name=spec["name"], tv=spec.get("tv"), kind=spec.get("kind"),
                                   note=spec.get("note"), error=f"analysis failed: {e}"[:200])
                        errors.append(dict(sym=spec["sym"], error=row["error"]))
                elif a.intraday and spec["sym"] in base_rows and not base_rows[spec["sym"]].get("error"):
                    row = dict(base_rows[spec["sym"]], stale=True)   # keep the last full-build values
                else:
                    err = next((e["error"] for e in errors if e["sym"] == spec["sym"]), "not fetched")
                    row = dict(sym=spec["sym"], name=spec["name"], tv=spec.get("tv"), kind=spec.get("kind"),
                               note=spec.get("note"), error=err)
                rows.append(row)
            gs["sections"].append(dict(label=sec.get("label"), note=sec.get("note"), rows=rows))
        groups.append(gs)

    # 10-year government yields side by side (Macro comparison chart) and spreads vs the Bund, bp
    y10 = {}
    TEN = [("US", "^TNX"), ("DE", "CNBC:DE10Y-DE"), ("GB", "CNBC:GB10Y-GB"), ("FR", "CNBC:FR10Y-FR"), ("CH", "CNBC:CH10Y-CH")]
    try:
        cols = {k: cache[s][0]["close"] for k, s in TEN if s in cache}
        if a.intraday and base0.get("__y10") and all("carry" in offx.get(x, {}) for _, x in TEN):
            cols = {}
            y10 = base0["__y10"]
        if "DE" in cols and len(cols) >= 2:
            f = pd.DataFrame(cols)
            last_ok = {k: f[k].last_valid_index() for k in f.columns}
            f = f[f.index > f.index[-1] - pd.DateOffset(years=1)].ffill()
            for k, li in last_ok.items():          # no carry-forward past a series' own latest close (official lags differ)
                f.loc[f.index > li, k] = float("nan")
            f = f.iloc[::-1].iloc[::2].iloc[::-1]      # every 2nd trading day, latest kept
            y10 = dict(dates=[d.strftime("%Y-%m-%d") for d in f.index],
                       series={k: [None if pd.isna(v) else round(float(v), 3) for v in f[k]] for k in f.columns},
                       src={k: (OFFICIAL[s]["name"] if cache[s][2] != "cnbc" else "CNBC (fallback)") for k, s in TEN if s in cache},
                       asof={k: cache[s][0].index[-1].strftime("%Y-%m-%d") for k, s in TEN if s in cache})
            sp = {}
            for k in ("FR", "GB"):
                if k in cols:
                    j = pd.DataFrame({"a": cols[k], "b": cols["DE"]}).dropna()     # same-day closes only
                    v = (j["a"] - j["b"]) * 100
                    def at(off):
                        t = v.index[-1] - pd.DateOffset(**off)
                        w = v[v.index <= t]
                        return round(float(v.iloc[-1] - w.iloc[-1]), 1) if len(w) else None
                    sp[k + "-DE"] = dict(bp=round(float(v.iloc[-1]), 1), d1=round(float(v.iloc[-1] - v.iloc[-2]), 1) if len(v) > 1 else None,
                                         m1=at(dict(months=1)), y1=at(dict(years=1)), date=v.index[-1].strftime("%Y-%m-%d"))
            y10["spreads"] = sp
    except Exception as e:
        log(f"yields10: {e}")

    fx_asof = sorted({v["asof"] for v in fx_table.values() if v.get("asof")})
    out = dict(
        generated_utc=now.isoformat(timespec="seconds"),
        generated_cet=now.astimezone(CET_TZ).isoformat(timespec="seconds"),
        mode="intraday" if a.intraday else "full",
        fundamentals_utc=base_meta.get("utc") if a.intraday else now.isoformat(timespec="seconds"),
        source="Yahoo Finance daily bars (chart API; yfinance fallback). Price returns, not total return. Market cap / EV from Yahoo quoteSummary, converted to EUR.",
        method=dict(
            returns="Last close vs last close on/before the same calendar date N periods earlier; YTD vs prior year-end close; n/a if history is shorter.",
            volume="Last session volume vs average of the N sessions before it.",
            ma="Simple moving average of daily closes incl. the latest bar; % = price / MA - 1.",
            futures=("Futures: one explicit contract month (e.g. Brent Jan-27, Yahoo BZF27.NYM) for the price, 1D to 1Y changes, "
                     "moving averages and the 1Y line, so every change compares the same month. The month is the one still trading "
                     "after today's session (on an expiry day the next month is used for both dates). 2Y to 10Y come from Yahoo's "
                     "continuous front-month series and include roll effects. If the contract quote is missing, the continuous "
                     "level is shown without any change."),
            mcap_ev="Yahoo marketCap. Equity EV = market cap + net debt (latest total debt minus cash & short-term investments) + minority interest, reporting-currency balance sheet converted at current FX (same as the company panel); Yahoo enterpriseValue only when components are missing (and for ETFs). Converted to EUR via Yahoo EUR{CCY}=X (ECB SDW mid fallback). London quotes are in pence (GBp); Yahoo market cap is in GBP (checked against shares x price, a 100x pence slip is scaled back).",
            last_close="When Yahoo's chart API leaves the latest daily close blank (common for Nordic/LSE tickers), the quote's regularMarketPrice for that session is used (last_from_quote).",
            prices="Latest price = Yahoo quote (regularMarketPrice / regularMarketTime; typically ~15 min delayed, not live); 1D vs the previous session's close. While a session is open the latest daily bar (and its volume) is today so far.",
        ),
        fx_eur=fx_table,
        fx_asof=fx_asof[-1] if fx_asof else None,
        symbols=len(syms), fetched=len(cache), seconds=round(time.time() - t0, 1),
        groups=groups, errors=errors, yields10=y10 or None,
    )
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    os.replace(tmp, a.out)
    log(f"wrote {a.out}: {len(cache)}/{len(syms)} symbols, {len(errors)} errors, {out['seconds']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

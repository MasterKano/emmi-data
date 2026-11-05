#!/usr/bin/env python3
"""Build the company page data: a lean index (data/companies.json) plus one detail file per company
(data/co/<SYMBOL>.json) that the page loads on demand when a company is opened.

Sources (Yahoo Finance public endpoints, cookie + crumb shared with build_data.py):
  * v10 quoteSummary: price, summaryDetail, defaultKeyStatistics, financialData, calendarEvents, assetProfile
    (profile, officers, governance scores), earningsTrend, earningsHistory, recommendationTrend,
    upgradeDowngradeHistory, majorHoldersBreakdown, institutionOwnership, fundOwnership, insiderTransactions,
    insiderHolders, netSharePurchaseActivity
  * ws/fundamentals-timeseries: annual / quarterly / trailing income statement, balance sheet and cash flow lines
  * v8 chart (events=div): dividend payments per share, last 10 years

Index (companies.json): what the Table and the panel header / overview need, incl. price-dependent figures.
Detail (co/SYM.json): slow-moving data only (no price, no fetch time) so unchanged companies keep identical files
and the daily commits stay small.

Real data only: missing values are null / omitted. Per-symbol failures never break the build; the previous index
entry is kept (flagged "stale") and the previous detail file is left in place. Fetches run in a small thread pool
with a global request pacer.

Usage: python scripts/build_companies.py [--out data/companies.json] [--only SYM,SYM] [--extra SYM,SYM] [--workers 4]
"""
import argparse, datetime as dt, json, math, os, random, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from universe import GROUPS  # noqa: E402
import build_data as BD  # noqa: E402
from build_data import SESSION, MAJOR, yahoo_crumb, build_fx_table, log, balance_from_ts, calc_ev, minor_unit_cap  # noqa: E402

ROOT = os.path.dirname(HERE)
UTC = dt.timezone.utc
QS_MODULES = ",".join((
    "price", "summaryDetail", "defaultKeyStatistics", "financialData", "calendarEvents", "assetProfile",
    "earningsTrend", "earningsHistory", "recommendationTrend", "upgradeDowngradeHistory", "majorHoldersBreakdown",
    "institutionOwnership", "fundOwnership", "insiderTransactions", "insiderHolders", "netSharePurchaseActivity"))

# short key -> Yahoo timeseries field(s) (first non-null wins); grouped by statement for the page
IS_F = [("rev", "TotalRevenue"), ("cogs", "CostOfRevenue"), ("gp", "GrossProfit"), ("sga", "SellingGeneralAndAdministration"),
        ("rnd", "ResearchAndDevelopment"), ("opex", "OperatingExpense"), ("oi", "OperatingIncome"), ("ebit", "EBIT"),
        ("ebitda", ("EBITDA", "NormalizedEBITDA")), ("da", "ReconciledDepreciation"), ("intx", "InterestExpense"),
        ("pti", "PretaxIncome"), ("tax", "TaxProvision"), ("ni", ("NetIncomeCommonStockholders", "NetIncome")),
        ("eps", "DilutedEPS"), ("dsh", "DilutedAverageShares")]
BS_F = [("ta", "TotalAssets"), ("ca", "CurrentAssets"), ("cash", ("CashCashEquivalentsAndShortTermInvestments", "CashAndCashEquivalents")),
        ("ar", "AccountsReceivable"), ("inv", "Inventory"), ("ppe", "NetPPE"), ("gw", "GoodwillAndOtherIntangibleAssets"),
        ("tl", "TotalLiabilitiesNetMinorityInterest"), ("cl", "CurrentLiabilities"), ("ap", "AccountsPayable"),
        ("std", "CurrentDebt"), ("ltd", "LongTermDebt"), ("debt", "TotalDebt"), ("eq", "StockholdersEquity"),
        ("mi", "MinorityInterest"), ("wc", "WorkingCapital"), ("so", "OrdinarySharesNumber")]
CF_F = [("ocf", "OperatingCashFlow"), ("dwc", "ChangeInWorkingCapital"), ("sbc", "StockBasedCompensation"),
        ("capex", "CapitalExpenditure"), ("fcf", "FreeCashFlow"), ("icf", "InvestingCashFlow"), ("fincf", "FinancingCashFlow"),
        ("div", "CashDividendsPaid"), ("buy", "RepurchaseOfCapitalStock"), ("iss", "IssuanceOfCapitalStock"),
        ("dis", "IssuanceOfDebt"), ("drp", "RepaymentOfDebt")]
FLOW_KEYS = {k for k, _ in IS_F + CF_F}
TS_FIELDS = sorted({f for _, fs in IS_F + BS_F + CF_F for f in ((fs,) if isinstance(fs, str) else fs)} | {"NetDebt"})
N_ANNUAL, N_QUARTER = 5, 6
N_TX, N_UPG, N_HOLD = 25, 25, 10
MIN_OK_SHARE = 0.5   # keep the old files if fewer than half the symbols fetched (e.g. Yahoo outage)
PACE = 0.12          # seconds between request starts across all threads (~8 req/s)


# ------------------------------------------------------------------ helpers
def num(x):
    if isinstance(x, dict):
        x = x.get("raw")
    try:
        x = float(x)
    except Exception:
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def sig(x, s=5):
    x = num(x)
    if x is None:
        return None
    if x == 0:
        return 0
    v = float(f"{x:.{s}g}")
    return int(v) if abs(v) >= 1e4 and v == int(v) else v


def rnd(x, n=2):
    x = num(x)
    return None if x is None else round(x, n)


def pctv(x, n=2):
    """Yahoo fraction -> percent."""
    x = num(x)
    return None if x is None else round(x * 100, n)


def iso_date(v):
    t = num(v)
    if t is None:
        return None
    try:
        return dt.datetime.fromtimestamp(t, UTC).date().isoformat()
    except Exception:
        return None


def clean(d):
    """Drop None / empty values (keeps 0 and False) so files stay small."""
    if isinstance(d, dict):
        out = {k: clean(v) for k, v in d.items()}
        return {k: v for k, v in out.items() if v is not None and v != {} and v != []}
    if isinstance(d, list):
        return [clean(v) for v in d]
    return d


_pace_lock, _pace_next = threading.Lock(), [0.0]
_tl = threading.local()


def session():
    """One HTTP session per worker thread, sharing the main session's Yahoo cookies."""
    s = getattr(_tl, "s", None)
    if s is None:
        try:
            s = BD.http.Session(impersonate="chrome")
        except TypeError:
            s = BD.http.Session()
            s.headers.update(SESSION.headers)
        try:
            s.cookies.update(SESSION.cookies)
        except Exception:
            for c in SESSION.cookies.jar if hasattr(SESSION.cookies, "jar") else SESSION.cookies:
                try:
                    s.cookies.set(c.name, c.value, domain=c.domain)
                except Exception:
                    pass
        _tl.s = s
    return s


def pace():
    with _pace_lock:
        now = time.time()
        wait = _pace_next[0] - now
        _pace_next[0] = max(now, _pace_next[0]) + PACE + random.uniform(0, 0.04)
    if wait > 0:
        time.sleep(wait)


def get(url, params, crumb, tries=4):
    last = None
    for i in range(tries):
        host = ("query2", "query1")[i % 2]
        try:
            p = dict(params)
            if crumb[0]:
                p["crumb"] = crumb[0]
            pace()
            r = session().get(url.format(host=host), params=p, timeout=25)
            if r.status_code in (401, 403):
                with _pace_lock:
                    crumb[0] = yahoo_crumb(SESSION)
                _tl.s = None
                raise IOError(f"HTTP {r.status_code} (crumb refreshed)")
            if r.status_code == 404:
                return r.json() if "json" in r.headers.get("content-type", "") else None
            if r.status_code >= 400:
                raise IOError(f"HTTP {r.status_code}")
            return r.json()
        except Exception as e:
            last = e
            time.sleep(min(20, 2 ** (i + 1)) + random.uniform(0, 1))
    raise RuntimeError(str(last)[:200])


def fetch_summary(sym, crumb):
    js = get("https://{host}.finance.yahoo.com/v10/finance/quoteSummary/" + sym, dict(modules=QS_MODULES), crumb)
    res = ((js or {}).get("quoteSummary") or {}).get("result") or []
    if not res:
        raise LookupError(str(((js or {}).get("quoteSummary") or {}).get("error"))[:160])
    r = res[0]
    p = r.get("price") or {}
    if num(p.get("regularMarketPrice")) is None and num(p.get("marketCap")) is None:
        raise LookupError("no quote data (unknown or delisted symbol?)")
    return r


def fetch_timeseries(sym, crumb):
    types = ",".join(p + f for p in ("annual", "quarterly", "trailing") for f in TS_FIELDS)
    js = get("https://{host}.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/" + sym,
             dict(type=types, period1=1420070400, period2=int(time.time()) + 86400), crumb)
    out, ccys = {}, {}
    for it in ((js or {}).get("timeseries") or {}).get("result") or []:
        t = ((it.get("meta") or {}).get("type") or [None])[0]
        pts = [x for x in (it.get(t) or []) if x and x.get("asOfDate") and num((x.get("reportedValue") or {}).get("raw")) is not None]
        if not t or not pts:
            continue
        out[t] = {x["asOfDate"]: num(x["reportedValue"]["raw"]) for x in pts}
        for x in pts:
            if x.get("currencyCode"):
                ccys[x["currencyCode"]] = ccys.get(x["currencyCode"], 0) + 1
    ccy = max(ccys, key=ccys.get) if ccys else None
    return out, ccy


def fetch_divs(sym, crumb):
    js = get("https://{host}.finance.yahoo.com/v8/finance/chart/" + sym, dict(range="10y", interval="3mo", events="div"), [None])
    res = (((js or {}).get("chart") or {}).get("result") or [None])[0] or {}
    ev = (res.get("events") or {}).get("dividends") or {}
    pts = sorted((iso_date(v.get("date")), num(v.get("amount"))) for v in ev.values() if v.get("date") and num(v.get("amount")))
    return pts, (res.get("meta") or {}).get("currency")


# ------------------------------------------------------------------ statements
def pick(ts, pre, fields, d):
    for f in ((fields,) if isinstance(fields, str) else fields):
        v = (ts.get(pre + f) or {}).get(d)
        if v is not None:
            return v
    return None


def statement_rows(ts, pre, n):
    """Full IS/BS/CF lines for the last n period ends of `pre` (annual|quarterly), oldest first."""
    dates = set()
    for f in ("TotalRevenue", "NetIncome", "NetIncomeCommonStockholders", "TotalAssets", "OperatingCashFlow", "EBITDA", "TotalDebt"):
        dates |= set(ts.get(pre + f) or {})
    rows = []
    for d in sorted(dates)[-n:]:
        row = {"d": d}
        for k, fs in IS_F + BS_F + CF_F:
            v = pick(ts, pre, fs, d)
            row[k] = round(v, 4) if k == "eps" and v is not None else sig(v)
        if row.get("fcf") is None and row.get("ocf") is not None and row.get("capex") is not None:
            row["fcf"] = sig(row["ocf"] + row["capex"])
        debt, cash = row.get("debt"), row.get("cash")
        nd = (debt - cash) if debt is not None and cash is not None else pick(ts, pre, "NetDebt", d)
        if nd is None and debt is None and cash is not None and any(row.get(k) is not None for k in ("ta", "tl", "eq")):
            nd = -cash
        row["nd"] = sig(nd)
        if sum(1 for k, v in row.items() if k != "d" and v is not None) >= 2:
            rows.append(row)
    return rows


def ttm_row(ts, quarterly):
    """Trailing twelve months: flows from Yahoo 'trailing' series (else sum of last 4 quarters)."""
    row, dmax = {}, None
    for k, fs in IS_F + CF_F:
        v = None
        for f in ((fs,) if isinstance(fs, str) else fs):
            s = ts.get("trailing" + f) or {}
            if s:
                d = max(s)
                v, dmax = s[d], max(dmax or d, d)
                break
        if v is None and len(quarterly) >= 4 and k != "dsh" and all(q.get(k) is not None for q in quarterly[-4:]):
            v = sum(q[k] for q in quarterly[-4:])
            dmax = max(dmax or quarterly[-1]["d"], quarterly[-1]["d"])
        row[k] = round(v, 4) if k == "eps" and v is not None else sig(v)
    if row.get("fcf") is None and row.get("ocf") is not None and row.get("capex") is not None:
        row["fcf"] = sig(row["ocf"] + row["capex"])
    if not any(v is not None for v in row.values()):
        return None
    row["d"] = dmax
    return row


def div0(a, b, pos=False):
    if a is None or b is None or b == 0 or (pos and b < 0):
        return None
    return a / b


def ratio_row(x, prev=None, bal=None):
    """Key ratios for one period. Flows from x; balance-sheet items from bal (defaults to x)."""
    b = bal or x
    rev = x.get("rev")
    eq0 = (prev or {}).get("eq")
    eq_avg = (b.get("eq") + eq0) / 2 if b.get("eq") is not None and eq0 is not None else b.get("eq")
    oi = x.get("oi") if x.get("oi") is not None else x.get("ebit")
    r = dict(
        gm=div0(x.get("gp"), rev, True), om=div0(oi, rev, True), em=div0(x.get("ebitda"), rev, True), nm=div0(x.get("ni"), rev, True),
        roe=div0(x.get("ni"), eq_avg, True), roa=div0(x.get("ni"), b.get("ta"), True),
        nd_ebitda=div0(b.get("nd"), x.get("ebitda"), True), de=div0(b.get("debt"), b.get("eq"), True),
        cur=div0(b.get("ca"), b.get("cl"), True),
        icov=div0(x.get("ebit"), abs(x["intx"]) if x.get("intx") else None, True),
        capex_rev=div0(abs(x["capex"]) if x.get("capex") is not None else None, rev, True),
        fcf_m=div0(x.get("fcf"), rev, True),
        payout=div0(abs(x["div"]) if x.get("div") is not None else None, x.get("ni"), True),
    )
    out = {"d": x.get("d")}
    for k, v in r.items():
        if v is not None:
            out[k] = round(v * 100, 1) if k in ("gm", "om", "em", "nm", "roe", "roa", "capex_rev", "fcf_m", "payout") else round(v, 2)
    return out


def first_sentences(text, limit=320):
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[a-z0-9\)])\.\s+(?=[A-Z])", text)
    out = ""
    for p in parts:
        p = p.rstrip(".") + "."
        if out and len(out) + len(p) + 1 > limit:
            break
        out = (out + " " + p).strip()
        if len(out) >= limit * 0.6:
            break
    if len(out) > limit + 80:
        out = out[:limit].rsplit(" ", 1)[0] + "…"
    return out


class FX:
    def __init__(self, table):
        self.t = table

    def rate(self, ccy):
        info = self.t.get(MAJOR.get(ccy, ccy) or "")
        return info.get("rate") if info else None

    def conv(self, v, src, dst):
        if v is None or not src or not dst:
            return None
        src, dst = MAJOR.get(src, src), MAJOR.get(dst, dst)
        if src == dst:
            return v
        a, b = self.rate(src), self.rate(dst)
        return v / a * b if a and b else None


# ------------------------------------------------------------------ shaping
def own_list(mod, minor=1.0):
    """Holder list; Yahoo values = position x current price in the quote unit (pence for GBp) -> major unit."""
    out = []
    for h in (mod or {}).get("ownershipList") or []:
        if not h.get("organization"):
            continue
        v = num(h.get("value"))
        out.append(dict(n=h["organization"].strip(), pct=pctv(h.get("pctHeld"), 2), pos=sig(h.get("position")),
                        val=sig(v / minor) if v is not None else None, chg=pctv(h.get("pctChange"), 1), d=iso_date(h.get("reportDate"))))
    out.sort(key=lambda x: (-(x.get("pct") or 0), -(x.get("val") or 0)))
    return out[:N_HOLD]


def shape(sym, spec, qs, ts, ts_ccy, divs, fx, now):
    """-> (index entry, detail dict)."""
    price = qs.get("price") or {}
    sd = qs.get("summaryDetail") or {}
    ks = qs.get("defaultKeyStatistics") or {}
    fd = qs.get("financialData") or {}
    cal = qs.get("calendarEvents") or {}
    prof = qs.get("assetProfile") or {}
    pccy = price.get("currency") or sd.get("currency")          # may be a minor unit (GBp, ILA, ZAc)
    pmaj = MAJOR.get(pccy, pccy)
    fccy = fd.get("financialCurrency") or ts_ccy or pmaj
    minor = 100.0 if pccy in MAJOR else 1.0

    px = num(price.get("regularMarketPrice")) or num(fd.get("currentPrice"))
    shares = num(ks.get("sharesOutstanding")) or num(ks.get("impliedSharesOutstanding"))
    mcap = num(price.get("marketCap")) or num(sd.get("marketCap"))   # Yahoo reports cap in the major unit
    mcap, cap_fixed = minor_unit_cap(mcap, pccy, px, shares)
    annual = statement_rows(ts, "annual", N_ANNUAL)
    quarterly = statement_rows(ts, "quarterly", N_QUARTER)
    t12 = ttm_row(ts, quarterly) or {}

    bal = balance_from_ts(ts)               # shared with build_data.py so Table EV == panel EV
    nd = bal["nd"]
    eq = next((r["eq"] for r in reversed(quarterly + annual) if r.get("eq") is not None), None)
    if quarterly and annual and quarterly[-1]["d"] < annual[-1]["d"]:
        eq = next((r["eq"] for r in reversed(annual) if r.get("eq") is not None), eq)

    mcap_f = fx.conv(mcap, pmaj, fccy)
    ev, ev_src = calc_ev(mcap, pccy, bal, fccy, None if cap_fixed else num(ks.get("enterpriseValue")), fx.t)
    ev_f = fx.conv(ev, pmaj, fccy)

    def ratio(a, b, lo=None):
        if a is None or b is None or b == 0:
            return None
        if lo is not None and b <= lo:
            return None
        return rnd(a / b, 2)

    pe_ttm = num(sd.get("trailingPE"))
    if pe_ttm is None and mcap_f is not None and t12.get("ni") and t12["ni"] > 0:
        pe_ttm = mcap_f / t12["ni"]
    ret = None
    if mcap_f and (t12.get("div") is not None or t12.get("buy") is not None):
        ret = (abs(t12.get("div") or 0) + abs(t12.get("buy") or 0)) / mcap_f * 100
    val = dict(
        mcap=sig(mcap, 6), ev=sig(ev, 6), ev_src=ev_src,
        mcap_eur=sig(fx.conv(mcap, pmaj, "EUR"), 6), ev_eur=sig(fx.conv(ev, pmaj, "EUR"), 6),
        pe=rnd(pe_ttm), pe_fwd=rnd(num(sd.get("forwardPE"))),
        ev_ebitda=ratio(ev_f, t12.get("ebitda"), lo=0), ev_rev=ratio(ev_f, t12.get("rev"), lo=0),
        pb=ratio(mcap_f, eq, lo=0) if (eq and mcap_f) else rnd(num(ks.get("priceToBook"))),
        dy=rnd((num(sd.get("dividendYield")) or 0) * 100, 2) if num(sd.get("dividendYield")) is not None else None,
        fcf_y=rnd(t12["fcf"] / mcap_f * 100, 1) if t12.get("fcf") is not None and mcap_f else None,
        ret_y=rnd(ret, 1),
        neg_earn=bool(t12.get("ni") is not None and t12["ni"] < 0),
        neg_ebitda=bool(t12.get("ebitda") is not None and t12["ebitda"] < 0),
        nd=sig(nd), shares=sig(shares, 6),
    )
    for k in ("pe", "pe_fwd"):            # Yahoo sometimes emits nonsense multiples (currency mix-ups)
        if val[k] is not None and not (0 < val[k] < 1000):
            val[k] = None

    # market stats (price-dependent: index)
    flt = num(ks.get("floatShares"))
    mkt = dict(lo52=rnd(num(sd.get("fiftyTwoWeekLow")), 6), hi52=rnd(num(sd.get("fiftyTwoWeekHigh")), 6),
               beta=rnd(num(sd.get("beta")) or num(ks.get("beta")), 2), float=sig(flt, 6),
               float_pct=rnd(flt / shares * 100, 1) if flt and shares and flt <= shares else None,
               short=sig(num(ks.get("sharesShort")), 6), short_prev=sig(num(ks.get("sharesShortPriorMonth")), 6),
               short_pf=pctv(ks.get("shortPercentOfFloat"), 2), short_ratio=rnd(num(ks.get("shortRatio")), 2),
               short_d=iso_date(ks.get("dateShortInterest")),
               avg_vol=sig(num(sd.get("averageVolume")), 4), ma50=rnd(num(sd.get("fiftyDayAverage")), 6),
               ma200=rnd(num(sd.get("twoHundredDayAverage")), 6), chg52=pctv(ks.get("52WeekChange"), 1),
               rev_g=pctv(fd.get("revenueGrowth"), 1), earn_g=pctv(fd.get("earningsGrowth"), 1))

    # analysts
    tgt = num(fd.get("targetMeanPrice"))
    cur = num(fd.get("currentPrice")) or px
    n_an = num(fd.get("numberOfAnalystOpinions"))
    trend = []
    for t in (qs.get("recommendationTrend") or {}).get("trend") or []:
        row = {k: int(t.get(k) or 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
        if sum(row.values()):
            trend.append(dict(p=t.get("period"), **row))
    breakdown = next(({k: v for k, v in t.items() if k != "p"} for t in trend if t["p"] == "0m"), None)
    cons = dict(
        target=rnd(tgt, 4), target_hi=rnd(num(fd.get("targetHighPrice")), 4), target_lo=rnd(num(fd.get("targetLowPrice")), 4),
        target_med=rnd(num(fd.get("targetMedianPrice")), 4),
        upside=rnd((tgt / cur - 1) * 100, 1) if tgt and cur else None,
        rating=(fd.get("recommendationKey") if fd.get("recommendationKey") not in (None, "none") else None),
        rating_mean=rnd(num(fd.get("recommendationMean")), 2), analysts=int(n_an) if n_an else None,
        breakdown=breakdown,
    )
    if not cons["analysts"] and not tgt:
        cons.update(target=None, target_hi=None, target_lo=None, target_med=None, upside=None)

    est = []
    for t in (qs.get("earningsTrend") or {}).get("trend") or []:
        if t.get("period") not in ("0q", "+1q", "0y", "+1y"):
            continue
        ee, re_ = t.get("earningsEstimate") or {}, t.get("revenueEstimate") or {}
        et, er = t.get("epsTrend") or {}, t.get("epsRevisions") or {}
        n_e, n_r = num(ee.get("numberOfAnalysts")), num(re_.get("numberOfAnalysts"))
        e = dict(p=t["period"], end=t.get("endDate"),
                 eps=dict(avg=rnd(ee.get("avg"), 4), lo=rnd(ee.get("low"), 4), hi=rnd(ee.get("high"), 4), ya=rnd(ee.get("yearAgoEps"), 4),
                          n=int(n_e), g=pctv(ee.get("growth"), 1), ccy=ee.get("earningsCurrency")) if n_e else None,
                 rev=dict(avg=sig(re_.get("avg")), lo=sig(re_.get("low")), hi=sig(re_.get("high")), ya=sig(re_.get("yearAgoRevenue")),
                          n=int(n_r), g=pctv(re_.get("growth"), 1), ccy=re_.get("revenueCurrency")) if n_r else None,
                 tr=dict(cur=rnd(et.get("current"), 4), d7=rnd(et.get("7daysAgo"), 4), d30=rnd(et.get("30daysAgo"), 4),
                         d60=rnd(et.get("60daysAgo"), 4), d90=rnd(et.get("90daysAgo"), 4)) if n_e else None,
                 rv=dict(u7=int(num(er.get("upLast7days")) or 0), u30=int(num(er.get("upLast30days")) or 0),
                         d7=int(num(er.get("downLast7Days")) or num(er.get("downLast7days")) or 0),
                         d30=int(num(er.get("downLast30days")) or 0)) if n_e else None)
        if e["eps"] or e["rev"]:
            est.append(e)
    hist = []
    for h in (qs.get("earningsHistory") or {}).get("history") or []:
        a, e_ = rnd(h.get("epsActual"), 4), rnd(h.get("epsEstimate"), 4)
        if a is None and e_ is None:
            continue
        hist.append(dict(q=iso_date(h.get("quarter")), act=a, est=e_, surp=pctv(h.get("surprisePercent"), 1), ccy=h.get("currency")))
    hist.sort(key=lambda x: x.get("q") or "")
    upg_all = sorted((qs.get("upgradeDowngradeHistory") or {}).get("history") or [], key=lambda x: -(x.get("epochGradeDate") or 0))
    cutoff = time.time() - 3 * 365 * 86400
    upg = [dict(d=iso_date(u.get("epochGradeDate")), firm=u.get("firm"), to=u.get("toGrade") or None, fr=u.get("fromGrade") or None,
                act=u.get("action"), pta=u.get("priceTargetAction") or None,
                pt=rnd(u.get("currentPriceTarget"), 4) or None, ppt=rnd(u.get("priorPriceTarget"), 4) or None)
           for u in upg_all if (u.get("epochGradeDate") or 0) >= cutoff and u.get("firm")][:N_UPG]
    upg_12m = sum(1 for u in upg_all if (u.get("epochGradeDate") or 0) >= time.time() - 365 * 86400)

    # calendar
    ce = cal.get("earnings") or {}
    ed = ce.get("earningsDate") or []
    dates = dict(
        earnings=iso_date(ed[0]) if ed else None,
        earnings_to=iso_date(ed[1]) if len(ed) > 1 else None,
        earnings_est=bool(ce.get("isEarningsDateEstimate")),
        call=iso_date((ce.get("earningsCallDate") or [None])[0]) if ce.get("earningsCallDate") else None,
        ex_div=iso_date(cal.get("exDividendDate")) or iso_date(sd.get("exDividendDate")),
        div_pay=iso_date(cal.get("dividendDate")),
        last_q=iso_date(ks.get("mostRecentQuarter")),
        fy_end=iso_date(ks.get("lastFiscalYearEnd")), next_fy_end=iso_date(ks.get("nextFiscalYearEnd")),
    )
    cal_est = dict(eps_avg=rnd(ce.get("earningsAverage"), 4), eps_lo=rnd(ce.get("earningsLow"), 4), eps_hi=rnd(ce.get("earningsHigh"), 4),
                   rev_avg=sig(ce.get("revenueAverage")), rev_lo=sig(ce.get("revenueLow")), rev_hi=sig(ce.get("revenueHigh")))

    # profile / people
    web = prof.get("website")
    web = web if web and re.match(r"^https?://", web) else None
    ir = prof.get("irWebsite")
    ir = ir if ir and re.match(r"^https?://", ir) else None
    profile = dict(sector=prof.get("sectorDisp") or prof.get("sector"), industry=prof.get("industryDisp") or prof.get("industry"),
                   country=prof.get("country"))
    officers = []
    for o in prof.get("companyOfficers") or []:
        if not o.get("name"):
            continue
        officers.append(dict(n=re.sub(r"\s+", " ", o["name"]).strip(), t=(o.get("title") or "").strip() or None,
                             age=int(o["age"]) if num(o.get("age")) else None, yb=int(o["yearBorn"]) if num(o.get("yearBorn")) else None,
                             pay=sig(o.get("totalPay")), fy=o.get("fiscalYear")))
    gov = dict(audit=prof.get("auditRisk"), board=prof.get("boardRisk"), comp=prof.get("compensationRisk"),
               rights=prof.get("shareHolderRightsRisk"), overall=prof.get("overallRisk"),
               d=iso_date(prof.get("governanceEpochDate")))
    if gov["overall"] is None:
        gov = None

    # ownership / insiders
    mh = qs.get("majorHoldersBreakdown") or {}
    own = dict(ins=pctv(mh.get("insidersPercentHeld"), 2), inst=pctv(mh.get("institutionsPercentHeld"), 2),
               inst_float=pctv(mh.get("institutionsFloatPercentHeld"), 2), inst_n=int(num(mh.get("institutionsCount")) or 0) or None)
    nsp = qs.get("netSharePurchaseActivity") or {}
    ins_net = dict(p=nsp.get("period"), buy_n=int(num(nsp.get("buyInfoCount")) or 0), buy_sh=sig(nsp.get("buyInfoShares")),
                   sell_n=int(num(nsp.get("sellInfoCount")) or 0), sell_sh=sig(nsp.get("sellInfoShares")),
                   net_sh=sig(nsp.get("netInfoShares")), tot=sig(nsp.get("totalInsiderShares")),
                   inst_net=sig(nsp.get("netInstSharesBuying")), inst_net_pct=pctv(nsp.get("netInstBuyingPercent"), 2))
    if not (ins_net["buy_n"] or ins_net["sell_n"] or ins_net["tot"] or ins_net["inst_net"]):
        ins_net = None
    tx = []
    for t in sorted((qs.get("insiderTransactions") or {}).get("transactions") or [], key=lambda x: -(num(x.get("startDate")) or 0)):
        txt = (t.get("transactionText") or "").strip()
        kind = ("buy" if re.search(r"\b(purchase|buy|bought|acquisition)\b", txt, re.I) and not re.search(r"\bsale\b", txt, re.I)
                else "sell" if re.search(r"\b(sale|sold|disposition|disposal)\b", txt, re.I)
                else "other")
        tx.append(dict(d=iso_date(t.get("startDate")), n=re.sub(r"\s+", " ", t.get("filerName") or "").strip(), rel=t.get("filerRelation") or None,
                       txt=txt or None, k=kind, sh=sig(t.get("shares")), val=sig(t.get("value")) or None))
        if len(tx) >= N_TX:
            break
    ih = []
    for h in (qs.get("insiderHolders") or {}).get("holders") or []:
        ih.append(dict(n=re.sub(r"\s+", " ", h.get("name") or "").strip(), rel=h.get("relation") or None,
                       desc=h.get("transactionDescription") or None, d=iso_date(h.get("latestTransDate")),
                       pos=sig(h.get("positionDirect")), pd=iso_date(h.get("positionDirectDate"))))
    ih.sort(key=lambda x: -(x.get("pos") or 0))

    # financial statements + ratios
    ratios_a = [ratio_row(x, annual[i - 1] if i else None) for i, x in enumerate(annual)]
    latest_bal = max([r for r in quarterly + annual if any(r.get(k) is not None for k in ("ta", "debt", "eq"))], key=lambda r: r["d"], default=None)
    ratios_t = ratio_row(dict(t12, d=t12.get("d")), None, latest_bal) if t12 else None
    if ratios_t is not None:
        ratios_t["bal_d"] = latest_bal and latest_bal["d"]

    # dividends per share (local trading currency, e.g. GBp pence for London)
    dv_pts, dv_ccy = divs or ([], None)
    by_year = {}
    for d, a in dv_pts:
        y = int(d[:4])
        by_year.setdefault(y, [0.0, 0])
        by_year[y][0] += a
        by_year[y][1] += 1
    dv = dict(ccy=dv_ccy or pccy, years=[[y, sig(v[0], 5), v[1]] for y, v in sorted(by_year.items())],
              last=[[d, sig(a, 5)] for d, a in dv_pts[-8:]][::-1],
              rate=rnd(sd.get("dividendRate"), 4), trail=rnd(sd.get("trailingAnnualDividendRate"), 4),
              yield5=rnd(sd.get("fiveYearAvgDividendYield"), 2), payout=pctv(sd.get("payoutRatio"), 1)) if (dv_pts or num(sd.get("dividendRate"))) else None

    chg_pct = num(price.get("regularMarketChangePercent"))
    lf = max([r["d"] for r in annual + quarterly], default=None)
    index = dict(
        sym=sym, name=spec["name"], long_name=price.get("longName") or price.get("shortName"),
        exch=price.get("exchangeName"), ccy=pccy, fin_ccy=fccy, px=rnd(px, 6),
        chg_pct=rnd(chg_pct * 100, 3) if chg_pct is not None else None,
        market_time=dt.datetime.fromtimestamp(price["regularMarketTime"], UTC).isoformat() if isinstance(price.get("regularMarketTime"), (int, float)) else None,
        val=val, mkt=mkt, cons=cons, dates=dates, profile=profile, fin_asof=lf,
        fetched_at=now.isoformat(timespec="seconds"),
    )
    detail = dict(
        sym=sym, v=2,
        about=dict(summary=re.sub(r"\s+", " ", prof.get("longBusinessSummary") or "").strip() or None,
                   lead=first_sentences(prof.get("longBusinessSummary")),
                   web=web, ir=ir, emp=int(num(prof.get("fullTimeEmployees"))) if num(prof.get("fullTimeEmployees")) else None,
                   city=prof.get("city"), state=prof.get("state"), country=prof.get("country")),
        officers=officers, gov=gov,
        own=own, inst=own_list(qs.get("institutionOwnership"), minor), funds=own_list(qs.get("fundOwnership"), minor),
        ins_net=ins_net, ins_tx=tx, ins_hold=ih[:15],
        an=dict(trend=trend, est=est, hist=hist, upg=upg, upg_12m=upg_12m, upg_total=len(upg_all)),
        cal=cal_est,
        fin=dict(ccy=fccy, annual=annual, quarterly=quarterly, ttm=t12 or None),
        ratios=dict(annual=ratios_a, ttm=ratios_t),
        divs=dv,
    )
    return clean(index), clean(detail)


def coverage(c, d):
    v, k = c.get("val") or {}, c.get("cons") or {}
    d = d or {}
    f = d.get("fin") or {}
    an = d.get("an") or {}
    return dict(val=any(v.get(x) is not None for x in ("pe", "pe_fwd", "ev_ebitda", "ev_rev", "pb")),
                cons=bool(k.get("analysts") or k.get("target")),
                fin=bool(f.get("annual") or f.get("quarterly")), fin_q=bool(f.get("quarterly")),
                officers=bool(d.get("officers")), gov=bool(d.get("gov")),
                holders=bool(d.get("inst") or d.get("funds")), insiders=bool(d.get("ins_tx") or d.get("ins_hold")),
                est=bool(an.get("est")), hist=bool(an.get("hist")), upg=bool(an.get("upg")), divs=bool(d.get("divs")),
                short=bool((c.get("mkt") or {}).get("short")))


# ------------------------------------------------------------------ main
def safe_name(sym):
    return re.sub(r"[^A-Za-z0-9._^=-]", "_", sym) + ".json"


def write_json(path, obj):
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    try:
        with open(path) as f:
            if f.read() == data:
                return False          # unchanged: keep mtime, no git churn
    except Exception:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, path)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "companies.json"))
    ap.add_argument("--only", default="")
    ap.add_argument("--extra", default="", help="extra Yahoo symbols (testing), name = symbol")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    now = dt.datetime.now(UTC)
    co_dir = os.path.join(os.path.dirname(a.out), "co")
    os.makedirs(co_dir, exist_ok=True)

    specs = {}
    for g in GROUPS:
        for sec in g["sections"]:
            for r in sec["rows"]:
                if r.get("kind", "equity") == "equity":
                    specs.setdefault(r["sym"], r)
    extra = [x.strip() for x in a.extra.split(",") if x.strip()]
    for s in extra:
        specs.setdefault(s, dict(sym=s, name=s, kind="equity"))
    syms = [s for s in specs if not only or s in only or s in extra]

    prev = {}
    try:
        with open(a.out) as f:
            prev = (json.load(f) or {}).get("companies") or {}
    except Exception:
        pass

    crumb = [yahoo_crumb(SESSION)]
    t0 = time.time()

    def work(sym):
        qs = ts = ts_ccy = divs = None
        err = []
        try:
            qs = fetch_summary(sym, crumb)
        except Exception as e:
            err.append(f"quoteSummary: {e}")
        if qs is not None:
            try:
                ts, ts_ccy = fetch_timeseries(sym, crumb)
            except Exception as e:
                err.append(f"timeseries: {e}")
            try:
                divs = fetch_divs(sym, crumb)
            except Exception as e:
                err.append(f"dividends: {e}")
        log(f"  {sym}: qs={'ok' if qs else 'FAIL'} ts={len(ts or {})} div={len((divs or [[]])[0])} {'; '.join(err)}")
        return sym, qs, ts, ts_ccy, divs, err

    raw, errors = {}, []
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        for sym, qs, ts, ts_ccy, divs, err in ex.map(work, syms):
            if qs is not None:
                raw[sym] = (qs, ts or {}, ts_ccy, divs)
            if err:
                errors.append(dict(sym=sym, error="; ".join(err)[:300]))
    t_fetch = time.time() - t0

    if len(raw) < MIN_OK_SHARE * len(syms) and prev:
        log(f"Only {len(raw)}/{len(syms)} fetched - keeping {a.out}")
        return 2

    needed = {"EUR", "USD"}
    for qs, ts, tc, _ in raw.values():
        for c in ((qs.get("price") or {}).get("currency"), (qs.get("financialData") or {}).get("financialCurrency"), tc):
            if c:
                needed.add(MAJOR.get(c, c))
    fx_table = build_fx_table(needed)
    fx = FX(fx_table)

    out_c, cov_rows, written, sizes = {}, {}, 0, []
    for sym in syms:
        spec = specs[sym]
        path = os.path.join(co_dir, safe_name(sym))
        if sym in raw:
            qs, ts, tc, divs = raw[sym]
            try:
                c, d = shape(sym, spec, qs, ts, tc, divs, fx, now)
                old_d = None
                if not ts or divs is None:
                    try:
                        with open(path) as f:
                            old_d = json.load(f)
                    except Exception:
                        old_d = None
                # a sub-fetch failed this run: keep the last good financials / dividends rather than blanking them
                if not ts and old_d and (old_d.get("fin") or {}).get("annual"):
                    d["fin"], d["ratios"] = old_d["fin"], old_d.get("ratios")
                    c["fin_stale"] = True
                    c["fin_asof"] = (prev.get(sym) or {}).get("fin_asof")
                if divs is None and old_d and old_d.get("divs"):
                    d["divs"] = old_d["divs"]
                c["detail"] = safe_name(sym)
                written += write_json(path, d)
                sizes.append(os.path.getsize(path))
                out_c[sym] = c
                cov_rows[sym] = coverage(c, d)
                continue
            except Exception as e:
                errors.append(dict(sym=sym, error=f"shape: {e}"[:300]))
                log(f"  {sym}: shape failed {e}")
        if sym in prev:
            c = dict(prev[sym])
            c["stale"] = True
            c["name"] = spec["name"]
            if os.path.exists(path):
                c["detail"] = safe_name(sym)
            out_c[sym] = c

    removed = 0
    if not only and not extra:
        keep = {safe_name(s) for s in out_c}
        for fn in os.listdir(co_dir):
            if fn.endswith(".json") and fn not in keep:
                os.remove(os.path.join(co_dir, fn))
                removed += 1

    keys = ("val", "cons", "fin", "fin_q", "officers", "gov", "holders", "insiders", "est", "hist", "upg", "divs", "short")
    cov = {k: sum(1 for v in cov_rows.values() if v[k]) for k in keys}
    out = dict(
        generated_utc=now.isoformat(timespec="seconds"),
        source="Yahoo Finance quoteSummary + fundamentals-timeseries + chart dividends. May be incomplete for small caps.",
        method=dict(
            ev="Market cap + net debt (latest reported total debt minus cash & short-term investments) + minority interest, "
               "reporting-currency balance sheet converted at the current EUR-cross; Yahoo enterpriseValue only when no balance sheet.",
            multiples="EV/EBITDA and EV/Revenue on trailing twelve months (both in reporting currency); P/E trailing and forward "
                      "as published by Yahoo; P/B = market cap / latest shareholders' equity. FCF yield = TTM free cash flow / "
                      "market cap; cash returned = TTM dividends paid + buybacks / market cap.",
            financials="Reporting currency. Net debt = total debt minus cash & short-term investments. FCF = Yahoo free cash flow "
                       "(operating cash flow + capex).",
            units="Market cap / EV / targets / price in trading currency (GBp quotes: market cap in GBP). *_eur converted to EUR.",
            detail="Per-company detail in data/co/<file> (officers, governance, holders, insiders, analysts, full statements, dividends).",
        ),
        fx_eur={k: v.get("rate") for k, v in fx_table.items()},
        count=len(out_c), coverage=cov, seconds=round(time.time() - t0, 1), fetch_seconds=round(t_fetch, 1),
        companies=out_c, errors=errors,
    )
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    write_json(a.out, out)
    log(f"wrote {a.out}: {len(out_c)} companies, {os.path.getsize(a.out)//1024} KB; detail files {len(sizes)} "
        f"({sum(sizes)//1024} KB total, max {max(sizes or [0])//1024} KB, {written} changed, {removed} removed); "
        f"coverage {cov}; {len(errors)} errors; {out['seconds']}s (fetch {out['fetch_seconds']}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""ENTSO-E Transparency Platform client shared by the build scripts. The token comes from ENTSOE_API_TOKEN and is
never printed (errors pass through _redact). Thread-safe: requests are throttled globally (_ENT_GAP), at most 8 in
flight, one HTTP session per thread; pmap() runs independent queries in parallel."""
import datetime as dt, io, os, re, threading, time
from zoneinfo import ZoneInfo

import pandas as pd

try:
    from curl_cffi import requests as http
    S = http.Session(impersonate="chrome")
except Exception:  # pragma: no cover
    import requests as http
    S = http.Session()
    S.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36"

UTC = dt.timezone.utc
CET = ZoneInfo("Europe/Zurich")  # CET/CEST = Nord Pool delivery-day convention


def _secrets():
    return [t for t in ((os.environ.get(k) or "").strip() for k in ("ENTSOE_API_TOKEN", "FINGRID_API_KEY", "FINGRID_API_KEY_SECONDARY")) if t]


ENTSOE_API = "https://web-api.tp.entsoe.eu/api"
ENTSOE_EIC = {"NO1": "10YNO-1--------2", "NO2": "10YNO-2--------T", "NO3": "10YNO-3--------J",
              "NO4": "10YNO-4--------9", "NO5": "10Y1001A1001A48H",
              "SE1": "10Y1001A1001A44P", "SE2": "10Y1001A1001A45N", "SE3": "10Y1001A1001A46L", "SE4": "10Y1001A1001A47J",
              "FI": "10YFI-1--------U", "DK1": "10YDK-1--------W", "DK2": "10YDK-2--------M",
              "DE-LU": "10Y1001A1001A82H", "PL": "10YPL-AREA-----S", "LT": "10YLT-1001A0008Q"}
SE4_BORDERS = ["SE3", "DK2", "DE-LU", "PL", "LT"]
RES_ZONES = ["SE1", "SE2", "SE3", "SE4", "NO1", "NO2", "NO3", "NO4", "NO5"]
PSR = {"B19": "wind", "B18": "wind", "B16": "solar", "B14": "nuclear", "B12": "hydro", "B11": "hydro",
       "B10": "hydro", "B04": "gas", "B05": "coal", "B06": "oil", "B01": "biomass", "B17": "waste", "B20": "other"}
_RES = {"PT1M": 1, "PT15M": 15, "PT30M": 30, "PT60M": 60, "P1D": 1440, "P7D": 10080}
_ENT_GAP = 0.25          # seconds between requests (<= 240/min, limit is 400/min)
_ent_last = [0.0]
_ent_lock = threading.Lock()
_ent_tl = threading.local()


def _sess():
    """One HTTP session per thread (sessions are not thread-safe)."""
    if not hasattr(_ent_tl, "s"):
        try:
            _ent_tl.s = http.Session(impersonate="chrome")
        except TypeError:
            _ent_tl.s = http.Session(); _ent_tl.s.headers.update(S.headers)
    return _ent_tl.s


_ent_sem = threading.BoundedSemaphore(8)   # at most 8 ENTSO-E requests in flight


def pmap(fn, items, workers=6):
    """Ordered parallel map for independent ENTSO-E queries (I/O bound)."""
    from concurrent.futures import ThreadPoolExecutor
    items = list(items)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as ex:
        return list(ex.map(fn, items))


def _token():
    return (os.environ.get("ENTSOE_API_TOKEN") or "").strip()


def _redact(msg):
    msg = str(msg)
    for t in _secrets():
        msg = msg.replace(t, "***")
    return re.sub(r"(securityToken=)[^&\s'\"]+", r"\1***", msg)


def _strip_ns(xml_text):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _ack(root):
    if root.tag.startswith("Acknowledgement"):
        reason = " ".join((t.text or "") for t in root.iter("text")).strip()
        raise LookupError(reason[:160] or "no data")


def _periods(ts):
    """Yield pd.Series (UTC) for each Period of a TimeSeries. A03 curves: omitted positions repeat the previous
    value. Prices use <price.amount>, everything else <quantity>."""
    for per in ts.iter():
        if per.tag not in ("Period", "Available_Period"):   # unavailability docs use Available_Period
            continue
        start = pd.Timestamp(per.findtext("timeInterval/start")).tz_convert("UTC")
        end = pd.Timestamp(per.findtext("timeInterval/end")).tz_convert("UTC")
        step = _RES.get(per.findtext("resolution") or "", 60)
        n = max(1, int(round((end - start).total_seconds() / (step * 60))))
        pts = {}
        for p in per.iter("Point"):
            v = p.findtext("quantity")
            if v is None:
                v = p.findtext("price.amount")
            if v is not None:
                pts[int(p.findtext("position"))] = float(v)
        vals, last = [], None
        for i in range(1, n + 1):
            last = pts.get(i, last)
            vals.append(last)
        idx = pd.date_range(start, periods=n, freq=f"{step}min")
        yield step, pd.Series(vals, index=idx, dtype="float64")


def entsoe_parse(xml_text):
    """-> list of dicts {psr, is_in, step, s} (one per TimeSeries, periods concatenated)."""
    root = _strip_ns(xml_text)
    _ack(root)
    out = []
    for ts in root.iter("TimeSeries"):
        parts = list(_periods(ts))
        if not parts:
            continue
        s = pd.concat([p[1] for p in parts]).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        out.append(dict(psr=ts.findtext("MktPSRType/psrType"),
                        is_in=ts.find("inBiddingZone_Domain.mRID") is not None or ts.find("outBiddingZone_Domain.mRID") is None,
                        step=min(p[0] for p in parts), s=s))
    return out


def entsoe_raw(params, tries=4):
    token = _token()
    if not token:
        raise PermissionError("ENTSOE_API_TOKEN not set")
    last = None
    for i in range(tries):
        with _ent_lock:
            wait = _ENT_GAP - (time.time() - _ent_last[0])
            if wait > 0:
                time.sleep(wait)
            _ent_last[0] = time.time()
        try:
            with _ent_sem:
                r = _sess().get(ENTSOE_API, params=dict(params, securityToken=token), timeout=90)
        except Exception as e:
            last = IOError(_redact(f"{type(e).__name__}: {e}")[:200])
            time.sleep(3 * (i + 1))
            continue
        if r.status_code == 401:
            raise PermissionError("ENTSO-E rejected the token (HTTP 401)")
        if r.status_code == 429 or r.status_code >= 500:
            last = IOError(f"HTTP {r.status_code}")
            time.sleep(min(60, float(r.headers.get("Retry-After") or 0) or 6 * (i + 1)))
            continue
        if r.status_code not in (200, 400):
            raise IOError(f"HTTP {r.status_code}")
        return r
    raise last


def entsoe_get(params):
    r = entsoe_raw(params)
    return entsoe_parse(r.text)


def entsoe_docs(params):
    """Unavailability queries (A78/A80) answer with a zip of XML documents (or a single XML / acknowledgement)."""
    import zipfile
    r = entsoe_raw(params)
    if r.content[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(r.content))
        return [_strip_ns(z.read(n)) for n in z.namelist()]
    root = _strip_ns(r.text)
    _ack(root)
    return [root]


def _ts(t):
    return t.isoformat().replace("+00:00", "Z")


def _cet_bounds(d0, d1):
    """CET days [d0, d1) -> UTC Timestamps."""
    a = dt.datetime.combine(d0, dt.time(), CET).astimezone(UTC)
    b = dt.datetime.combine(d1, dt.time(), CET).astimezone(UTC)
    return pd.Timestamp(a), pd.Timestamp(b)


def _period(d0, d1):
    a, b = _cet_bounds(d0, d1)
    return dict(periodStart=a.strftime("%Y%m%d%H%M"), periodEnd=b.strftime("%Y%m%d%H%M"))


def _combine(items):
    """Several TimeSeries for the same quantity (e.g. revisions or mixed resolutions): finest resolution first."""
    s = None
    for it in sorted(items, key=lambda x: x["step"]):
        s = it["s"] if s is None else s.combine_first(it["s"])
    return s


def _sum(items):
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    df = pd.concat(items, axis=1).sort_index()
    df = df.ffill(limit=3)  # hourly series on a 15-min grid
    return df.sum(axis=1, min_count=1)

#!/usr/bin/env python3
"""Build data/news.json: latest headlines per Table tab (plus the Nordic Power tab, key "power").

Sources (all free, no scraping of article pages, headline + link only):
  * Google News RSS search (news.google.com/rss/search), per company/topic query
  * Oslo Børs NewsWeb public list API (exchange announcements for Oslo issuers)
  * Nasdaq Nordic company news API (exchange announcements for Nasdaq Stockholm/Copenhagen/Helsinki)
FT links are excluded. Items are de-duplicated by normalised headline and capped per tab.
"""
import argparse, datetime as dt, email.utils, html, json, os, random, re, sys, time, unicodedata
from xml.etree import ElementTree as ET

try:
    from curl_cffi import requests as http
    S = http.Session(impersonate="chrome")
except Exception:  # pragma: no cover
    import requests as http
    S = http.Session()
    S.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTC = dt.timezone.utc
PER_TAB = 20
EXCLUDE_SOURCES = re.compile(r"financial times|\bft\.com\b", re.I)
EXCLUDE_URLS = re.compile(r"(^|//|\.)ft\.com", re.I)
# quote/chart landing pages that Google News sometimes returns as "articles"
JUNK_TITLES = re.compile(r"stock price, news, quote|quote & history|price chart\b|stock quote|share price & news|"
                         r"stock forecast & analyst|actuals & estimates|trade ideas —|return on invested capital|"
                         r"\) stock price$|^[\w .&-]+ (plc|asa|ab|inc\.?)$", re.I)

# (query, must-match regex on headline or None, lookback days)
G = lambda q, must=None, days=14: dict(q=q, must=must, days=days)
TABS = {
    "nordic": dict(
        google=[G("Cloudberry Clean Energy", r"cloudberry", 60), G("Scatec", r"scatec", 21), G("Eolus", r"eolus", 45),
                G('"Orrön Energy" OR "Orron Energy"', r"orr[oö]n", 45), G("Ørsted OR Orsted", r"[øo]rsted", 7),
                G("Vestas", r"vestas", 7), G("Fortum", r"fortum", 14), G('"Nord Pool" OR "Nordic power"', r"nordic|nord pool|norw|swed|finland|denmark", 21)],
        newsweb=["CLOUD", "SCATC"], nasdaq=["Eolus Aktiebolag", "Orrön Energy AB"]),
    "power": dict(
        google=[G('"Nordic power" OR "Nordic electricity" OR "Nordic energy"', r"^(?!.*base (week|month|quarter|year) future)(?=.*(nordic|nord pool|norw|swed|finland|finnish|danish|denmark))(?=.*(power|electric|price|energy))", 30),
                G('"Nord Pool"', r"nord pool|power|electric|price", 14),
                G("Statnett", r"statnett", 21), G('"Svenska kraftnät" OR "Svenska kraftnat"', r"svenska kraftn|kraftnät", 30),
                G("Fingrid OR Energinet electricity", r"fingrid|energinet", 21),
                G("Norway hydro reservoir", r"^(?!.*norsk hydro)(?=.*(norw|nordic|swed|scandinav))(?=.*(reservoir|hydro|magasin))", 45),
                G("Swedish nuclear outage Forsmark OR Ringhals OR Oskarshamn", r"forsmark|ringhals|oskarshamn|olkiluoto|loviisa|^(?=.*(swed|finland|finnish|nordic)).*(nuclear|reactor)", 30),
                G("Olkiluoto", r"olkiluoto|^(?=.*(finland|finnish)).*(nuclear|reactor)", 30)],
        newsweb=[], nasdaq=[]),
    "energy": dict(
        google=[G("OPEC", r"opec", 5), G('"Brent crude"', r"brent|oil", 5), G("Equinor", r"equinor", 10),
                G('"Aker BP" OR "Vår Energi"', r"aker bp|v[åa]r energi", 14), G("Shell OR TotalEnergies OR BP oil major", r"shell|totalenergies|\bbp\b", 7),
                G("European gas prices TTF", r"gas|ttf|lng", 7)],
        newsweb=["AKRBP", "VAR"], nasdaq=[]),
    "lundin": dict(
        google=[G('"Lundin Mining"', r"lundin", 21), G('"Lundin Gold"', r"lundin", 21), G('"International Petroleum Corp"', r"international petroleum|ipc", 30),
                G('"NGEx Minerals"', r"ngex", 30), G('"Lucara Diamond"', r"lucara", 30), G('"Montage Gold"', r"montage", 30),
                G('"ShaMaran"', r"shamaran", 45), G('"LunR Royalties"', r"lunr", 60)],
        newsweb=["SNM"], nasdaq=["International Petroleum Corporation", "Lundin Mining Corporation", "Lundin Gold Inc.",
                                  "Lucara Diamond Corp", "Orrön Energy AB"]),
    "metals": dict(
        google=[G("copper price", r"copper", 5), G("gold price", r"gold", 4), G("Glencore OR \"Rio Tinto\" OR BHP OR \"Anglo American\"", r"glencore|rio tinto|bhp|anglo", 7),
                G("Boliden OR \"Norsk Hydro\"", r"boliden|hydro", 14), G("mining merger acquisition", r"mining|miner", 7)],
        newsweb=[], nasdaq=[]),
    "commodities": dict(
        google=[G('"Brent crude"', r"brent|oil", 4), G("OPEC", r"opec", 5), G("TTF gas Europe", r"gas|ttf", 5),
                G("gold price", r"gold", 4), G("uranium price", r"uranium", 14), G("EU carbon price EUA", r"carbon|eua|emission", 14)],
        newsweb=[], nasdaq=[]),
    "indices": dict(
        google=[G("European stocks STOXX 600", r"stoxx|europe|stocks|shares", 3), G("Wall Street stocks S&P 500", r"s&p|wall street|stocks|nasdaq|dow", 3),
                G("Swiss stocks SMI", r"smi|swiss|stocks", 5), G("Nordic stocks Oslo Stockholm", r"oslo|stockholm|nordic|copenhagen|helsinki", 7),
                G("Asian stocks Nikkei Hang Seng", r"nikkei|hang seng|asia", 3)],
        newsweb=[], nasdaq=[]),
    "rates": dict(
        google=[G("Federal Reserve rates", r"fed|federal reserve|rate", 5), G("ECB interest rates", r"ecb|lagarde|rate", 7),
                G("Swiss National Bank", r"snb|swiss national bank|franc", 14), G("Treasury yields", r"yield|treasur", 4),
                G("Swiss franc", r"franc|chf", 7), G("dollar index forex", r"dollar|fx|currenc|forex", 4)],
        newsweb=[], nasdaq=[]),
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, method="GET", tries=4, **kw):
    err = None
    for i in range(tries):
        try:
            r = S.request(method, url, timeout=30, **kw)
            if r.status_code == 200:
                return r
            err = IOError(f"HTTP {r.status_code}")
        except Exception as e:
            err = e
        time.sleep(min(30, 2 ** (i + 1)) + random.uniform(0, 1))
    raise err


def norm(t):
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()[:90]


def fold(t):
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower() + " " + t.lower()


def google(q, must, days, limit=10):
    r = get("https://news.google.com/rss/search",
            params={"q": f"{q} when:{days}d", "hl": "en-GB", "gl": "GB", "ceid": "GB:en"})
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        title = html.unescape(it.findtext("title") or "").strip()
        src_el = it.find("source")
        src = (src_el.text or "").strip() if src_el is not None else ""
        src_url = src_el.get("url", "") if src_el is not None else ""
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3].strip()
        link = (it.findtext("link") or "").strip()
        if EXCLUDE_SOURCES.search(src) or EXCLUDE_URLS.search(src_url) or EXCLUDE_URLS.search(link):
            continue
        if JUNK_TITLES.search(title):
            continue
        if must and not re.search(must, fold(title), re.I):
            continue
        try:
            ts = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone(UTC)
        except Exception:
            continue
        out.append(dict(title=title, source=src or "Google News", url=link, time=ts.isoformat(), kind="news"))
        if len(out) >= limit:
            break
    return out


def newsweb(issuer, limit=8):
    r = get(f"https://api3.oslo.oslobors.no/v1/newsreader/list?category=&issuer={issuer}&fromDate=&toDate=&market=&messageTitle=",
            method="POST")
    out = []
    for m in r.json()["data"]["messages"][:limit]:
        out.append(dict(title=m["title"].strip(), source=f"Oslo Børs NewsWeb · {m.get('issuerSign', issuer)}",
                        url=f"https://newsweb.oslobors.no/message/{m['messageId']}",
                        time=dt.datetime.fromisoformat(m["publishedTime"].replace("Z", "+00:00")).astimezone(UTC).isoformat(),
                        kind="exchange"))
    return out


def nasdaq(company, limit=8):
    r = get("https://api.news.eu.nasdaq.com/news/query.action", params=dict(
        type="json", showAttachments="false", showCnsSpecific="true", showCompany="true", countResults="false",
        freeText="", company=company, market="", cnscategory="", globalGroup="exchangeNotice",
        globalName="NordicAllMarkets", displayLanguage="en", language="en", timeZone="UTC",
        dateMask="yyyy-MM-dd HH:mm:ss", limit=str(limit), start="0", dir="DESC"))
    out = []
    for m in r.json()["results"]["item"]:
        ts = dt.datetime.strptime(m["releaseTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        out.append(dict(title=m["headline"].strip(), source=f"Nasdaq Nordic · {m.get('company', company)}",
                        url=m["messageUrl"], time=ts.isoformat(), kind="exchange"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "news.json"))
    a = ap.parse_args()
    now = dt.datetime.now(UTC)
    tabs, status = {}, []
    for tab, cfg in TABS.items():
        items = []
        for g in cfg["google"]:
            try:
                got = google(g["q"], g["must"], g["days"])
                status.append(dict(tab=tab, feed="google", query=g["q"], ok=True, n=len(got)))
                items += [dict(x, topic=re.sub(r'["]', "", g["q"]).split(" OR ")[0]) for x in got]
            except Exception as e:
                status.append(dict(tab=tab, feed="google", query=g["q"], ok=False, error=str(e)[:200]))
            time.sleep(0.6 + random.uniform(0, 0.6))
        for iss in cfg["newsweb"]:
            try:
                got = newsweb(iss)
                status.append(dict(tab=tab, feed="newsweb", query=iss, ok=True, n=len(got)))
                items += got
            except Exception as e:
                status.append(dict(tab=tab, feed="newsweb", query=iss, ok=False, error=str(e)[:200]))
        for co in cfg["nasdaq"]:
            try:
                got = nasdaq(co)
                status.append(dict(tab=tab, feed="nasdaq", query=co, ok=True, n=len(got)))
                items += got
            except Exception as e:
                status.append(dict(tab=tab, feed="nasdaq", query=co, ok=False, error=str(e)[:200]))
        # newest first, de-duplicate by normalised headline and by URL
        items.sort(key=lambda x: x["time"], reverse=True)
        seen, keep, bags = set(), [], []
        for it in items:
            k = norm(it["title"])
            if not k or k in seen or it["url"] in seen:
                continue
            bag = set(k.split())
            # near-duplicates (same story, slightly different headline from another outlet)
            if any(len(bag & o) / max(1, len(bag | o)) >= 0.6 for o in bags):
                continue
            seen.add(k); seen.add(it["url"]); bags.append(bag)
            keep.append(it)
        # diversity: round-robin across topics (each exchange feed is its own topic), newest first within each
        by = {}
        for it in keep:
            by.setdefault(it["source"] if it["kind"] == "exchange" else it.get("topic", ""), []).append(it)
        sel, i = [], 0
        while len(sel) < PER_TAB and any(i < len(v) for v in by.values()):
            for k, v in by.items():
                if i < len(v) and len(sel) < PER_TAB:
                    sel.append(v[i])
            i += 1
        sel.sort(key=lambda x: x["time"], reverse=True)
        tabs[tab] = sel
        log(f"{tab}: {len(items)} raw -> {len(sel)} kept")
    ok = sum(1 for s in status if s["ok"])
    if ok == 0:
        log("no feed worked - not overwriting")
        return 2
    out = dict(generated_utc=now.isoformat(timespec="seconds"), tabs=tabs, feeds=status,
               note="Headlines and links only. Google News RSS + official exchange feeds. FT excluded.")
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out + ".tmp", "w") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(a.out + ".tmp", a.out)
    log(f"wrote {a.out}: {ok}/{len(status)} feeds ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

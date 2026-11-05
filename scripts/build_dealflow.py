#!/usr/bin/env python3
"""Build data/dealflow.json: material corporate announcements (last 30 days) for the site's equities.

Coverage: Renewables & Nordic (incl. strategic peers), Oil & gas, London-listed E&Ps and the Lundin Group.
Sources (keyless, public; headline + link, plus the release text where needed for classification hints / €/MW):
  * Oslo Børs NewsWeb list + message API (api3.oslo.oslobors.no)  -> Oslo issuers
  * MFN JSON feeds (mfn.se/all/a/<slug>.json)                       -> Swedish issuers, Stockholm-listed
    Canadians (Lundin Group), Ørsted, Fortum
  * Nasdaq Nordic company news (GlobeNewswire / api.news.eu.nasdaq.com) -> Vestas
  * Investegate company pages (RNS / EQS / other regulatory feeds)  -> London names
  * Yahoo Finance search news (media headlines; only typed, material items kept) -> other EU / US / Canada names
  * Finansinspektionen insider register CSV (marknadssok.fi.se)    -> Swedish insider transactions
Classification is a fixed, ordered list of keyword rules (see RULES / ROUTINE below); no models.
Previously processed URLs are reused from the last file, so release texts are fetched only once.
"""
import argparse, csv, datetime as dt, html, io, json, os, random, re, sys, threading, time, unicodedata
from concurrent.futures import ThreadPoolExecutor

try:
    from curl_cffi import requests as http
    IMP = dict(impersonate="chrome")
except Exception:  # pragma: no cover
    import requests as http
    IMP = {}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from universe import GROUPS  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTC = dt.timezone.utc
DAYS = 30            # feed window
FI_DAYS = 90         # Swedish insider register window (company page)
PER_CO = 25          # max kept items per company
WORKERS = 4

# ---------------------------------------------------------------- coverage
GROUP_LABEL = {"ren": "Renewables & Nordic", "og": "Oil & gas", "lse": "London E&Ps", "lun": "Lundin Group"}
NEWSWEB = {}  # filled from .OL symbols (issuer sign = ticker)
MFN = {"EOLU-B.ST": "eolus", "ORRON.ST": "orron-energy", "EQT.ST": "eqt", "ORSTED.CO": "orsted", "FORTUM.HE": "fortum",
       "IPCO.TO": "international-petroleum", "LUN.TO": "lundin-mining", "LUG.TO": "lundin-gold", "LUC.TO": "lucara-diamond"}
NASDAQ = {"VWS.CO": "Vestas Wind Systems A/S"}
FI = {"EOLU-B.ST": "Eolus", "ORRON.ST": "Orrön Energy", "EQT.ST": "EQT AB", "IPCO.TO": "International Petroleum",
      "LUN.TO": "Lundin Mining", "LUG.TO": "Lundin Gold", "LUC.TO": "Lucara"}
YAHOO_Q = {"RWE.DE": "RWE AG", "EDPR.LS": "EDP Renovaveis", "ANE.MC": "Acciona Energia", "SLR.MC": "Solaria Energia",
           "GRE.MC": "Grenergy", "ERG.MI": "ERG SpA", "VLTSA.PA": "Voltalia", "NDX1.DE": "Nordex", "VER.VI": "Verbund",
           "TTE.PA": "TotalEnergies", "BEP": "Brookfield Renewable", "KKR": "KKR", "XOM": "Exxon Mobil", "COP": "ConocoPhillips",
           "CNQ.TO": "Canadian Natural Resources", "TOU.TO": "Tourmaline Oil", "NGEX.TO": "NGEx Minerals",
           "MAU.TO": "Montage Gold", "LUNR.TO": "LunR Royalties"}
YAHOO_NAME = {"XOM": "Exxon", "EDPR.LS": "EDP", "CNQ.TO": "Canadian Natural"}


def companies():
    cos = {}
    for g in GROUPS:
        if g["id"] not in ("nordic", "energy", "lundin"):
            continue
        for sec in g["sections"]:
            lab = sec.get("label") or ""
            if g["id"] == "nordic" and lab == "Benchmarks":
                continue
            key = {"nordic": "ren", "lundin": "lun"}.get(g["id"]) or ("lse" if "London" in lab else "og")
            for r in sec["rows"]:
                if r["kind"] != "equity":
                    continue
                c = cos.setdefault(r["sym"], dict(n=r["name"], g=[]))
                if key not in c["g"]:
                    c["g"].append(key)
    for s in cos:
        if s.endswith(".OL"):
            NEWSWEB[s] = s[:-3]
    return cos


def source_of(sym):
    if sym in NEWSWEB: return "newsweb"
    if sym in MFN: return "mfn"
    if sym in NASDAQ: return "nasdaq"
    if sym.endswith(".L"): return "investegate"
    if sym in YAHOO_Q: return "yahoo"
    return None


# ---------------------------------------------------------------- http
_tl = threading.local()
_pace_lock = threading.Lock()
_last = {}


def sess():
    s = getattr(_tl, "s", None)
    if s is None:
        s = _tl.s = http.Session(**IMP)
        if not IMP:
            s.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36"
    return s


def pace(host, gap):
    with _pace_lock:
        now = time.time(); wait = _last.get(host, 0) + gap - now
        _last[host] = max(now, _last.get(host, 0) + gap)
    if wait > 0:
        time.sleep(wait)


GAPS = {"api3.oslo.oslobors.no": 0.25, "mfn.se": 0.3, "www.investegate.co.uk": 0.6, "api.news.eu.nasdaq.com": 0.3,
        "view.news.eu.nasdaq.com": 0.4, "query2.finance.yahoo.com": 0.4, "marknadssok.fi.se": 0.6}


def get(url, method="GET", tries=3, **kw):
    host = re.sub(r"^https?://([^/]+).*", r"\1", url)
    err = None
    for i in range(tries):
        pace(host, GAPS.get(host, 0.3))
        try:
            r = sess().request(method, url, timeout=30, **kw)
            if r.status_code == 200:
                return r
            err = IOError(f"HTTP {r.status_code}")
            if r.status_code in (403, 404):
                break
        except Exception as e:
            err = e
        time.sleep(1.5 * (i + 1) + random.uniform(0, 0.5))
    raise err


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def text_of(h):
    h = re.sub(r"(?is)<(script|style|table)[^>]*>.*?</\1>", " ", h or "")
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", " ", h))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def iso(d):
    return d.astimezone(UTC).isoformat(timespec="minutes").replace("+00:00", "Z")


# ---------------------------------------------------------------- classification rules
# Routine notices are not shown individually; they are counted per company (collapsed).
ROUTINE = [
    ("buyback", "Share buyback reports", r"transactions? in own shares|purchase of own shares|repurchase of (own )?shares|share buy-?backs?|buy-?back (programme|program|tranche|update|transactions|report)|off[- ]market buyback|"
                r"buy-?back (of shares )?(update|transactions|report)|weekly report on share buy|(acquisition|purchase)s? of own shares|changes in company'?s own shares|"
                r"share repurchase (programme|program)? ?(update|transactions)|normal course issuer bid|\bncib\b|issuer bid|buy-?back of shares to share programmes?|aksjeprogram for ansatte|"
                r"aktier? (i|av) egna|repurchases of shares by|återköp av (egna )?aktier|tilbakekjøp av egne aksjer|kjøp av egne aksjer"),
    ("holdings", "Voting rights and holdings", r"total voting rights|holding\(s\) in company|holdings? in company|major shareholding|disclosure of (major )?(share)?holding|"
                 r"notification of major (share)?holdings?|flagging|flaggemelding|block listing|rule 2\.9|form 8\b|form 8\.[35]|form 8 \(opd\)|\b8\.3\b|"
                 r"major shareholder announcement|voting rights and capital|share capital and votes|number of shares and votes|antal aktier och röster|storaktionær|"
                 r"share capital (and voting rights )?update|updated share capital|share capital and voting|rentefastsettelse|interest rate (fixing|reset)|coupon (fixing|reset)"),
    ("meeting", "AGM and meeting notices", r"\bagm\b|annual general meeting|extraordinary general meeting|general meeting|\begm\b|notice of (annual|extraordinary)|"
                r"result of (the )?(annual )?general meeting|annual (and special )?meeting|special meeting|meeting (voting )?results|kallelse|årsstämma|bolagsstämma|"
                r"generalforsamling|nomination committee|valberedning"),
    ("dividend", "Dividend notices", r"dividend (exchange rate|payment|currency|declaration|reinvestment|timetable)|ex-dividend|trade ex-div|payment of .*dividends?|"
                 r"equivalent dividend|scrip (dividend|reference)|dividends? in (sterling|euro)|interim dividend (declaration|exchange)|utbytte|osinko"),
    ("calendar", "Calendar, invitations and presentations", r"appointment of (ceo|cfo|coo|chair|director|board)|resignation of (board|director|ceo|cfo)|board changes|executive management team|invitation to|invites to|inbjudan|investor (presentation|meetings?|day)|to present at|\bimc\b|notice of (interim|half|full|annual|q[1-4]) results|webcast|conference call|financial calendar|presentation (at|of)|presenting at|"
                 r"capital markets? (day|update)|to (host|publish|announce|release) (its )?(q[1-4]|first|second|third|fourth|half|full|interim|annual)|"
                 r"date (of|for) (the )?(publication|results|report)|notice of results|results date|key information relating to|ex[- ]dividend date|"
                 r"dividend (record|payment) date|net asset value|\bnav\b|director declaration|change of (registered office|adviser|nomad|broker|name)|"
                 r"publication of (annual|half|the) .*report.*(available|published)|annual report .*(published|available)|prospectus (approved|published)|listing of new shares|admission of new shares|"
                 r"application for admission|issue of equity for employee|grant of .*(awards?|options|rsus?|psus?)\b|exercise of (stock |share )?options|employee share|lti(p)? award|"
                 r"share sav(ing|ings) (plan|programme|program)|aksjespareprogram|osakesäästöohjelma|tulosjulkistamisajankohdat|financial reporting dates|reporting dates"),
]
ROUTINE_RE = [(k, lab, re.compile(p, re.I)) for k, lab, p in ROUTINE]

# Ordered: the first matching rule decides the type. Patterns run on the (diacritic-folded, lower-case) headline,
# plus the source's own category / tags where noted.
RULES = [
    ("insider", "Insider trade", r"mandatory notification of trade|primary insider|pdmr|director(s)?/pdmr|director'?s? (dealing|shareholding|share purchase)|directors? dealings?|"
                r"persons? discharging managerial|managerial responsibilities|insider (purchase|transaction|trade|buy|sale)|share purchase by (the )?(ceo|cfo|chair|board|management)|"
                r"(ceo|cfo|chair(man)?|board member|director) (buys|purchases|acquires|sells) (shares|.* shares)|insynshandel|meldepliktig handel|close associate"),
    ("ppa", "PPA / offtake", r"\bppas?\b|power purchase agreement|offtake|off-take|contract for difference|\bcfds?\b|(power|electricity|energy) (sales|supply) (agreement|contract)|"
            r"long-term (power|electricity|energy|gas) (contract|agreement|supply)|tolling agreement|gas sales agreement|\bgsa\b|crude (oil )?(sales|offtake)|virtual ppa|\bvppa\b|"
            r"capacity (market|contract|auction) (award|win|agreement)"),
    ("ma", "M&A / divestment", r"acqui(re|res|red|sition|sitions)|merger|\bmerge\b|takeover|take-over|tender offer|public offer(?!ing)|(cash|share|all-share|recommended|possible|firm|increased|final) offer|"
           r"offer (for|to acquire|by)|scheme of arrangement|scheme document|rule 2\.7|rule 2\.4|business combination|divest|disposal|dispose|\bsells?\b|\bsold\b|sale of|"
           r"farm[- ]?(in|out|down)|transfer of (interest|licen[cs]e|operatorship)|(buys|purchases) (a |an |the )?(stake|interest|portfolio|project|company|business|asset)|"
           r"joint venture|\bjv\b|strategic (review|partnership|investment)|carve-?out|demerger|spin-?off|exit from|completion of (the )?(sale|acquisition|transaction)|"
           r"completes? (the )?(sale|acquisition|transaction|merger)|investment in [a-z]|(enters|entered) into (an )?agreement to (sell|acquire|buy)|forvarv|förvärv|avyttr|oppkjøp|oppkjop|salg av"),
    ("raise", "Capital raise", r"private placement|initial public offering|\bipo\b|rights issue|share issue|directed (share )?issue|equity (raise|issue|offering)|\bplacing\b|subscription (period|price|rights)|"
              r"issue of (new )?shares|capital increase|fund ?rais|open offer|retail offer|bookbuild|accelerated book|warrants? (exercise|issue)|subsequent offering|repair offering|"
              r"\bemission\b|nyemission|riktad|rettet emisjon|kapitalforhøyelse|kapitalforhoyelse|spin-off offering"),
    ("financing", "Financing / debt", r"\bbonds?\b|\bnotes\b|\bnotes? (issue|offering|due)|senior (secured |unsecured )?notes|tap issue|\bloan\b|credit facilit|facility agreement|\brbl\b|reserve[- ]based|"
                  r"refinanc|financing|debt (facility|financing|raise)|green (bond|loan|financing)|convertible|term loan|revolving|\brcf\b|hybrid (capital|bond)|project finance|"
                  r"obligasjon|obligationslån|grönt lån|lånefinansiering"),
    ("project", "Project / FID / operations", r"\b(drill(ing)?|assay|metallurgical|exploration|test) results\b"),
    ("results", "Results / guidance", r"\bresults?\b|interim (report|statement|management)|quarterly report|half[- ]year|half year|annual report|full[- ]year|\bq[1-4] 20\d\d|\bq[1-4]\b|"
                r"(first|second|third|fourth) quarter|trading (update|statement)|operational update|operations update|production (update|report|figures|volumes)|"
                r"guidance|outlook|profit warning|preliminary|delårsrapport|delarsrapport|bokslutskommuniké|kvartalsrapport|halvårsrapport|kvartalsrapport|"
                r"monthly (production|operating|sales)|traffic figures|reserves (report|update)|cpr\b|competent person"),
    ("project", "Project / FID / operations", r"\bfid\b|final investment decision|investment decision|commission|commercial operation|\bcod\b|first (oil|gas|power|gold|production)|spud|"
                r"discovery|\bdrill|well result|construction (start|begins|commenced)|start of construction|groundbreaking|grid connection|energi[sz]ed|permit|consent|"
                r"licen[cs]e (award|application|granted)|awarded .*licen|production start|start(s|ed)? production|ready[- ]to[- ]build|\brtb\b|tender|auction|"
                r"contract (award|win)|awarded .*contract|wins? .*contract|\border(s)? (for|from|intake|of|totall?ing)|receives? .*order|secures? .*order|turbine (order|supply)|\bepc\b|"
                r"\bmw\b|\bgw\b|\bmwp\b|land (lease|agreement|development)|project (milestone|update)|pipeline update|development plan|\bpdo\b|plan for development|well test|flow test|appraisal"),
]
RULE_RE = [(k, lab, re.compile(p, re.I)) for k, lab, p in RULES]
TYPE_LABEL = {k: lab for k, lab, _ in RULES}
TYPE_LABEL["other"] = "Other"

TECH = [("solar", r"\bsolar\b|photovoltaic|\bpv\b|\bmwp\b|solcell|solpark|solenergi"),
        ("wind", r"\bwind\b|offshore wind|onshore wind|wind ?farm|wind ?park|turbine|vindkraft|vindpark|vind\b"),
        ("battery", r"battery|\bbess\b|energy storage|storage (system|project|asset)|batteri"),
        ("datacentre", r"data ?cent(er|re)s?|datacent|hyperscal")]
TECH_RE = [(k, re.compile(p, re.I)) for k, p in TECH]


YAHOO_JUNK = re.compile(r"\?|\bstocks\b|\bshares (climb|jump|fall|drop|surge|slide|rise|sink)|why .* (shares|stock)|undervalued|overvalued|"
                        r"\bbuy\b|dividend story|ranked|top picks?|to watch|bull case|bear case|price target|analyst|upgrade|downgrade|sector update|market chatter: .*(could|may)", re.I)


def fold(t):
    return (unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode() + " " + (t or "")).lower()


def routine_of(title, hints=""):
    t = fold(title)
    for k, lab, rx in ROUTINE_RE:
        if rx.search(t):
            # a headline that is clearly a deal is never routine (e.g. "Result of AGM and placing")
            if k != "buyback" and re.search(r"acqui|placing|private placement|rights issue|open offer|merger|offer for|sale of|spin-?(out|off)|demerger|"
                                             r"arrangement|takeover|divest|disposal", t):
                return None
            return k
    h = (hints or "").lower()
    if re.search(r"changes in company's own shares|major shareholder announcements|total number of voting rights|annual general meeting|"
                 r"flaggepliktige|egne aksjer|share buy back", h):
        return "buyback" if "own shares" in h or "egne aksjer" in h or "buy back" in h else "holdings" if "shareholder" in h or "voting" in h or "flagg" in h else "meeting"
    return None


def classify(title, hints=""):
    t = fold(title)
    for k, lab, rx in RULE_RE:
        if rx.search(t):
            return k
    h = (hints or "").lower()
    if re.search(r"insider|managers'? transactions|pdmr|meldepliktig", h): return "insider"
    if re.search(r"interim|financial report|half year|annual financial|quarterly|sub:report", h): return "results"
    return "other"


def tech_of(text):
    t = fold(text)
    return [k for k, rx in TECH_RE if rx.search(t)]


# ---------------------------------------------------------------- €/MW extraction
CUR = r"(?P<c>EUR|€|NOK|SEK|DKK|USD|US\$|\$|GBP|£|CAD|C\$)"
NUM = r"(?P<a>\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)"
MUL = r"(?P<m>billion|bn|million|mill\.?|mln|mn|m|mrd|milliarder|millioner)\b"
MONEY = [re.compile(CUR + r"\s?" + NUM + r"\s?" + MUL, re.I),
         re.compile(NUM + r"\s?" + MUL + r"\s?" + CUR, re.I),
         re.compile(r"(?P<m2>M|B)(?P<c>NOK|SEK|EUR|USD|DKK)\s?" + NUM, re.I),
         re.compile(r"(?P<c>NOK|SEK|EUR|USD|DKK)\s?" + NUM + r"\s?(?P<m>mill|mrd)\b", re.I)]
CAP = re.compile(r"(?P<v>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?(?P<u>MW|GW)(?P<s>p|ac|dc|e)?\b(?!h)", re.I)
CCY = {"€": "EUR", "$": "USD", "US$": "USD", "£": "GBP", "C$": "CAD"}


def money_in(s):
    out = []
    for rx in MONEY:
        for m in rx.finditer(s):
            d = m.groupdict(); c = CCY.get(d["c"].upper() if d["c"] not in ("€", "$", "£") else d["c"], d["c"].upper())
            c = CCY.get(c, c)
            a = d["a"].replace(" ", "")
            a = float(a.replace(",", "")) if re.search(r",\d{3}(\D|$)", a) else float(a.replace(",", "."))
            mu = (d.get("m") or d.get("m2") or "").lower().rstrip(".")
            f = 1e9 if mu in ("billion", "bn", "mrd", "milliarder", "b") else 1e6 if mu else 1
            out.append(dict(ccy=c, amt=a * f, txt=m.group(0).strip(), pos=m.start()))
    # de-duplicate overlapping matches
    out.sort(key=lambda x: x["pos"]); res = []
    for x in out:
        if not res or x["pos"] >= res[-1]["pos"] + len(res[-1]["txt"]):
            res.append(x)
    return res


def per_mw(text, fx, kind):
    """Value per MW where one sentence states exactly one money amount and one capacity. Returns the inputs."""
    if not text:
        return None
    for s in re.split(r"(?<=[.!?])\s+(?=[A-ZÆØÅÄÖ\"(])|\n", text[:6000]):
        if len(s) > 600:
            continue
        caps = {(float(m.group("v").replace(",", "")) * (1000 if m.group("u").upper() == "GW" else 1)) for m in CAP.finditer(s)}
        if len(caps) != 1:
            continue
        mon = [m for m in money_in(s) if m["amt"] >= 5e5]
        if len({(m["ccy"], m["amt"]) for m in mon}) != 1:
            continue
        mw = caps.pop(); m = mon[0]
        r = (fx.get(m["ccy"]) or {}).get("rate")
        if not r or mw <= 0:
            continue
        eur = m["amt"] / r; v = eur / mw
        if not (5e3 <= v <= 2e7):   # €0.005m–€20m per MW, else probably not a price for that capacity
            continue
        return dict(kind=kind, ccy=m["ccy"], amt=round(m["amt"]), amt_txt=m["txt"], mw=mw, fx=round(r, 4),
                    eur=round(eur), eur_mw=round(v), quote=s.strip()[:260])
    return None


BOILER = re.compile(r"not for (release|publication|distribution)|publication or distribution|inside information|for immediate release|legal advis|"
                    r"financial advis|reference is (further )?made|this announcement|neither .* nor|owes or accepts|is a socially|^about |"
                    r"listed on the (main market|london stock)|\blei\b|artificial intelligence|ai-generated|accuracy and clarity|generated or assisted|persons who are not resident|inform themselves|restricted|forward-looking|^\W*(about|contact|for further)|"
                    r"stock exchange release|press release|regulatory|\bepic\b|\bindex: ?aim|^\*|investor relations", re.I)
DATELINE = re.compile(r"^[^.:]{0,80}?\b(\d{1,2}(st|nd|rd|th)? \w+ 20\d\d|\w+ \d{1,2}, 20\d\d|\d{1,2}\.\d{1,2}\.20\d\d)\b[^:–—-]{0,40}[:–—-]\s*", re.I)


def lead_sentence(text, title):
    """First informative sentence of a release: skips headers, disclaimers, datelines and all-caps lines."""
    for line in (text or "").split("\n"):
        line = line.split(" | ")[-1].strip()
        for snt in re.split(r"(?<=[.!?])\s+(?=[A-Z\"(])", line):
            snt = DATELINE.sub("", snt.strip())
            if len(snt) < 60 or BOILER.search(snt) or snt.upper() == snt or norm(snt)[:50] == norm(title)[:50] or not snt[0].isupper() or not re.search(r"[.!?)\"\u201d]$", snt):
                continue
            return re.sub(r"\s+", " ", snt)
    return ""


# ---------------------------------------------------------------- sources
def since(days):
    return dt.datetime.now(UTC) - dt.timedelta(days=days)


def src_newsweb(sym, issuer, cut):
    r = get("https://api3.oslo.oslobors.no/v1/newsreader/list", method="POST",
            params=dict(category="", issuer=issuer, fromDate=cut.strftime("%Y-%m-%d"), toDate="", market="", messageTitle=""))
    out = []
    for m in r.json()["data"]["messages"]:
        cat = " ".join(c.get("category_en", "") for c in m.get("category") or [])
        out.append(dict(t=dt.datetime.fromisoformat(m["publishedTime"].replace("Z", "+00:00")), h=m["title"].strip(),
                        u=f"https://newsweb.oslobors.no/message/{m['messageId']}", src="newsweb", hint=cat, mid=m["messageId"]))
    return out


def body_newsweb(it):
    r = get("https://api3.oslo.oslobors.no/v1/newsreader/message", params=dict(messageId=it["mid"]))
    b = (r.json()["data"]["message"].get("body") or "").replace("\r", "")
    return re.sub(r"(?<!\n)\n(?!\n)", " ", b)  # NewsWeb hard-wraps lines at ~80 characters


def src_mfn(sym, slug, cut):
    out, url = [], f"https://mfn.se/all/a/{slug}.json?limit=60"
    for _ in range(3):
        d = get(url).json()
        stop = False
        for x in d.get("items", []):
            c = x.get("content", {}); p = x.get("properties", {})
            t = dt.datetime.fromisoformat(c["publish_date"].replace("Z", "+00:00"))
            if t < cut:
                stop = True; break
            body = text_of(c.get("html", ""))
            out.append(dict(t=t, h=(c.get("title") or "").strip(), u=x.get("url"), src="mfn", lang=p.get("lang"),
                            grp=x.get("group_id"), hint=" ".join(p.get("tags") or []), body=body, pre=(c.get("preamble") or "").strip()))
        url = d.get("next_url")
        if stop or not url:
            break
    # one language per release: prefer English
    by = {}
    for it in out:
        k = it.get("grp") or it["u"]
        if k not in by or (it.get("lang") == "en" and by[k].get("lang") != "en"):
            by[k] = it
    return list(by.values())


def src_nasdaq(sym, company, cut):
    r = get("https://api.news.eu.nasdaq.com/news/query.action", params=dict(
        type="json", showAttachments="false", showCnsSpecific="true", showCompany="true", countResults="false",
        freeText="", company=company, market="", cnscategory="", globalGroup="exchangeNotice",
        globalName="NordicAllMarkets", displayLanguage="en", language="en", timeZone="UTC",
        dateMask="yyyy-MM-dd HH:mm:ss", limit="60", start="0", dir="DESC"))
    out = []
    for m in r.json()["results"]["item"]:
        t = dt.datetime.strptime(m["releaseTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        if t >= cut:
            out.append(dict(t=t, h=m["headline"].strip(), u=m["messageUrl"], src="nasdaq", hint=m.get("cnsCategory") or ""))
    return out


def body_nasdaq(it):
    t = get(it["u"]).text
    m = re.search(r'(?is)<div[^>]+id="(?:previewContent|content)"[^>]*>(.*)', t)
    return text_of(m.group(1) if m else t)[:8000]


LON_TZ = __import__("zoneinfo").ZoneInfo("Europe/London")
STO_TZ = __import__("zoneinfo").ZoneInfo("Europe/Stockholm")


def src_investegate(sym, tidm, cut):
    out = []
    for page in (1, 2, 3):
        t = get(f"https://www.investegate.co.uk/company/{tidm}" + (f"?page={page}" if page > 1 else "")).text
        rows = re.findall(r'<td>(\d\d \w\w\w \d{4})</td>\s*<td>([^<]+)</td>\s*<td>.*?title="supplier: ([^"]*)".*?</td>\s*<td>\s*'
                          r'<a class="announcement-link" href="([^"]+)">([^<]+)</a>', t, re.S)
        if not rows:
            break
        oldest = None
        for d, tm, sup, u, h in rows:
            ts = dt.datetime.strptime(f"{d} {tm.strip()}", "%d %b %Y %I:%M %p").replace(tzinfo=LON_TZ).astimezone(UTC)
            oldest = ts
            if ts >= cut:
                out.append(dict(t=ts, h=html.unescape(h).strip(), u=u, src="investegate", hint=sup))
        if oldest is None or oldest < cut:
            break
    return out


def body_investegate(it):
    t = get(it["u"]).text
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", t)
    if m:
        it["h"] = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))).strip()
    b = re.search(r'(?is)<div[^>]+class="[^"]*news-window[^"]*"[^>]*>(.*)', t)  # the release itself (not the site's AI summary)
    return text_of(b.group(1))[:9000] if b else ""


def src_yahoo(sym, q, cut):
    d = get("https://query2.finance.yahoo.com/v1/finance/search", params=dict(q=q, newsCount=20, quotesCount=0)).json()
    out = []
    for n in d.get("news", []):
        if sym not in (n.get("relatedTickers") or []) or fold(YAHOO_NAME.get(sym, q.split()[0])) .split()[0] not in fold(n.get("title", "")):
            continue  # must be tagged with the ticker and name the company in the headline
        t = dt.datetime.fromtimestamp(n.get("providerPublishTime", 0), UTC)
        if t >= cut:
            out.append(dict(t=t, h=n["title"].strip(), u=n.get("link"), src="yahoo", pub=n.get("publisher") or ""))
    return out


# ---------------------------------------------------------------- FI insider register
FI_KIND = [(r"^förvärv", "buy"), (r"^avyttring", "sell"), (r"^teckning", "subscription"), (r"^tilldelning", "grant"),
           (r"^lösen ökning", "exercise"), (r"^lösen minskning", "exercise"), (r"^utdelning", "dividend"), (r"^gåva mottagen", "gift in"),
           (r"^gåva lämnad", "gift out"), (r"^pantsättning", "pledge"), (r"^byte", "exchange"), (r"^interntransaktion", "internal"),
           (r"^arv", "inheritance"), (r"^lån", "loan")]
FI_ROLE = [(r"vice verkställande|vice vd", "Deputy CEO"), (r"verkställande direktör|\bvd\b", "CEO"), (r"ordförande", "Chair"),
           (r"styrelseledamot|styrelse", "Board member"), (r"ekonomi|finans|cfo", "CFO"), (r"revisor", "Auditor"),
           (r"annan medlem|ledande befattningshavare|annan ledande", "Senior executive")]
FI_INSTR = {"aktie": "Share", "option": "Option", "teckningsoption": "Warrant", "teckningsrätt": "Subscription right",
            "obligation": "Bond", "betald tecknad aktie": "Paid subscribed share", "aktierätt": "Share right"}


def fnum(s):
    s = (s or "").replace(" ", "").replace("\xa0", "")
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def src_fi(sym, issuer, cut):
    r = get("https://marknadssok.fi.se/publiceringsklient/sv-SE/Search/Search",
            params={"SearchFunctionType": "Insyn", "Utgivare": issuer, "Publiceringsdatum.From": cut.strftime("%Y-%m-%d"), "button": "export"})
    raw = r.content
    txt = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw[:40] else raw.decode("utf-8", "replace")
    rows = [{k: re.sub(r"[\xa0\s]+", " ", v or "").strip() for k, v in x.items() if k} for x in csv.DictReader(io.StringIO(txt), delimiter=";")]
    agg = {}
    for x in rows:
        if (x.get("Status") or "").lower().startswith("makul"):
            continue
        k0 = (x.get("Karaktär") or "").strip().lower()
        kind = next((v for p, v in FI_KIND if re.search(p, k0)), k0 or "other")
        role0 = (x.get("Befattning") or "").lower()
        role = next((v for p, v in FI_ROLE if re.search(p, role0)), (x.get("Befattning") or "").strip()[:40])
        it0 = (x.get("Instrumenttyp") or "").strip().lower()
        instr = FI_INSTR.get(it0, (x.get("Instrumenttyp") or "").strip()[:30])
        vol, px = fnum(x.get("Volym")), fnum(x.get("Pris"))
        d = (x.get("Transaktionsdatum") or "")[:10]
        person = (x.get("Person i ledande ställning") or "").strip()
        rel = (x.get("Närstående") or "").strip().lower() == "ja"
        key = (d, person, kind, instr, x.get("Valuta"), rel)
        a = agg.setdefault(key, dict(d=d, pub=(x.get("Publiceringsdatum") or "")[:16], p=person, r=role, rel=rel, k=kind, i=instr,
                                     v=0.0, val=0.0, c=(x.get("Valuta") or "").strip(), n=0))
        a["n"] += 1
        if vol:
            a["v"] += vol
            if px:
                a["val"] += vol * px
    out = []
    for a in agg.values():
        a["px"] = round(a["val"] / a["v"], 4) if a["v"] and a["val"] else None
        a["v"] = round(a["v"]); a["val"] = round(a["val"]) if a["val"] else None
        out.append({k: v for k, v in a.items() if v not in (None, "", False)})
    out.sort(key=lambda a: (a["d"], a.get("pub", "")), reverse=True)
    return out


# ---------------------------------------------------------------- main
def norm(t):
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()[:80]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "dealflow.json"))
    ap.add_argument("--only", help="comma-separated symbols (debug)")
    ap.add_argument("--debug", action="store_true", help="log collapsed routine headlines")
    a = ap.parse_args()
    t0 = time.time()
    now = dt.datetime.now(UTC); cut = since(DAYS); fcut = since(FI_DAYS)
    cos = companies()
    if a.only:
        keep = set(a.only.split(","))
        cos = {k: v for k, v in cos.items() if k in keep}
    try:
        fx = json.load(open(os.path.join(ROOT, "data", "market.json"))).get("fx_eur") or {}
    except Exception:
        fx = {}
    prev = {}
    try:
        old = json.load(open(a.out))
        prev = {x["u"]: x for x in old.get("items", [])}
    except Exception:
        old = {}

    jobs = []
    for s in cos:
        src = source_of(s)
        if src == "newsweb": jobs.append((s, "newsweb", lambda s=s: src_newsweb(s, NEWSWEB[s], cut)))
        elif src == "mfn": jobs.append((s, "mfn", lambda s=s: src_mfn(s, MFN[s], cut)))
        elif src == "nasdaq": jobs.append((s, "nasdaq", lambda s=s: src_nasdaq(s, NASDAQ[s], cut)))
        elif src == "investegate": jobs.append((s, "investegate", lambda s=s: src_investegate(s, s[:-2] if s != "BP.L" else "BP.", cut)))
        elif src == "yahoo": jobs.append((s, "yahoo", lambda s=s: src_yahoo(s, YAHOO_Q[s], cut)))
        if s in FI: jobs.append((s, "fi", lambda s=s: src_fi(s, FI[s], fcut)))

    def run(j):
        s, kind, fn = j
        t1 = time.time()
        try:
            return s, kind, fn(), None, time.time() - t1
        except Exception as e:
            return s, kind, [], str(e)[:160], time.time() - t1

    with ThreadPoolExecutor(WORKERS) as ex:
        res = list(ex.map(run, jobs))
    stat, raw, fi = {}, [], {}
    for s, kind, got, err, sec in res:
        st = stat.setdefault(kind, dict(ok=0, fail=0, items=0, errors=[]))
        if err:
            st["fail"] += 1; st["errors"].append(f"{s}: {err}")
            # keep the previous items of a failed source rather than dropping the company
            if kind == "fi":
                if s in (old.get("fi") or {}): fi[s] = old["fi"][s]
            else:
                raw += [dict(x, keep_prev=True) for x in prev.values() if x["s"] == s and dt.datetime.fromisoformat(x["t"].replace("Z", "+00:00")) >= cut]
            continue
        st["ok"] += 1; st["items"] += len(got)
        if kind == "fi":
            if got: fi[s] = got
        else:
            raw += [dict(x, s=s) for x in got]

    # classify; queue release-text fetches for deal-type items that need them (once per URL)
    routine, items, need = {}, [], []
    for x in raw:
        if x.get("keep_prev"):
            items.append({k: v for k, v in x.items() if k != "keep_prev"}); continue
        if x["src"] == "investegate" and x["u"] in prev:
            x["h"] = prev[x["u"]]["h"]  # full headline fetched earlier
        rk = routine_of(x["h"], x.get("hint", ""))
        if rk:
            if x.get("lang") in (None, "en"):  # MFN bilingual twins count once
                routine.setdefault(x["s"], {}).setdefault(rk, 0); routine[x["s"]][rk] += 1
            if a.debug: log(f"  routine/{rk}: {x['s']} | {x['h'][:100]}")
            continue
        ty = classify(x["h"], x.get("hint", ""))
        if x["src"] == "yahoo" and (ty in ("other", "insider") or YAHOO_JUNK.search(x["h"]) or
                                    (ty == "results" and not re.search(r"results|earnings|quarter|guidance|production", x["h"], re.I))):
            continue  # media headlines: only typed, material items; no listicles, questions or commentary
        x["ty"] = ty
        items.append(x)
        p = prev.get(x["u"])
        if p and p.get("ty") == ty:
            for k in ("m", "f", "x", "h"):
                if k in p: x[k] = p[k]
            x["done"] = True
        elif ty in ("ma", "ppa", "raise", "financing", "project") or (x["src"] == "investegate" and x["h"].endswith("...")):
            if x["src"] in ("newsweb", "nasdaq", "investegate"):
                need.append(x)

    def fetch_body(x):
        try:
            x["body"] = {"newsweb": body_newsweb, "nasdaq": body_nasdaq, "investegate": body_investegate}[x["src"]](x)
            return 1
        except Exception:
            return 0

    nb = 0
    if need:
        need.sort(key=lambda x: x["t"], reverse=True)
        need = need[:90]
        with ThreadPoolExecutor(WORKERS) as ex:
            nb = sum(ex.map(fetch_body, need))
    # re-classify Investegate items whose truncated headline was replaced by the full one
    out_items = []
    for x in items:
        if x["src"] == "investegate" and not x.get("done"):
            rk = routine_of(x["h"], x.get("hint", ""))
            if rk:
                routine.setdefault(x["s"], {}).setdefault(rk, 0); routine[x["s"]][rk] += 1
                continue
            x["ty"] = classify(x["h"], x.get("hint", ""))
        if not x.get("done"):
            body = x.get("body") or ""
            lead = ((x.get("pre") or "") or body)[:300]
            f = tech_of(x["h"] + " " + lead)
            if f: x["f"] = f
            if x["ty"] in ("ma", "project"):
                m = per_mw(x["h"] + ".\n" + body, fx, "deal" if x["ty"] == "ma" else "investment")
                if m: x["m"] = m
            if x["ty"] in ("ma", "ppa", "raise", "financing", "project") and (x.get("pre") or body):
                ex_ = lead_sentence(x.get("pre") or "", x["h"]) or lead_sentence(body, x["h"])
                if ex_:
                    x["x"] = ex_[:220].rstrip() + ("…" if len(ex_) > 220 else "")
        if isinstance(x["t"], dt.datetime):
            x["t"] = iso(x["t"])
        out_items.append(x)

    # FI share buys / sells as feed items (one per person, day and side)
    for s, rows in fi.items():
        for r in rows:
            if r.get("k") not in ("buy", "sell") or r.get("i") != "Share" or not r.get("d"):
                continue
            t = dt.datetime.fromisoformat(r["d"] + "T12:00:00+00:00")
            if t < cut:
                continue
            who = r.get("p") or "Insider"
            verb = "bought" if r["k"] == "buy" else "sold"
            val = f" (≈{r['c']} {r['val'] / 1e6:.2f}m)" if r.get("val") and r.get("c") else ""
            h = f"{who}{' (' + r['r'] + ')' if r.get('r') else ''}{', via a closely associated person,' if r.get('rel') else ''} {verb} {r['v']:,} shares" + \
                (f" at {r['c']} {r['px']:.2f}" if r.get("px") else "") + val
            out_items.append(dict(t=iso(dt.datetime.fromisoformat((r.get("pub") or r["d"] + " 12:00")).replace(tzinfo=STO_TZ)),
                                  s=s, h=h, u="https://marknadssok.fi.se/publiceringsklient/sv-SE/Search/Search?SearchFunctionType=Insyn&Utgivare=" +
                                  re.sub(r"\s", "+", FI[s]), src="fi", ty="insider", k=r["k"]))

    # de-duplicate per company: same normalised headline; the non-English twin of a release published within
    # 20 minutes of an English one; near-identical headlines (word overlap >= 60%) within 2 days (media duplicates)
    out_items.sort(key=lambda x: x["t"], reverse=True)
    def tsec(x): return dt.datetime.fromisoformat(x["t"].replace("Z", "+00:00")).timestamp()
    def english(x): return x.get("lang") == "en" or (x.get("lang") is None and bool(re.search(r"\b(the|of|and|to|for|in|on|with|by|from|announces?|update)\b", x["h"], re.I)))
    bys = {}
    for x in out_items: bys.setdefault(x["s"], []).append(x)
    drop = set()
    for s_, xs in bys.items():
        for i, x in enumerate(xs):
            if english(x):
                continue
            if any(english(y) and abs(tsec(x) - tsec(y)) <= 1200 for y in xs if y is not x):
                drop.add(id(x)); continue
            if x.get("lang") and x["lang"] != "en" and any(y.get("lang") == "en" for y in xs):
                drop.add(id(x))
    seen, per, final, bags = set(), {}, [], {}
    for x in out_items:
        if id(x) in drop:
            continue
        k = (x["s"], norm(x["h"]))
        if k in seen:
            continue
        bag = set(norm(x["h"]).split())
        if any(abs(tsec(x) - t2) <= 2 * 86400 and ty2 == x["ty"] and len(bag & b2) / max(1, len(bag | b2)) >= 0.45 for b2, t2, ty2 in bags.get(x["s"], [])):
            continue
        bags.setdefault(x["s"], []).append((bag, tsec(x), x["ty"]))
        seen.add(k)
        per[x["s"]] = per.get(x["s"], 0) + 1
        if per[x["s"]] > PER_CO:
            continue
        rec = dict(t=x["t"], s=x["s"], ty=x["ty"], h=x["h"], u=x["u"], src=x["src"])
        for k2 in ("f", "m", "x", "pub", "k"):
            if x.get(k2):
                rec[k2] = x[k2]
        final.append(rec)

    srcs = {"newsweb": "Oslo Børs NewsWeb", "mfn": "MFN", "nasdaq": "Nasdaq Nordic (GlobeNewswire)", "investegate": "Investegate (RNS and other UK regulatory feeds)",
            "yahoo": "Yahoo Finance news", "fi": "Finansinspektionen insider register"}
    out = dict(generated_utc=now.isoformat(timespec="seconds"), window_days=DAYS, fi_days=FI_DAYS,
               groups=GROUP_LABEL, types=TYPE_LABEL, routine_labels={k: lab for k, lab, _ in ROUTINE},
               cos={s: dict(n=c["n"], g=c["g"], src=source_of(s), **({"fi": 1} if s in FI else {})) for s, c in cos.items()},
               items=final, routine=routine, fi=fi,
               sources=[dict(id=k, label=srcs[k], **{kk: vv for kk, vv in v.items() if kk != "errors"}, errors=v["errors"][:5]) for k, v in stat.items()],
               seconds=round(time.time() - t0, 1), bodies=nb)
    if not any(v["ok"] for v in stat.values()):
        log("no source worked - not overwriting"); return 2
    js = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out + ".tmp", "w") as f:
        f.write(js)
    os.replace(a.out + ".tmp", a.out)
    by = {}
    for x in final: by[x["ty"]] = by.get(x["ty"], 0) + 1
    log(f"wrote {a.out}: {len(final)} items {by}, routine collapsed {sum(sum(v.values()) for v in routine.values())}, "
        f"fi {sum(len(v) for v in fi.values())} rows/{len(fi)} cos, {len(js) // 1024} KB, bodies fetched {nb}, "
        f"sources {{{', '.join(f'{k}: {v['ok']} ok/{v['fail']} fail' for k, v in stat.items())}}}, {out['seconds']}s")
    for k, v in stat.items():
        for e in v["errors"][:5]:
            log(f"  {k} error {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

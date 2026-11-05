#!/usr/bin/env python3
"""Build data/griddc.json (Power tab, 'Grid & DC'): grid-connection registers, tender results and data-centre news.

Once a day in the full morning build. Plain facts with source links only. Keyless public sources:
  UK  NESO data portal (CKAN): TEC register, embedded register, interconnector register (CSV);
      NESO news page + RSS (connections items only)
  DE  BNetzA 'Beendete Ausschreibungen' (ground-mounted solar, first segment) results table;
      TSO press: Amprion RSS, TransnetBW press JSON, TenneT DE news page (connection / capacity notices only)
  FR  ODRE 'Projets en développement' (RTE + distribution connection queue by region);
      CRE tender pages + public synthesis reports (PDF) for PPE2 solar 'Sol' and 'Bâtiment'
  DC  Google News RSS (headline search per country), DCD RSS, and the site's deal-flow feed (data/dealflow.json)
Change detection for the TEC/embedded registers keeps one compact fingerprint per entry in data/griddc_state.json
(entry id + 4 hash characters, no project data), so new and changed entries can be flagged against the previous build.
Lists keep a 90-day window. Each section falls back to the previous file when its source fails.
"""
import csv, datetime as dt, hashlib, html, io, json, os, re, sys, time, urllib.parse
import xml.etree.ElementTree as ET

try:
    from curl_cffi import requests as http
    IMP = dict(impersonate="chrome")
except Exception:  # pragma: no cover
    import requests as http
    IMP = {}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "griddc.json")
STATE = os.path.join(ROOT, "data", "griddc_state.json")
UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
TODAY = NOW.date()
WIN = 90
CUT = TODAY - dt.timedelta(days=WIN)
S = http.Session(**IMP)
if not IMP:
    S.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) Chrome/126 Safari/537.36"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, tries=2, **kw):
    err = None
    for i in range(tries):
        try:
            r = S.get(url, timeout=45, **kw)
            if r.status_code == 200:
                return r
            err = IOError(f"HTTP {r.status_code}")
            if r.status_code in (401, 403, 404):
                break
        except Exception as e:  # noqa: BLE001
            err = e
        time.sleep(2 * (i + 1))
    raise err


def txt(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).replace("\xad", "").strip()


def strip_tags(s):
    return re.sub(r"<script.*?</script>|<style.*?</style>", "", s, flags=re.S)


def fnum(s):
    s = (s or "").strip().replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    if not s:
        return None
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", s):   # 2.294.768 / 1.234,5
        s = s.replace(".", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def in_win(d):
    return d and d >= CUT.isoformat()


# ---------------------------------------------------------------- UK: NESO registers
NESO = "https://api.neso.energy/api/3/action/package_show?id="
REG = {"T": "transmission-entry-capacity-tec-register", "E": "embedded-register"}
B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def b64n(n, w):
    s = ""
    for _ in range(w):
        s = B64[n % 64] + s; n //= 64
    return s


def h1(s):
    return B64[hashlib.md5(s.encode()).digest()[0] & 63]


def cat_of(pt):
    p = pt.lower()
    s, b, d = "pv array" in p or "solar" in p, "storage" in p, "demand" in p
    if not (s or b or d):
        return None
    c = "SB" if s and b else "S" if s else "B" if b else "D"
    return c, d


def isodate(s):
    s = (s or "").strip()
    for f in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s[:10], f).date().isoformat()
        except ValueError:
            pass
    return None


def neso_pkg(name):
    p = get(NESO + name).json()["result"]
    r = p["resources"][0]
    return r["url"], (r.get("last_modified") or p.get("metadata_modified") or "")[:10]


def neso_csv(url):
    return list(csv.DictReader(io.StringIO(get(url).content.decode("utf-8-sig", "replace"))))


def build_uk(prev_state):
    regs, rows, fp = {}, [], {}
    for rk, name in REG.items():
        url, mod = neso_pkg(name)
        file_date = None
        m = re.search(r"(\d{1,2})-([a-z]+)-(20\d\d)\.csv", url)
        if m:
            try:
                file_date = dt.datetime.strptime(" ".join(m.groups()), "%d %B %Y").date().isoformat()
            except ValueError:
                pass
        data = neso_csv(url)
        regs[rk] = dict(name=name, url=url, mod=mod, date=file_date or mod, n=len(data),
                        page="https://www.neso.energy/data-portal/" + name)
        for x in data:
            c = cat_of(x.get("Plant Type", ""))
            if not c:
                continue
            pn = re.sub(r"\D", "", x.get("Project Number", "")) or "0"
            key = rk + b64n(int(pn), 3)
            mw = fnum(x.get("Cumulative Total Capacity (MW)"))
            inc = fnum(x.get("MW Increase / Decrease"))
            con = fnum(x.get("MW Connected"))
            r = dict(k=key, r=rk, n=x.get("Project Name", "").strip(), c=c[0], dm=c[1], mw=mw, inc=inc, con=con,
                     d=isodate(x.get("MW Effective From")), s=x.get("Project Status", "").strip(),
                     site=x.get("Connection Site", "").strip(), to=x.get("HOST TO", "").strip(), pn=x.get("Project Number", "").strip())
            parts = (f"{mw}|{inc}|{con}", r["d"] or "", r["s"], "|".join([r["n"], r["site"], x.get("Plant Type", ""), x.get("Stage", ""), x.get("Customer Name", "")]))
            h = "".join(h1(p) for p in parts[:3]) + h1("#".join(parts))
            fp[key] = h if key not in fp else h1(fp[key] + h) * 4     # rare duplicate project numbers: one combined print
            rows.append(r)
    # interconnectors: pipeline summary only
    ic = None
    try:
        url, mod = neso_pkg("interconnector-register")
        d = neso_csv(url)
        st = {}
        for x in d:
            s = x.get("Project Status", "").strip(); mw = fnum(x.get("MW Import - Total")) or 0
            a = st.setdefault(s, [0, 0]); a[0] += 1; a[1] += mw
        ic = dict(date=mod, url="https://www.neso.energy/data-portal/interconnector-register",
                  status={k: [v[0], round(v[1])] for k, v in st.items()})
    except Exception as e:  # noqa: BLE001
        log("interconnector register:", e)
    # summary by category x status
    summ = {}
    for r in rows:
        a = summ.setdefault(r["c"], {}).setdefault(r["s"], [0, 0.0])
        a[0] += 1; a[1] += r["mw"] or 0
    summ = {c: {s: [v[0], round(v[1])] for s, v in d.items()} for c, d in summ.items()}
    dm = [0, 0.0]
    for r in rows:
        if r["dm"]:
            dm[0] += 1; dm[1] += r["mw"] or 0
    # changes vs previous fingerprints
    packed = "".join(k + v for k, v in sorted(fp.items()))
    prev = {}
    pp = (prev_state or {}).get("tec") or ""
    for i in range(0, len(pp) - 7, 8):
        prev[pp[i:i + 4]] = pp[i + 4:i + 8]
    changes, removed = [], 0
    if prev:
        by = {r["k"]: r for r in rows}
        for k, h in fp.items():
            o = prev.get(k)
            if o == h:
                continue
            r = by[k]
            f = [] if o is None else [n for n, a, b in zip(("MW", "date", "status"), o[:3], h[:3]) if a != b] or ["details"]
            changes.append(dict(t="new" if o is None else "chg", f=f, **{q: r[q] for q in ("r", "n", "c", "mw", "d", "s", "site", "pn")}))
        removed = sum(1 for k in prev if k not in fp)
    # next to connect: not built, effective date within the next 12 months
    lim = (TODAY + dt.timedelta(days=365)).isoformat()
    nxt = sorted((r for r in rows if r["s"] != "Built" and r["d"] and TODAY.isoformat() <= r["d"] <= lim),
                 key=lambda r: (r["d"], -(r["mw"] or 0)))
    nxt = [{q: r[q] for q in ("r", "n", "c", "mw", "d", "s", "site")} for r in nxt[:30]]
    return dict(regs=regs, summary=summ, demand=[dm[0], round(dm[1])], ic=ic, next=nxt, n=len(rows)), packed, changes, removed


# ---------------------------------------------------------------- UK: NESO connections publications
CONN_RX = re.compile(r"connection|gate 2|gate two|\bqueue\b|tmo4|cmp ?43[45]|g2twq|tec register|reform", re.I)


def neso_news():
    out = []
    s = strip_tags(get("https://www.neso.energy/news").text)
    seen = set()
    for m in re.finditer(r'<a[^>]+href="(/news/[^"#?]+)"[^>]*>(.*?)</a>', s, flags=re.S):
        t = txt(m.group(2))
        mm = re.match(r"(.*?)\s(\d{1,2} [A-Z][a-z]{2,8} 20\d\d) - \d+ minute read", t)
        if not mm or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        head = mm.group(1)
        cat = "Connections" if head.startswith("Connections ") else None
        title = head[len("Connections "):] if cat else re.sub(r"^(Energy Explained|Strategic planning|Resilience & Emergency Management|[A-Z][\w&]+(?: [\w&]+){0,3}) (?=[A-Z])", "", head, count=1)
        try:
            d = dt.datetime.strptime(mm.group(2).replace("Sept", "Sep"), "%d %b %Y").date().isoformat()
        except ValueError:
            continue
        if cat or CONN_RX.search(head):
            out.append(dict(d=d, h=title.strip(), u="https://www.neso.energy" + m.group(1), src="NESO news"))
    try:
        root = ET.fromstring(get("https://www.neso.energy/rss.xml").content)
        for i in root.findall(".//item"):
            t = (i.findtext("title") or "").strip()
            if t.startswith("FOI-") or not CONN_RX.search(t):
                continue
            d = dt.datetime.strptime(i.findtext("pubDate")[5:16], "%d %b %Y").date().isoformat()
            out.append(dict(d=d, h=t, u=i.findtext("link"), src="NESO publications"))
    except Exception as e:  # noqa: BLE001
        log("neso rss:", e)
    return out


# ---------------------------------------------------------------- DE: BNetzA solar tenders
BNETZA = "https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/Ausschreibungen/Solaranlagen1/BeendeteAusschreibungen/start.html"
MON_DE = {m: i + 1 for i, m in enumerate("Januar Februar März April Mai Juni Juli August September Oktober November Dezember".split())}


def bnetza():
    s = strip_tags(get(BNETZA).text)
    s = s[s.find("<main"):] if "<main" in s else s
    out = []
    for ym in re.finditer(r"Jahr (20\d\d)(?:/\d+)?\s*</[^>]+>(.*?)(?=Jahr 20\d\d|$)", s, flags=re.S):
        year = int(ym.group(1))
        tb = re.search(r"<table.*?</table>", ym.group(2), flags=re.S)
        if not tb:
            continue
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", tb.group(0), flags=re.S):
            cells = [txt(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.S)]
            if cells:
                rows.append(cells)
        lab = {r[0]: r[1:] for r in rows if r}
        dates = next((r[1:] for r in rows if r[0].startswith("Gebotstermin")), [])

        def row(rx):
            for k, v in lab.items():
                if re.search(rx, k):
                    return v
            return []
        vol, aw, avg = row(r"^Ausgeschriebene Menge"), row(r"^Zuschlagsmenge"), row(r"mengengewichteter Zuschlagswert")
        lo, hi, cap = row(r"Niedrigster Gebotswert \(mit"), row(r"Höchster Gebotswert \(mit"), row(r"Höchstwert")
        bids, nz = row(r"^Eingereichte Gebotsmenge"), row(r"^Zuschläge")
        for i, d in enumerate(dates):
            d = d.strip()
            mon = next((v for k, v in MON_DE.items() if d and k.startswith(d[:3])), None)
            if not mon or i >= len(avg) or fnum(avg[i]) is None:
                continue
            g = lambda a: fnum(a[i]) if i < len(a) else None  # noqa: E731
            kw = lambda a: (round(g(a) / 1e3) if g(a) >= 2e4 else round(g(a))) if g(a) else None  # noqa: E731  (kW; a few old cells are in MW)
            out.append(dict(r=f"{year}-{mon:02d}", vol=kw(vol), aw=kw(aw), bid=kw(bids), n=g(nz), avg=g(avg), lo=g(lo), hi=g(hi), cap=g(cap)))
    out.sort(key=lambda x: x["r"])
    return dict(rounds=out, url=BNETZA, seg="Solaranlagen des ersten Segments (ground-mounted)")


# ---------------------------------------------------------------- DE: TSO notices
TSO_RX = re.compile(r"netzanschluss|netzanschlüsse|anschlussverfahren|anschlusskapazit|anschlussbegehren|anschlussanfrage|anschlusszusage|"
                    r"reifegrad|rechenzent|batteriespeicher|großbatterie|netzkapazit|kapazitätsvergabe|überbauung|großverbraucher", re.I)


def tso_items():
    items, st = [], []
    try:
        root = ET.fromstring(get("https://www.amprion.net/Presse/RSS-Presse.xml").content)
        n = 0
        for i in root.findall(".//item"):
            items.append(("Amprion", (i.findtext("pubDate") or "")[:10], (i.findtext("title") or "").strip(), i.findtext("link"))); n += 1
        st.append(dict(id="amprion", ok=True, n=n))
    except Exception as e:  # noqa: BLE001
        st.append(dict(id="amprion", ok=False, err=str(e)[:80]))
    try:
        n = 0
        for x in get("https://www.transnetbw.de/de/api/press-releases").json():
            items.append(("TransnetBW", x.get("date", ""), txt(x.get("title")), x.get("url"))); n += 1
        st.append(dict(id="transnetbw", ok=True, n=n))
    except Exception as e:  # noqa: BLE001
        st.append(dict(id="transnetbw", ok=False, err=str(e)[:80]))
    try:
        s = strip_tags(get("https://www.tennet.eu/de/news").text); n = 0
        for m in re.finditer(r'<a[^>]+href="(/de/news/[^"]+)"[^>]*>(.*?)</a>', s, flags=re.S):
            t = txt(m.group(2)); d = re.match(r"(\d{1,2})\. (\w+) (20\d\d)\s*(?:\| Lesezeit \d+ Min\s*)?(.*)", t)
            if d and d.group(2) in MON_DE:
                title = re.sub(r"\s+(News|Finanznachrichten|Pressemitteilung)(\s.*)?$", "", d.group(4))
                items.append(("TenneT", f"{d.group(3)}-{MON_DE[d.group(2)]:02d}-{int(d.group(1)):02d}", title, "https://www.tennet.eu" + m.group(1))); n += 1
        st.append(dict(id="tennet", ok=n > 0, n=n))
    except Exception as e:  # noqa: BLE001
        st.append(dict(id="tennet", ok=False, err=str(e)[:80]))
    hits = [dict(src=a, d=d, h=t, u=u) for a, d, t, u in items if TSO_RX.search(t)]
    hits.sort(key=lambda x: x["d"], reverse=True)
    seen, out = set(), []
    for x in hits:
        k = re.sub(r"\W+", "", x["h"].lower())[:40]
        if k not in seen:
            seen.add(k); out.append(x)
    return out, st


# ---------------------------------------------------------------- FR: connection queue (ODRE)
ODRE = "https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/suivi-projet-raccordement-enr"


def odre():
    meta = get(ODRE).json()
    f = [x["name"] for x in meta["fields"]]
    col = next(x for x in f if x.startswith("projets_en_developpement"))
    m = re.search(r"(\d\d)_(\d\d)_(20\d\d)", col)
    asof = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None
    d = get(ODRE + "/exports/json").json()
    FIL = {"Solaire": "pv", "Eolien terrestre": "won", "Eolien en mer": "wof"}
    reg = {}
    for x in d:
        r = reg.setdefault(x["region"], {"pv": [0, 0], "won": [0, 0], "wof": [0, 0], "oth": [0, 0]})
        k = FIL.get(x["filiere"], "oth"); v = x.get(col) or 0
        r[k][0 if x["reseau"].startswith("Réseau public de transport") else 1] += v
    out = sorted(([k, {q: [round(a), round(b)] for q, (a, b) in v.items()}] for k, v in reg.items()), key=lambda r: -sum(sum(x) for x in r[1].values()))
    return dict(asof=asof, regions=out, url="https://odre.opendatasoft.com/explore/dataset/suivi-projet-raccordement-enr/")


# ---------------------------------------------------------------- FR: CRE solar tenders
CRE_AO = {"sol": ("PPE2 ground-mounted solar ('Centrales au sol')", "https://www.cre.fr/documents/appels-doffres/appel-d-offres-portant-sur-la-realisation-et-l-exploitation-d-installations-de-production-d-electricite-a-partir-de-l-energie-solaire-centrales-a2.html"),
          "bat": ("PPE2 rooftop solar > 500 kWc ('Bâtiments')", "https://www.cre.fr/documents/appels-doffres/appel-doffres-portant-sur-la-realisation-et-lexploitation-dinstallations-de-production-delectricite-a-partir-de-lenergie-solaire-centrales-sur-batiments-serres-agrivoltaiques-ombrieres-et-ombrieres-agrivoltaiques-de-puissance-superieure-a-500-kwc.html")}
MON_FR = {m: i + 1 for i, m in enumerate("janvier février mars avril mai juin juillet août septembre octobre novembre décembre".split())}


def cre_report(url):
    from pypdf import PdfReader
    b = get(url).content
    t = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(b)).pages[:7])
    flat = re.sub(r"\s+", " ", t)
    per = re.search(r"(\d+)\s*(?:e|è|ème|eme)\s*(?:période|P\b)", flat[:600])
    dd = re.search(r"(\d{1,2})(?:er)? (" + "|".join(MON_FR) + r") (20\d\d)", flat[:1500])
    tot = [ln for ln in t.split("\n") if ln.strip().startswith("Total")]
    price = mw = n = called = None
    for ln in tot:
        tok = [x for x in re.sub(r"\[SDA\]|%", " ", ln[5:]).split()]
        nums = []
        # re-join thousands written with a space ("1 935,67"): a 1-3 digit token followed by a 3-digit+decimal token
        i = 0
        while i < len(tok):
            if i + 1 < len(tok) and re.fullmatch(r"\d{1,3}", tok[i]) and re.fullmatch(r"\d{3}(,\d+)?", tok[i + 1]) and "," in tok[i + 1]:
                nums.append(tok[i] + tok[i + 1]); i += 2
            else:
                nums.append(tok[i]); i += 1
        if price is None and len(nums) >= 4 and all(re.fullmatch(r"\d+", x) for x in nums[:3]) and "," in nums[-1]:
            ints = [x for x in nums if re.fullmatch(r"\d+", x)]
            price, n = fnum(nums[-1]), int(ints[-1])
        elif price is not None and mw is None and len(tok) >= 3:
            # from the end: share of the volume sought (%), volume sought, MW the CRE proposes to retain
            r3 = tok[-3]
            if len(tok) >= 4 and re.fullmatch(r"\d{1,3}", tok[-4]) and re.fullmatch(r"\d{3},\d+", r3):
                r3 = tok[-4] + r3
            mw, called = fnum(r3), fnum(tok[-2])
    if price is None or mw is None:
        raise ValueError("totals not found")
    return dict(p=int(per.group(1)) if per else None, d=f"{dd.group(3)}-{MON_FR[dd.group(2)]:02d}-{int(dd.group(1)):02d}" if dd else None,
                mw=mw, called=called, n=n, avg=price, u=url)


def cre(cache):
    out, errs = {}, []
    for k, (label, page) in CRE_AO.items():
        s = get(page).text
        rep = [urllib.parse.urljoin(page, h) for h in re.findall(r'href="(/fileadmin/[^"]+\.pdf)"[^>]*>[^<]*rapport de synth', s, flags=re.I)]
        res = []
        for u in rep[:14]:
            if u not in cache:
                try:
                    cache[u] = cre_report(u)
                except Exception as e:  # noqa: BLE001  (remembered, so a report is downloaded once)
                    cache[u] = dict(err=str(e)[:60], u=u)
            if cache[u].get("err"):
                errs.append(f"{u.rsplit('/', 1)[-1]}: {cache[u]['err']}")
            else:
                res.append(cache[u])
        res.sort(key=lambda r: r.get("d") or "")
        out[k] = dict(label=label, url=page, periods=res)
    return out, errs


# ---------------------------------------------------------------- Data centres
PLACES = {
    "UK": r"\bUK\b|U\.K\.|Britain|British|England|English|Scotland|Scottish|Wales|Welsh|Northern Ireland|London|Slough|Manchester|Birmingham|Leeds|"
          r"Teesside|Teesworks|Northumberland|Blyth|Cambridge|Oxford|Didcot|Hertfordshire|Essex|Kent|Surrey|Lincolnshire|Yorkshire|Newport|Cardiff|"
          r"Edinburgh|Glasgow|Aberdeen|Thurrock|Docklands|Hayes|Park Royal|Bristol|Liverpool|Sheffield|Newcastle|Durham|Didcot|Hull|Humber|Cumbria",
    "DE": r"\bDeutschland\b|\bGermany\b|German|deutsch|Frankfurt|Berlin|Hessen|Hesse|Bayern|Bavaria|München|Munich|Hamburg|Nordrhein|NRW|Brandenburg|"
          r"Leipzig|Dresden|Sachsen|Saxony|Düsseldorf|Köln|Cologne|Stuttgart|Hanau|Offenbach|Rhein|Ruhr|Niedersachsen|Schleswig|Thüringen|"
          r"Mecklenburg|Hannover|Bremen|Essen|Dortmund|Mainz|Wiesbaden|Nürnberg|Lausitz|Rheinland|Baden",
    "FR": r"\bFrance\b|français|française|French|Paris|Île-de-France|Ile-de-France|Marseille|Lyon|Lille|Dunkerque|Dunkirk|Grenoble|Bordeaux|Toulouse|"
          r"Nantes|Strasbourg|Normandie|Normandy|Bretagne|Brittany|Hauts-de-France|Occitanie|Grand Est|Essonne|Seine|Yvelines|Val-d|Trappes|"
          r"Vélizy|Fos-sur-Mer|Saclay|Bourgogne|Nouvelle-Aquitaine|Auvergne|Provence|Moselle|Alsace|Cergy|Marne",
}
PLACE_RX = {c: re.compile(r"\b(?:" + r + r")\b", re.I if c != "UK" else 0) for c, r in PLACES.items()}
OTHER_RX = re.compile(r"\b(US|U\.S\.|USA|United States|America|Texas|Virginia|Ohio|Japan|India|China|Sweden|Finland|Norway|Denmark|Spain|Italy|"
                      r"Portugal|Ireland|Netherlands|Poland|Chile|Brazil|Saudi|UAE|Africa|Kazakhstan|Malaysia|Australia|Canada|Mexico|Indonesia|Korea|"
                      r"Schweden|Finnland|Norwegen|Spanien|Italien|Niederlande|Polen|États-Unis|Etats-Unis|Espagne|Italie|Suède|Finlande|Japon|Inde|"
                      r"Afrique|Maroc|Morocco|Dakota|Arizona|Georgia|Iowa|Nevada|Oregon|Singapore|Thailand|Vietnam|Philippines|Taiwan|Israel|Egypt|"
                      r"Nigeria|Kenya|Argentina|Colombia|Peru|Österreich|Oberösterreich|Austria|Schweiz|Switzerland|Belgium|Belgique|Luxembourg|Europe|Europa|"
                      r"Jordanie|Jordan|Égypte|Egypte|Toronto|Nairobi|Pune|Maharashtra|Queensland|Utah|Wyoming|Pennsylvania|Tasmania|Estonia|Oman|"
                      r"Pakistan|Islamabad|Indonésie|Pologne|Allemagne|Royaume-Uni|Großbritannien|Frankreich|"
                      # foreign places as written in French / German headlines (the native feeds tag by language, not by site)
                      r"Malaisie|Malaysien|Casablanca|Rabat|Tanger|Marrakech|Agadir|Dakhla|Sénégal|Dakar|Abidjan|Ivoire|Tunisie|Tunis|Algérie|Alger|"
                      r"Québec|Montréal|Arabie|Émirats|Emirats|Dubaï|Dubai|Abou Dhabi|Abu Dhabi|Qatar|Brésil|Chili|Mexique|Chine|Corée|Singapour|"
                      r"Irlande|Irland|Pays-Bas|Danemark|Dänemark|Norvège|Grèce|Griechenland|Suisse|Autriche|Kalifornien|Californie|Island|Iceland|"
                      r"Lagos|Johannesburg|Mumbai|Hyderabad|Chennai|Jakarta|Bangkok|Tokyo|Sydney|Melbourne|Lisbonne|Madrid|Milan|Mailand|Varsovie|Warschau)\b")
DC_RX = re.compile(r"data ?cent(re|er)s?|rechenzent|centres? de données|datacenter|hyperscale|campus IA|AI campus|KI-Rechen", re.I)
CAP_RX = re.compile(r"(\d{1,3}(?:[,.]\d{3})+|\d+(?:[.,]\d+)?)\s?(MW|GW|mégawatts?|megawatts?|Megawatt|gigawatts?|Gigawatt)\b", re.I)
GRID_RX = re.compile(r"\b(?:grid connection|grid capacity|connection (agreement|offer|date)|gate 2|substation|power (secured|connection)|\bgrid\b|"
                     r"Netzanschluss|Anschlusszusage|Umspannwerk|Stromnetz|raccordement|réseau électrique|\bRTE\b|National Grid|NESO|SSEN|UKPN|"
                     r"behind[- ]the[- ]meter|on-?site power|Anschluss(?:leistung)?|raccorder|raccordé)\b", re.I)
VERB_RX = re.compile(r"^(.{2,60}?)\s+(?:plans?|files?|submits?|secures?|gets|wins|unveils|announces|acquires|buys|launches|opens|proposes|lodges|"
                     r"receives|signs|eyes|seeks|reveals|breaks ground|begins|starts|to build|to develop|lands|bags|expands|confirms|"
                     r"kündigt|plant|baut|errichtet|investiert|erwirbt|startet|erhält|sichert|eröffnet|prévoit|lance|construit|annonce|"
                     r"inaugure|veut|dévoile|obtient|signe|implante|investit|projette|prépare|pousse)\b")
CUR_RX = re.compile(r"(?:(€|£|\$|EUR|GBP|USD)\s?(\d+(?:[.,]\d+)?)\s?(bn|billion|m|million|Mrd\.?|Milliarden|Mio\.?|milliards?|millions?)\b)|"
                    r"(?:(\d+(?:[.,]\d+)?)\s?(bn|billion|million|Mrd\.?|Milliarden|Mio\.?|milliards?|millions?)\s?(?:d'|de )?(euros?|€|Euro|pounds|£|dollars|\$|EUR|GBP|USD))", re.I)
GN = {"UK": [('("data centre" OR "data center") (MW OR GW OR megawatt) (UK OR England OR Scotland OR Wales OR London) when:90d', "en-GB", "GB", "GB:en"),
             ('("data centre" OR "AI growth zone") MW when:30d', "en-GB", "GB", "GB:en"),
             ('"data centre" (MW OR megawatt) (planning OR plans OR approved OR campus) when:90d', "en-GB", "GB", "GB:en"),
             ('hyperscale "data centre" MW (Scotland OR Wales OR England) when:90d', "en-GB", "GB", "GB:en")],
      "DE": [("(Rechenzentrum OR Rechenzentren OR \"data center\") (MW OR Megawatt) when:90d", "de", "DE", "DE:de"),
             ('("data center" OR "data centre") MW Germany when:90d', "en-GB", "GB", "GB:en"),
             ("Rechenzentrum Megawatt (geplant OR plant OR baut OR Campus) when:90d", "de", "DE", "DE:de")],
      "FR": [('("data center" OR "centre de données" OR datacenter OR "campus IA") (MW OR mégawatts) when:90d', "fr", "FR", "FR:fr"),
             ('("data center" OR "data centre") MW France when:90d', "en-GB", "GB", "GB:en"),
             ('"data center" MW (projet OR campus OR implantation) when:90d', "fr", "FR", "FR:fr")]}


def cap_mw(t):
    out = set()
    for m in CAP_RX.finditer(t):
        v = fnum(m.group(1).replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", m.group(1)) else m.group(1))
        if v is None:
            continue
        out.add(v * (1000 if m.group(2).lower().startswith("g") else 1))
    return out


def money(t):
    for m in CUR_RX.finditer(t):
        if m.group(1):
            c, a, u = m.group(1), m.group(2), m.group(3)
        else:
            a, u, c = m.group(4), m.group(5), m.group(6)
        c = {"€": "EUR", "£": "GBP", "$": "USD", "euro": "EUR", "euros": "EUR", "pounds": "GBP", "dollars": "USD"}.get(c.lower() if len(c) > 1 else c, c.upper())
        f = 1e9 if re.match(r"(bn|billion|mrd|milliard)", u, re.I) else 1e6
        v = fnum(a)
        if v:
            return dict(ccy=c, amt=v * f, txt=m.group(0).strip())
    return None


LOC_FR = re.compile(r"^([A-ZÉÈÎ][\w'’\-]+(?: [\w'’\-]+){0,2}) : |\b(?:à|de|en) ([A-ZÉÈÎ][\w'’\-]{2,})")
GRIDCO = {"DE": re.compile(r"\b(50Hertz|Amprion|TenneT|TransnetBW)\b"), "FR": re.compile(r"\b(RTE|Enedis)\b"),
          "UK": re.compile(r"\b(NESO|National Grid|SSEN|UKPN|SP Energy Networks|NGED)\b")}
MARKET_RX = re.compile(r"\bAktien?\b|\bbondit\b|\bbourse\b|\bshares?\b|\bstock\b|\b(the|with|signs|deal)\b", re.I)
LOC_DE = re.compile(r"\b(?:in|im|bei) ([A-ZÄÖÜ][\wäöüß\-]+)")


FOREIGN_CO = set()   # capitalised names from data-centre headlines about foreign sites (e.g. a developer behind a Casablanca project)


def site_ok(x):
    """Keep an item only when its site is in its tagged country: no foreign place in the headline and, for items whose
    location came from a headline pattern rather than the country gazetteer, no name shared with a foreign-site headline."""
    t = x.get("h") or ""
    if OTHER_RX.search(t) and not re.search(r"\b(UK|Germany|Deutschland|France)\b", t):
        return False
    if PLACE_RX[x["c"]].search(t):
        return True
    names = set(re.findall(r"\b[A-Z][\w\-]{3,}\b", t)) - {"Data", "Rechenzentrum", "Rechenzentren", "Megawatt"}
    return not (names & FOREIGN_CO)


def dc_item(c, d, title, url, src, pub, fx, native=False):
    """native: the query ran in the country's own language and edition, so a headline naming no foreign place is
    taken as domestic (location then comes from the headline pattern, else just the country)."""
    t = title
    if not DC_RX.search(t):
        return None
    pm_ = PLACE_RX[c].search(t)
    if OTHER_RX.search(t) and not re.search(r"\b(UK|Germany|Deutschland|France)\b", t):
        FOREIGN_CO.update(w for w in re.findall(r"\b[A-Z][\w\-]{3,}\b", t) if not PLACE_RX[c].search(w) and not DC_RX.search(w))
        return None
    if not pm_ and not native:
        return None
    if native and MARKET_RX.search(t):    # stock-market items and English wire copy in the native feeds
        return None
    caps = cap_mw(t)
    if not caps:                      # announcements only: a capacity must be stated
        return None
    if pm_:
        loc = pm_.group(0)
    else:
        m = (LOC_FR if c == "FR" else LOC_DE).search(t)
        loc = next((g for g in m.groups() if g), None) if m else None
        if loc and (OTHER_RX.search(loc) or DC_RX.search(loc)):
            return None
        if not loc and not GRIDCO[c].search(t):   # native item with no place and no grid company named: not attributable
            return None
    dev = None
    m = VERB_RX.match(re.sub(r"^(?:[\d.,]+\s?(?:MW|GW|Megawatt|mégawatts?)|[A-ZÉÈÎ][\w'’\-]+(?: [\w'’\-]+){0,2})\s*[:–]\s+", "", t, flags=re.I))
    if m and len(m.group(1).split()) <= 6 and m.group(1)[0].isupper() and not re.match(r"(Plans?|Why|How|What|The|New|Council|Inside|Data|Rechenzent|Le |La |Les |Un |Une |Der |Die |Das |Ein )", m.group(1)) \
            and not DC_RX.search(m.group(1)) and not CAP_RX.search(m.group(1)):
        dev = m.group(1).strip(" :,")
        if loc is None or dev == loc or loc in dev:   # subject may itself be the place: keep only when a separate location is named
            dev = None
    g = GRID_RX.search(t)
    it = dict(c=c, d=d, h=t, u=url, src=src, pub=pub, mw=max(caps) if len(caps) == 1 else None, loc=loc, dev=dev, grid=g.group(0) if g else None)
    mo = money(t)
    if mo and len(caps) == 1:
        r = (fx.get(mo["ccy"]) or {}).get("rate")
        mw = next(iter(caps))
        if r and mw > 0:
            eur = mo["amt"] / r
            if 2e5 <= eur / mw <= 5e7:
                it["pm"] = dict(amt=mo["txt"], ccy=mo["ccy"], fx=round(r, 4), mw=mw, eur_mw=round(eur / mw))
    return it


def dc_news(fx, df):
    out, st = [], []
    for c, (q, hl, gl, ce) in ((c, q) for c, qs in GN.items() for q in qs):
        try:
            u = "https://news.google.com/rss/search?" + urllib.parse.urlencode(dict(q=q, hl=hl, gl=gl, ceid=ce))
            root = ET.fromstring(get(u).content); n = 0
            for i in root.findall(".//item"):
                tt = (i.findtext("title") or "").strip()
                srcel = i.find("source"); pub = srcel.text if srcel is not None else ""
                if pub and tt.endswith(" - " + pub):
                    tt = tt[: -len(pub) - 3]
                try:
                    d = dt.datetime.strptime(i.findtext("pubDate")[5:16], "%d %b %Y").date().isoformat()
                except Exception:  # noqa: BLE001
                    continue
                link = i.findtext("link") or ""
                link = re.sub(r"^https://news\.google\.com/rss/articles/", "g:", link).split("?")[0]
                x = dc_item(c, d, tt, link, "Google News", pub, fx, native=hl[:2].lower() == c.lower()[:2] and c != "UK")
                if x:
                    out.append(x); n += 1
            st.append(dict(id="gnews-" + c.lower(), ok=True, n=n))
        except Exception as e:  # noqa: BLE001
            st.append(dict(id="gnews-" + c.lower(), ok=False, err=str(e)[:80]))
    try:
        root = ET.fromstring(get("https://www.datacenterdynamics.com/en/rss/").content); n = 0
        for i in root.findall(".//item"):
            tt = (i.findtext("title") or "").strip()
            if tt.startswith(("Sponsored", "DCD Studio")):
                continue
            d = dt.datetime.strptime(i.findtext("pubDate")[5:16], "%d %b %Y").date().isoformat()
            for c in GN:
                x = dc_item(c, d, tt, i.findtext("link"), "DCD", "DatacenterDynamics", fx)
                if x:
                    out.append(x); n += 1; break
        st.append(dict(id="dcd", ok=True, n=n))
    except Exception as e:  # noqa: BLE001
        st.append(dict(id="dcd", ok=False, err=str(e)[:80]))
    n = 0
    for x in (df or {}).get("items", []):
        if "datacentre" not in (x.get("f") or []) and not DC_RX.search(x.get("h", "")):
            continue
        full = x.get("h", "") + ". " + (x.get("x") or "")
        for c in GN:
            if PLACE_RX[c].search(full):
                y = dc_item(c, x["t"][:10], x["h"] if PLACE_RX[c].search(x["h"]) else x["h"] + " (" + PLACE_RX[c].search(full).group(0) + ")",
                            x["u"], "Deal flow", x.get("pub") or x.get("src"), fx)
                if y:
                    y["sym"] = x.get("s"); out.append(y); n += 1
                break
    st.append(dict(id="dealflow", ok=df is not None, n=n))
    return out, st


# ---------------------------------------------------------------- main
def merge(new, old, key, cap):
    seen, out = set(), []
    for x in sorted(new + (old or []), key=lambda x: x.get("d") or "", reverse=True):
        k = key(x)
        if k in seen or not in_win(x.get("d")):
            continue
        seen.add(k); out.append(x)
    return out[:cap]


def nkey(x):
    return re.sub(r"[^a-z0-9]+", "", (x.get("h") or "").lower())[:48]


def main():
    t0 = time.time()
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT))
        except Exception:  # noqa: BLE001
            prev = {}
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:  # noqa: BLE001
            state = {}
    try:
        fx = json.load(open(os.path.join(ROOT, "data", "market.json"))).get("fx_eur") or {}
    except Exception:  # noqa: BLE001
        fx = {}
    try:
        df = json.load(open(os.path.join(ROOT, "data", "dealflow.json")))
    except Exception:  # noqa: BLE001
        df = None
    out = dict(generated_utc=NOW.strftime("%Y-%m-%dT%H:%MZ"), window_days=WIN, sources=[], stale=[])
    src = out["sources"]

    def section(name, fn):
        t = time.time()
        try:
            v = fn(); log(f"{name}: ok in {time.time() - t:.0f}s"); return v
        except Exception as e:  # noqa: BLE001
            log(f"{name}: FAILED {type(e).__name__}: {str(e)[:120]}"); out["stale"].append(name)
            src.append(dict(id=name, ok=False, err=f"{type(e).__name__}: {str(e)[:80]}")); return None

    # UK registers + change log
    r = section("neso_registers", lambda: build_uk(state))
    if r:
        uk, packed, changes, removed = r
        regdate = max(v["date"] or "" for v in uk["regs"].values())
        log_ = (prev.get("uk") or {}).get("changes") or []
        if state.get("tec") and (changes or removed):
            for c in changes:
                c["seen"] = regdate
            log_ = changes + log_
            uk["last_diff"] = dict(date=regdate, new=sum(1 for c in changes if c["t"] == "new"), chg=sum(1 for c in changes if c["t"] == "chg"), removed=removed)
        else:
            uk["last_diff"] = (prev.get("uk") or {}).get("last_diff")
        uk["changes"] = [c for c in log_ if in_win(c.get("seen"))][:60]
        uk["baseline"] = state.get("tec_since") or regdate
        state["tec"] = packed; state["tec_since"] = uk["baseline"]; state["tec_date"] = regdate
        src.append(dict(id="neso_registers", ok=True, n=uk["n"]))
    else:
        uk = prev.get("uk")
    nn = section("neso_news", neso_news)
    if uk is not None:
        uk["pubs"] = merge(nn or [], (prev.get("uk") or {}).get("pubs"), lambda x: x["u"], 12)
        if nn is not None:
            src.append(dict(id="neso_news", ok=True, n=len(nn)))
    out["uk"] = uk
    # DE
    de = {}
    b = section("bnetza", bnetza)
    de["solar"] = b or (prev.get("de") or {}).get("solar")
    if b:
        src.append(dict(id="bnetza", ok=True, n=len(b["rounds"])))
    tso = section("tso", tso_items)
    if tso:
        items, st = tso; src.extend(st)
        old = (prev.get("de") or {}).get("notices") or []
        allx = merge(items, old, lambda x: x["u"], 12)
        de["notices"] = allx
        de["latest"] = items[0] if items and not allx else None
    else:
        de["notices"] = (prev.get("de") or {}).get("notices") or []
        de["latest"] = (prev.get("de") or {}).get("latest")
    de["tso_dropped"] = [dict(id="50hertz", why="press list is rendered in the browser and the RSS feed sits behind a login")]
    out["de"] = de
    # FR
    fr = {}
    q = section("odre", odre)
    fr["queue"] = q or (prev.get("fr") or {}).get("queue")
    if q:
        src.append(dict(id="odre", ok=True, n=len(q["regions"])))
    cache = state.get("cre") or {}
    c = section("cre", lambda: cre(cache))
    if c:
        fr["cre"], errs = c
        state["cre"] = cache
        src.append(dict(id="cre", ok=any(t["periods"] for t in fr["cre"].values()), n=sum(len(t["periods"]) for t in fr["cre"].values()), err="; ".join(errs)[:160] or None))
    else:
        fr["cre"] = (prev.get("fr") or {}).get("cre")
    fr["dropped"] = [dict(id="capareseau", why="Caparéseau is a map application with no public data download"),
                     dict(id="rte_news", why="RTE news list is rendered in the browser; no RSS feed")]
    out["fr"] = fr
    # DC
    d = section("dc_news", lambda: dc_news(fx, df))
    if d:
        items, st = d; src.extend(st)
        old = prev.get("dc") or []
        merged = merge(items, old, nkey, 200)
        for x in merged:     # stored items too: learn names from foreign-site headlines, then re-check every item's site
            if OTHER_RX.search(x.get("h") or "") and not re.search(r"\b(UK|Germany|Deutschland|France)\b", x.get("h") or ""):
                FOREIGN_CO.update(w for w in re.findall(r"\b[A-Z][\w\-]{3,}\b", x["h"]) if not PLACE_RX[x["c"]].search(w) and not DC_RX.search(w))
        drop = [x for x in merged if not site_ok(x)]
        if drop:
            print("dc: dropped (site outside UK/DE/FR):", "; ".join(f'{x["c"]} {x["h"][:70]}' for x in drop))
        merged = [x for x in merged if site_ok(x)]
        per = {}
        keep = []
        for x in merged:
            if per.get(x["c"], 0) < 10:
                per[x["c"]] = per.get(x["c"], 0) + 1; keep.append(x)
        out["dc"] = keep
    else:
        out["dc"] = prev.get("dc") or []
    out["build_s"] = round(time.time() - t0)
    js = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    with open(OUT, "w") as f:
        f.write(js)
    with open(STATE, "w") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    log(f"wrote {OUT} ({len(js) / 1024:.0f} KB) + state ({os.path.getsize(STATE) / 1024:.0f} KB) in {out['build_s']} s; stale={out['stale']}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Project progression tracker (Grid & DC tab): stage changes of individual projects in public registers.

  python scripts/build_tracker.py uk              daily: NESO TEC + embedded registers (CKAN), ~10 s
  python scripts/build_tracker.py uk --backfill D [--partials G]
                                                  rebuild UK history from dated register CSVs in D (archived copies,
                                                  file date taken from the file name) plus optional partial
                                                  observations: earlier data/griddc.json copies in G (changed entries)
  python scripts/build_tracker.py de              daily: Marktstammdatenregister units > 1 MW (ground-mounted solar,
                                                  battery storage, wind), all operating statuses, ~4-6 min

History lives outside data/ in TRACKER_HIST (default data/history; the workflow checks out the orphan branch
tracker-history there and pushes it back fast-forward only). The live-data branch is rebuilt as one commit on every
run, so it cannot hold an append-only log. Files (tab-separated, sorted, small diffs):
  uk_state.tsv / de_state.tsv    one line per project/unit: current stage, date first seen at that stage, ...
  uk_events.tsv / de_events.tsv  one line per stage change: date seen, previous observation date, from, to, ...
  uk_obs.tsv / de_obs.tsv        every register snapshot applied (date, register, full/partial, source)
An event is dated by the register file in which the new stage was first seen; the previous observation date is
kept, so the change happened somewhere in between. Nothing is interpolated. Customer / operator names are never
stored. MaStR units registered by natural persons are stored without their unit name.
UI files (published to live-data): data/tracker_uk.json, data/tracker_de.json (solar + battery), data/tracker_de_wind.json.
"""
import csv, datetime as dt, glob, io, json, os, re, sys, time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import write_json  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.environ.get("TRACKER_HIST") or os.path.join(ROOT, "data", "history")
DATA = os.path.join(ROOT, "data")
UTC = dt.timezone.utc
NOWS = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
TODAY = dt.datetime.now(UTC).date().isoformat()
H = {"User-Agent": "Mozilla/5.0 (markets-monitor; public data)"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, tries=3, **kw):
    err = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=H, timeout=120, **kw)
            if r.status_code == 200:
                return r
            err = IOError(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            err = e
        time.sleep(4 * (i + 1))
    raise err


# ------------------------------------------------------------------ TSV store
def rd_tsv(name, ncol):
    p = os.path.join(HIST, name)
    if not os.path.exists(p):
        return []
    out = []
    for ln in open(p, encoding="utf-8"):
        ln = ln.rstrip("\n")
        if ln and not ln.startswith("#"):
            v = ln.split("\t"); out.append(v + [""] * (ncol - len(v)))
    return out


def wr_tsv(name, rows, head):
    os.makedirs(HIST, exist_ok=True)
    p = os.path.join(HIST, name); tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# " + "\t".join(head) + "\n")
        for r in rows:
            f.write("\t".join(("" if x is None else str(x)).replace("\t", " ").replace("\n", " ") for x in r) + "\n")
    os.replace(tmp, p)


def num(s):
    s = (s or "").strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def mwfmt(x):
    return "" if x is None else ("%.3f" % x).rstrip("0").rstrip(".")


# ================================================================== UK
UK_ST = ["Scoping", "Awaiting Consents", "Consents Approved", "Under Construction/Commissioning", "Built"]
UK_REG = {"T": "transmission-entry-capacity-tec-register", "E": "embedded-register"}
NESO = "https://api.neso.energy/api/3/action/package_show?id="
UST_H = ["key", "stage", "since", "since_prev", "first_seen", "last_seen", "name", "tech", "mw", "site", "eff", "gone", "merged"]
UEV_H = ["date", "prev", "key", "from", "to", "mw", "name", "tech", "src"]
UOB_H = ["date", "reg", "kind", "source"]


def tech_uk(pt):
    t = set()
    for p in (pt or "").lower().split(";"):
        p = p.strip()
        if not p:
            continue
        if "pv array" in p or "solar" in p: t.add("S")
        elif "energy storage" in p or p.startswith("laes"): t.add("B")
        elif "wind onshore" in p: t.add("W")
        elif "wind offshore" in p: t.add("O")
        elif any(k in p for k in ("ccgt", "ocgt", "gas", "chp", "thermal", "oil")): t.add("G")
        elif p == "demand": t.add("D")
        else: t.add("X")   # hydro, pumped storage, nuclear, biomass, tidal, hydrogen, reactive compensation, ...
    return "".join(sorted(t)) or "X"


MONTHS = {m.lower(): i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                              "September", "October", "November", "December"], 1)}


def file_date(name):
    m = re.search(r"(\d{1,2})-(\d{1,2}|[a-z]+)-(20\d\d)", name.lower())
    if not m:
        return None
    d, mo, y = m.groups()
    mo = int(mo) if mo.isdigit() else MONTHS.get(mo)
    try:
        return dt.date(int(y), mo, int(d)).isoformat()
    except (TypeError, ValueError):
        return None


def isod(s):
    s = (s or "").strip()
    for f in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s[:10], f).date().isoformat()
        except ValueError:
            pass
    return ""


def uk_rows(reg, text):
    """Parse one register CSV into {key: row}. Key = register letter + ':' + NESO project number."""
    rows = list(csv.DictReader(io.StringIO(text)))
    out = {}
    for x in rows:
        pn = (x.get("Project Number") or x.get("Project No") or "").strip()
        if not pn:
            continue
        st = (x.get("Project Status") or "").strip()
        r = dict(stage=st, name=(x.get("Project Name") or "").strip(), tech=tech_uk(x.get("Plant Type")),
                 mw=num(x.get("Cumulative Total Capacity (MW)")), site=(x.get("Connection Site") or "").strip(),
                 eff=isod(x.get("MW Effective From")))
        k = reg + ":" + pn
        i = 2
        while k in out:                       # rare duplicate project numbers within one file
            k = reg + ":" + pn + "#" + str(i); i += 1
        out[k] = r
    return out, len(rows)


class UK:
    def __init__(self):
        self.st = {r[0]: dict(zip(UST_H, r)) for r in rd_tsv("uk_state.tsv", len(UST_H))}
        self.ev = rd_tsv("uk_events.tsv", len(UEV_H))
        self.obs = rd_tsv("uk_obs.tsv", len(UOB_H))

    def last_full(self, reg):
        d = [o[0] for o in self.obs if o[1] == reg and o[2] == "full"]
        return max(d) if d else None

    def first_full(self, reg):
        d = [o[0] for o in self.obs if o[1] == reg and o[2] == "full"]
        return min(d) if d else None

    def event(self, date, prev, k, fr, to, s, src):
        self.ev.append([date, prev or "", k, fr, to, mwfmt(num(s.get("mw")) if isinstance(s.get("mw"), str) else s.get("mw")), s["name"], s["tech"], src])

    def apply_full(self, reg, date, rows, src):
        lf = self.last_full(reg)
        if lf and date <= lf:
            log(f"UK {reg}: register {date} already applied (last {lf})"); return 0
        n0 = len(self.ev)
        if lf:
            self.renumber(reg, date, rows)
        for k, r in rows.items():
            s = self.st.get(k)
            if s is None or s["gone"]:
                if lf:      # not in the first snapshot: entered the register
                    self.event(date, lf, k, "", r["stage"], r, src)
                s = dict(key=k, stage=r["stage"], since=date if lf else "", since_prev=lf or "", first_seen=date, gone="")
                self.st[k] = s
            elif s["stage"] != r["stage"]:
                self.event(date, s["last_seen"], k, s["stage"], r["stage"], r, src)
                s.update(stage=r["stage"], since=date, since_prev=s["last_seen"])
            s.update(name=r["name"], tech=r["tech"], mw=mwfmt(r["mw"]), site=r["site"], eff=r["eff"], last_seen=date)
        for k, s in self.st.items():
            if k.startswith(reg + ":") and not s["gone"] and k not in rows:
                self.event(date, s["last_seen"], k, s["stage"], "", s, src)
                s.update(gone=date)
        self.obs.append([date, reg, "full", src])
        return len(self.ev) - n0

    def renumber(self, reg, date, rows):
        """NESO splits or merges the phases of a project by changing the suffix of its number (PRO-000012 ->
        PRO-000012-1 + PRO-000012-2, and back). Pair entries that vanished with entries that appeared under the same
        base number (same stage first, then closest MW) and carry the history over: not an exit plus an entry.
        A vanished phase whose base number is still in the register is treated as merged (no event)."""
        def base(k):
            m = re.match(r"^[TE]:PRO-\d{6}", k)
            return m.group() if m else k.split("#")[0]
        gone = [k for k, s in self.st.items() if k.startswith(reg + ":") and not s["gone"] and k not in rows]
        new = [k for k in rows if k not in self.st or self.st[k]["gone"]]
        gb, nb = {}, {}
        for k in gone: gb.setdefault(base(k), []).append(k)
        for k in new: nb.setdefault(base(k), []).append(k)
        live_bases = {base(k) for k in rows}
        for b, nks in nb.items():
            olds = gb.get(b, [])
            if not olds:
                continue
            mw = lambda k, src: num(src[k]["mw"]) if isinstance(src[k]["mw"], str) else (src[k]["mw"] or 0)
            pairs = []
            for nk in sorted(nks):
                cand = [o for o in olds if self.st[o]["stage"] == rows[nk]["stage"]] or olds
                o = min(cand, key=lambda o: abs((mw(o, self.st) or 0) - (rows[nk]["mw"] or 0)))
                pairs.append((nk, o))
            for nk, o in pairs:
                s = dict(self.st[o]); s.update(key=nk, gone="", renum_from=o)
                self.st[nk] = s
            for o in olds:
                self.st[o].update(gone=date, merged="1")
        for b, olds in gb.items():
            if b in live_bases and b not in nb:
                for o in olds:
                    self.st[o].update(gone=date, merged="1")

    def apply_partial(self, reg, date, items, src):
        """items: {key: dict(stage,name,tech?,mw,new)} from an earlier griddc.json change list (subset of entries)."""
        n0 = len(self.ev)
        for k, r in items.items():
            s = self.st.get(k)
            if s is None or s["gone"] or date <= s["last_seen"]:
                continue
            if s["stage"] != r["stage"]:
                self.event(date, s["last_seen"], k, s["stage"], r["stage"], dict(s, mw=r["mw"]), src)
                s.update(stage=r["stage"], since=date, since_prev=s["last_seen"])
            s.update(last_seen=date, mw=mwfmt(r["mw"]))
        self.obs.append([date, reg, "partial", src])
        return len(self.ev) - n0

    def save(self):
        wr_tsv("uk_state.tsv", [[s.get(h, "") for h in UST_H] for _, s in sorted(self.st.items())], UST_H)
        self.ev.sort(key=lambda e: (e[0], e[2]))
        wr_tsv("uk_events.tsv", self.ev, UEV_H)
        self.obs.sort()
        wr_tsv("uk_obs.tsv", self.obs, UOB_H)


def griddc_partials(gdir):
    """Change lists of earlier data/griddc.json builds: {(reg, date): {key: item}} (solar/storage/demand entries only)."""
    out = {}
    for f in sorted(glob.glob(os.path.join(gdir, "*.json"))):
        try:
            u = json.load(open(f))["uk"]
        except Exception:  # noqa: BLE001
            continue
        for c in u.get("changes") or []:
            if not c.get("pn") or not c.get("seen"):
                continue
            out.setdefault((c["r"], c["seen"]), {})[c["r"] + ":" + c["pn"]] = dict(stage=c.get("s", ""), name=c.get("n", ""), mw=c.get("mw"))
    return out


def uk_backfill(d, gdir):
    for p in ("uk_state.tsv", "uk_events.tsv", "uk_obs.tsv"):
        if os.path.exists(os.path.join(HIST, p)):
            os.remove(os.path.join(HIST, p))
    U = UK()
    snaps = []
    for f in glob.glob(os.path.join(d, "*.csv")):
        b = os.path.basename(f).lower()
        reg = "T" if b.startswith("tec-register") else "E" if b.startswith("embedded-register") else None
        fd = file_date(b)
        if reg and fd:
            snaps.append((fd, 0, reg, f))
    parts = griddc_partials(gdir) if gdir else {}
    for (reg, date), items in parts.items():
        snaps.append((date, 1, reg, items))
    latest = {}
    for fd, k, reg, f in sorted(s for s in snaps if s[1] == 0):
        latest[reg] = os.path.basename(f)
    for date, kind, reg, x in sorted(snaps, key=lambda s: (s[0], s[1], s[2])):
        if kind == 0:
            rows, n = uk_rows(reg, open(x, encoding="utf-8-sig", errors="replace").read())
            e = U.apply_full(reg, date, rows, "NESO register " + date)
            U.obs[-1][3] += " · " + ("web.archive.org capture of the NESO file" if os.path.basename(x) != latest.get(reg) else "NESO data portal")
            log(f"UK {reg} {date}: {n} rows, {e} events")
        else:
            lf = U.last_full(reg)
            # partial observations only between two full snapshots (a later full snapshot must exist or come)
            if lf and date > lf:
                e = U.apply_partial(reg, date, x, "NESO register " + date + " · entries flagged as changed by this site's daily build")
                log(f"UK {reg} {date}: partial {len(x)} entries, {e} events")
    U.save()
    return U


def uk_daily():
    U = UK()
    if not U.obs:
        raise SystemExit("no UK history found in " + HIST + " (run --backfill first or check out tracker-history)")
    for reg, pkg in UK_REG.items():
        p = get(NESO + pkg).json()["result"]
        url = p["resources"][0]["url"]
        fd = file_date(url.split("?")[0].rsplit("/", 1)[-1]) or (p["resources"][0].get("last_modified") or "")[:10]
        lf = U.last_full(reg)
        if lf and fd <= lf:
            log(f"UK {reg}: register {fd} unchanged since last run"); continue
        rows, n = uk_rows(reg, get(url).content.decode("utf-8-sig", "replace"))
        prev_n = sum(1 for k, s in U.st.items() if k.startswith(reg + ":") and not s["gone"])
        if n < 0.6 * prev_n or not rows:
            raise SystemExit(f"UK {reg}: only {n} rows vs {prev_n} tracked; register file looks incomplete, history not changed")
        e = U.apply_full(reg, fd, rows, "NESO register " + fd)
        log(f"UK {reg} {fd}: {n} rows, {e} events")
    U.save()
    return U


def ui_uk(U):
    names, sites = [], {}
    rows = sorted((s for s in U.st.values() if not s["gone"]), key=lambda s: -(num(s["mw"]) or 0))
    col = dict(k=[], n=[], r=[], t=[], mw=[], s=[], since=[], sp=[], eff=[], site=[])
    for s in rows:
        col["k"].append(s["key"].split(":", 1)[1]); col["n"].append(s["name"]); col["r"].append(s["key"][0])
        col["t"].append(s["tech"]); col["mw"].append(num(s["mw"])); col["s"].append(UK_ST.index(s["stage"]) if s["stage"] in UK_ST else -1)
        col["since"].append(s["since"]); col["sp"].append(s["since_prev"]); col["eff"].append(s["eff"])
        col["site"].append(sites.setdefault(s["site"], len(sites)))
    ev = sorted(U.ev, key=lambda e: (e[0], e[2]), reverse=True)
    st = lambda x: UK_ST.index(x) if x in UK_ST else (-1 if x == "" else 9)  # -1 = not in register
    moves = [[e[0], e[1], e[2].split(":", 1)[1], e[2][0], e[6], e[7], num(e[5]), st(e[3]), st(e[4]), e[8]] for e in ev[:4000]]
    snaps = sorted({(o[0], o[1], o[2]) for o in U.obs})
    out = dict(generated_utc=NOWS, country="UK", stages=["Offer held (scoping)", "Awaiting consents", "Consents approved", "Under construction", "Built"],
               raw_stages=UK_ST, hist_start={r: U.first_full(r) for r in UK_REG}, asof={r: U.last_full(r) for r in UK_REG},
               snapshots=[list(s) for s in snaps], n_events=len(U.ev),
               links={"T": "https://www.neso.energy/data-portal/" + UK_REG["T"], "E": "https://www.neso.energy/data-portal/" + UK_REG["E"]},
               sites=list(sites), rows=col,
               moves_cols=["date", "prev_obs", "project_no", "reg", "name", "tech", "mw", "from", "to", "source"], moves=moves)
    write_json(os.path.join(DATA, "tracker_uk.json"), out)
    log(f"tracker_uk.json {os.path.getsize(os.path.join(DATA, 'tracker_uk.json'))} B, {len(rows)} projects, {len(U.ev)} events")


# ================================================================== DE (MaStR)
API = "https://www.marktstammdatenregister.de/MaStR/Einheit/EinheitJson/GetErweiterteOeffentlicheEinheitStromerzeugung"
WEB = "https://www.marktstammdatenregister.de/MaStR/Einheit/Einheiten/ErweiterteOeffentlicheEinheitenuebersicht"
BIG = "~and~Nettonennleistung der Einheit~gt~'1000'"
DE_T = {"S": "Energieträger~eq~'2495'~and~Art der Solaranlage~eq~'852'",
        "B": "Energieträger~eq~'2496'~and~Speichertechnologie~eq~'524'",
        "W": "Energieträger~eq~'2497'"}
DE_ST = {31: "In Planung", 35: "In Betrieb", 37: "Vorübergehend stillgelegt", 38: "Endgültig stillgelegt"}
DE_ORD = [31, 35, 37, 38]
DST_H = ["id", "status", "since", "since_prev", "first_seen", "last_seen", "gone", "kw", "name", "tech", "land", "kreis", "uid"]
DEV_H = ["date", "prev", "id", "from", "to", "kw", "name", "tech", "land", "kreis", "uid"]
DOB_H = ["date", "reg", "kind", "source"]
PS = 2500


def msd(s):
    m = re.search(r"-?\d+", s or "")
    return dt.datetime.fromtimestamp(int(m.group()) / 1000, UTC).date().isoformat() if m else ""


def de_fetch(tech):
    out, page, tot = {}, 1, None
    while True:
        for k in range(4):
            try:
                r = requests.get(API, params=dict(page=page, pageSize=PS, filter=DE_T[tech] + BIG), headers=H, timeout=180)
                r.raise_for_status(); j = r.json(); break
            except Exception:  # noqa: BLE001
                if k == 3:
                    raise
                time.sleep(10 * (k + 1))
        d = j.get("Data") or []
        tot = j.get("Total") if tot is None else tot
        for x in d:
            sid = x.get("MaStRNummer")
            if not sid:
                continue
            org = x.get("AnlagenbetreiberPersonenArt") == 517
            out[sid] = dict(st=x.get("BetriebsStatusId"), kw=x.get("Nettonennleistung"), uid=x.get("Id"),
                            name=(x.get("EinheitName") or "").strip() if org else "",
                            land=x.get("Bundesland") or "", kreis=x.get("Landkreis") or "",
                            comm=msd(x.get("InbetriebnahmeDatum")), plan=msd(x.get("GeplantesInbetriebsnahmeDatum")),
                            regd=msd(x.get("EinheitRegistrierungsdatum")), stop=msd(x.get("EndgueltigeStilllegungDatum")))
        if len(d) < PS or page * PS >= (tot or 0):
            break
        page += 1
        time.sleep(0.5)
    if tot is None or len(out) < tot * 0.995:
        raise IOError(f"MaStR {tech}: got {len(out)} of {tot} units")
    return out


def de_daily():
    t0 = time.time()
    st = {r[0]: dict(zip(DST_H, r)) for r in rd_tsv("de_state.tsv", len(DST_H))}
    ev = rd_tsv("de_events.tsv", len(DEV_H))
    obs = rd_tsv("de_obs.tsv", len(DOB_H))
    cur = {}
    for tech in DE_T:
        u = de_fetch(tech)
        for v in u.values():
            v["tech"] = tech
        had = sum(1 for s in st.values() if not s["gone"] and s.get("tech") == tech)
        if had and len(u) < 0.9 * had:
            raise SystemExit(f"MaStR {tech}: {len(u)} units vs {had} tracked; query looks broken, history not changed")
        cur.update(u)
        log(f"MaStR {tech}: {len(u)} units > 1 MW ({time.time() - t0:.0f} s)")
    last = max((o[0] for o in obs), default=None)
    if last and TODAY <= last:
        log(f"DE: snapshot {TODAY} already applied"); return cur, st, ev, obs
    src = "MaStR snapshot " + TODAY
    for sid, x in cur.items():
        sname = DE_ST.get(x["st"], str(x["st"]))
        s = st.get(sid)
        e = lambda fr, prev: ev.append([TODAY, prev or "", sid, fr, sname, x["kw"], x["name"], x["tech"], x["land"], x["kreis"], x["uid"]])
        if s is None or s["gone"]:
            if last:
                e("", last)
            st[sid] = s = dict(id=sid, status=sname, since=TODAY if last else "", since_prev=last or "",
                               first_seen=(s or {}).get("first_seen") or TODAY, gone="")
        elif s["status"] != sname:
            e(s["status"], s["last_seen"])
            s.update(status=sname, since=TODAY, since_prev=s["last_seen"])
        s["last_seen"] = TODAY
        s.update(kw=x["kw"], name=x["name"], tech=x["tech"], land=x["land"], kreis=x["kreis"], uid=x["uid"])
    for sid, s in st.items():
        if not s["gone"] and sid not in cur:  # left the set: deleted, or now <= 1 MW / other technology
            ev.append([TODAY, s["last_seen"], sid, s["status"], "", s.get("kw", ""), s.get("name", ""), s.get("tech", ""),
                       s.get("land", ""), s.get("kreis", ""), s.get("uid", "")])
            s["gone"] = TODAY
    obs.append([TODAY, "MaStR", "full", src + f" ({len(cur)} units)"])
    wr_tsv("de_state.tsv", [[s.get(h, "") for h in DST_H] for _, s in sorted(st.items())], DST_H)
    ev.sort(key=lambda r: (r[0], r[2])); wr_tsv("de_events.tsv", ev, DEV_H)
    wr_tsv("de_obs.tsv", sorted(obs), DOB_H)
    log(f"DE: {len(cur)} units, {len(ev)} events in total, {time.time() - t0:.0f} s")
    return cur, st, ev, obs


def ui_de(cur, st, ev, obs):
    start = min((o[0] for o in obs), default=TODAY)
    lands, kreise = {}, {}
    STI = {DE_ST[c]: i for i, c in enumerate(DE_ORD)}

    def pack(techs):
        col = dict(n=[], t=[], kw=[], s=[], since=[], sp=[], comm=[], plan=[], land=[], kreis=[], uid=[])
        for sid, x in sorted(cur.items(), key=lambda kv: -(kv[1]["kw"] or 0)):
            if x["tech"] not in techs:
                continue
            s = st.get(sid, {})
            col["n"].append(x["name"]); col["t"].append(x["tech"]); col["kw"].append(round(x["kw"] or 0))
            col["s"].append(STI.get(DE_ST.get(x["st"], ""), -1)); col["since"].append(s.get("since", "")); col["sp"].append(s.get("since_prev", ""))
            col["comm"].append(x["comm"]); col["plan"].append(x["plan"])
            col["land"].append(lands.setdefault(x["land"], len(lands))); col["kreis"].append(kreise.setdefault(x["kreis"], len(kreise)))
            col["uid"].append(x["uid"])
        return col

    stx = lambda v: STI.get(v, -1 if v == "" else 9)
    for name, techs in (("tracker_de.json", "SB"), ("tracker_de_wind.json", "W")):
        lands.clear(); kreise.clear()
        col = pack(techs)
        mv = []
        for e in sorted(ev, key=lambda r: (r[0], r[2]), reverse=True):
            x = cur.get(e[2])
            tech = e[7] or (x and x["tech"]) or ""
            if tech and tech not in techs:
                continue
            if not tech:
                continue
            mv.append([e[0], e[1], e[2], e[6] or (x and x["name"]) or "", tech, round(float(e[5] or 0)) if e[5] else (x and round(x["kw"] or 0)),
                       stx(e[3]), stx(e[4]), e[8] or (x and x["land"]) or "", e[9] or (x and x["kreis"]) or "", int(e[10]) if str(e[10]).isdigit() else None])
            if len(mv) >= 4000:
                break
        out = dict(generated_utc=NOWS, country="DE", techs=techs, stages=[DE_ST[c] for c in DE_ORD],
                   stages_en=["In planning", "In operation", "Temporarily shut down", "Permanently shut down"],
                   hist_start=start, asof=max((o[0] for o in obs), default=TODAY), snapshots=sorted({o[0] for o in obs}),
                   url=WEB, unit_url="https://www.marktstammdatenregister.de/MaStR/Einheit/Detail/IndexOeffentlich/",
                   lands=list(lands), kreise=list(kreise), rows=col,
                   moves_cols=["date", "prev_obs", "mastr_id", "name", "tech", "kw", "from", "to", "land", "kreis", "uid"], moves=mv)
        write_json(os.path.join(DATA, name), out)
        log(f"{name} {os.path.getsize(os.path.join(DATA, name))} B, {len(col['n'])} units, {len(mv)} moves")


def main():
    a = sys.argv[1:]
    if not a or a[0] not in ("uk", "de"):
        raise SystemExit(__doc__)
    if a[0] == "uk":
        if "--backfill" in a:
            U = uk_backfill(a[a.index("--backfill") + 1], a[a.index("--partials") + 1] if "--partials" in a else None)
        else:
            U = uk_daily()
        ui_uk(U)
    else:
        ui_de(*de_daily())


if __name__ == "__main__":
    main()

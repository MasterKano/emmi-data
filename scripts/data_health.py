#!/usr/bin/env python3
"""Central EMMI data health check (freshness, missing runs, partial failures, manifest consistency).

Reads the published datasets (main and live-data), the Actions run history and the in-file error lists, judges each
dataset against its OWN publication cadence and acceptable lag (REGISTRY below), and reports:
  * stale observation      the newest data point inside the file is older than the allowed lag
  * stale build            the file was not rebuilt within its cadence
  * missing run            a scheduled workflow has not started within its expected gap (manual runs do not count)
  * failed workflow        the latest completed run failed
  * partial failure        a green build still carries errors/gaps in the data, or a step failed behind continue-on-error
  * manifest outdated      nordic-v2/manifest.json per-file entries differ from the files themselves
Expected delays are not failures: NVE publishes Wednesday 13:00 Oslo, Energiforetagen's weekly PDF trails the week by up
to 16 days, EEX settlements trail by up to 2 business days (accepted), ERA5 trails by about 5 days, and so on.

Alert path: one GitHub issue ("EMMI data health") assigned to the repo owner, opened/updated when the set of alerts changes,
closed when clear; the job exits 1 when the alert set changes (and once a day while unresolved) so GitHub's failed-workflow
email goes out too. No other service. Status for dashboards is published to the orphan branch `data-health`.
--simulate-days N shifts "now" forward N days (a deliberate staleness/missing-run test, issue prefixed [TEST]).
"""
import argparse, datetime as dt, fnmatch, hashlib, json, os, re, subprocess, sys, tempfile
from zoneinfo import ZoneInfo

UTC, ZH, OSLO = dt.timezone.utc, ZoneInfo("Europe/Zurich"), ZoneInfo("Europe/Oslo")
REPO = os.environ.get("GITHUB_REPOSITORY", "MasterKano/emmi-data")
ISSUE_TITLE = "EMMI data health"
H = 3600.0

# ------------------------------------------------------------------ helpers
_cache = {}


def sh(*a, check=True):
    r = subprocess.run(a, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(a)[:80]}: {r.stderr.strip()[:200]}")
    return r.stdout


def load(ref, path):
    key = (ref, path)
    if key not in _cache:
        try:
            if ref == "main":
                with open(path, encoding="utf-8") as fh:
                    _cache[key] = json.load(fh)
            else:
                _cache[key] = json.loads(sh("git", "show", f"origin/{ref}:{path}"))
        except Exception as e:  # missing or unreadable file is itself a finding
            _cache[key] = e
    return _cache[key]


def jp(d, path):
    """a/b/c lookup; a '*' segment fans out over dict values/list items; returns a list of leaves."""
    cur = [d]
    for seg in path.split("/"):
        nxt = []
        for c in cur:
            if seg == "*":
                nxt += list(c.values()) if isinstance(c, dict) else list(c) if isinstance(c, list) else []
            elif isinstance(c, dict) and "*" in seg:
                nxt += [v for k, v in c.items() if fnmatch.fnmatch(k, seg)]
            elif isinstance(c, dict) and seg in c:
                nxt.append(c[seg])
        cur = nxt
    return [c for c in cur if c is not None]


def parse_ts(v):
    v = str(v).strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return dt.datetime.fromisoformat(v).replace(tzinfo=UTC)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{2}:\d{2}", v):
        pass
    t = dt.datetime.fromisoformat(v)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def biz_lag(d0, d1):
    """Whole business days (Mon-Fri) from d0 to d1 (d0 <= d1)."""
    n, d = 0, d0
    while d < d1:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def age_h(ts, now, weekday_only=False):
    """Hours since ts; with weekday_only the Saturday/Sunday (Zurich) hours are not counted."""
    h = (now - ts).total_seconds() / H
    if weekday_only:
        d = ts.astimezone(ZH).replace(hour=0, minute=0, second=0, microsecond=0)
        while d < now.astimezone(ZH):
            if d.weekday() >= 5:
                s, e = max(d, ts.astimezone(ZH)), min(d + dt.timedelta(days=1), now.astimezone(ZH))
                h -= max(0.0, (e - s).total_seconds() / H)
            d += dt.timedelta(days=1)
    return h


def nve_expected_sunday(now, grace_h):
    """Newest NVE reservoir Sunday that must be in a file: published Wednesday 13:00 Oslo, plus a grace for the builder."""
    o = now.astimezone(OSLO)
    wed = (o - dt.timedelta(days=(o.weekday() - 2) % 7)).replace(hour=13, minute=0, second=0, microsecond=0)
    if o < wed + dt.timedelta(hours=grace_h):
        wed -= dt.timedelta(days=7)
    return (wed - dt.timedelta(days=3)).date()


SV_MONTH = dict(jan=1, feb=2, mar=3, apr=4, maj=5, jun=6, jul=7, aug=8, sep=9, okt=10, nov=11, dec=12)


def sv_period_end(p, year):
    m = re.search(r"-\s*(\d{1,2})\s+([a-z]{3})", str(p).lower())
    return dt.date(int(year), SV_MONTH[m.group(2)], int(m.group(1))) if m else None


def scan_errors(d, path="", depth=0, out=None):
    out = [] if out is None else out
    if isinstance(d, dict) and depth < 4:
        for k, v in d.items():
            if k in ("errors", "gaps", "failed") and isinstance(v, list) and v:
                msg = v[0].get("error") or v[0].get("part") if isinstance(v[0], dict) else v[0]
                out.append((f"{path}/{k}".lstrip("/"), len(v), str(msg)[:90]))
            elif isinstance(v, dict):
                scan_errors(v, f"{path}/{k}", depth + 1, out)
    return out


# ------------------------------------------------------------------ checks (each returns dict(ok, latest, note))
def c_date(ref, path, field, lag_days, mode="cal", agg=min):
    def f(now):
        d = load(ref, path)
        if isinstance(d, Exception):
            return dict(ok=False, latest="unreadable", note=f"{path}: {str(d)[:80]}")
        vals = [parse_ts(v).date() for v in jp(d, field)]
        if not vals:
            return dict(ok=False, latest="none", note=f"no {field}")
        obs = agg(vals)
        today = now.astimezone(ZH).date()
        lag = biz_lag(obs, today) if mode == "biz" else (today - obs).days
        unit = "business days" if mode == "biz" else "days"
        return dict(ok=lag <= lag_days, latest=str(obs), note=f"{lag} {unit} old, allowed {lag_days}")
    return f


def c_gen(ref, path, field, max_h, weekday_only=False):
    def f(now):
        d = load(ref, path)
        if isinstance(d, Exception):
            return dict(ok=False, latest="unreadable", note=f"{path}: {str(d)[:80]}")
        vals = [parse_ts(v) for v in jp(d, field)]
        if not vals:
            return dict(ok=False, latest="none", note=f"no {field}")
        t = min(vals)
        a = age_h(t, now, weekday_only)
        return dict(ok=a <= max_h, latest=t.strftime("%Y-%m-%d %H:%MZ"), note=f"built {a:.0f} h ago, allowed {max_h:.0f} h" + (" (weekends excluded)" if weekday_only else ""))
    return f


def c_nve(path, field, grace_h, extra_days=0, ref="main"):
    def f(now):
        d = load(ref, path)
        if isinstance(d, Exception):
            return dict(ok=False, latest="unreadable", note=str(d)[:80])
        vals = [parse_ts(v).date() for v in jp(d, field)]
        if not vals:
            return dict(ok=False, latest="none", note=f"no {field}")
        exp = nve_expected_sunday(now, grace_h) - dt.timedelta(days=extra_days)
        return dict(ok=min(vals) >= exp, latest=str(min(vals)), note=f"NVE Sunday required: {exp} (published Wed 13:00 Oslo + {grace_h} h)")
    return f


def c_tomorrow(now):
    d = load("main", "nordic-v2/tomorrow.json")
    if isinstance(d, Exception):
        return dict(ok=False, latest="unreadable", note=str(d)[:80])
    dd = parse_ts(d["delivery_date"]).date()
    z = now.astimezone(ZH)
    need = z.date() + dt.timedelta(days=1) if z.hour >= 18 else z.date()
    return dict(ok=dd >= need, latest=f"delivery {dd}", note=f"auction ~12:42 CET; delivery day {need} required at this hour")


def c_se_pdf(now):
    d = load("live-data", "data/power.json")
    if isinstance(d, Exception):
        return dict(ok=False, latest="unreadable", note=str(d)[:80])
    h = (d.get("hydro") or {}).get("se") or {}
    end = sv_period_end(h.get("period"), h.get("year") or now.year)
    if not end:
        return dict(ok=False, latest="none", note="no Energiforetagen week in power.json hydro.se")
    lag = (now.astimezone(ZH).date() - end).days
    return dict(ok=lag <= 16, latest=f"week {h.get('week')} to {end}", note=f"{lag} days after week end, allowed 16 (weekly PDF trails the week)")


def c_errors(ref, path, max_errors=0):
    def f(now):
        d = load(ref, path)
        if isinstance(d, Exception):
            return dict(ok=False, latest="unreadable", note=str(d)[:80])
        found = scan_errors(d)
        n = sum(c for _, c, _ in found)
        note = "; ".join(f"{p}: {c} (e.g. {m})" for p, c, m in found[:3]) or "no errors in file"
        return dict(ok=n <= max_errors, latest=f"{n} in-file errors", note=note)
    return f


def c_static(now):
    return dict(ok=True, latest="baseline/static", note="not a feed; checked for readability only")


def c_exists(ref, path):
    def f(now):
        d = load(ref, path)
        return dict(ok=not isinstance(d, Exception), latest="readable" if not isinstance(d, Exception) else "unreadable", note="")
    return f


# ------------------------------------------------------------------ registry: dataset -> (builder, cron, checks)
M, L = "main", "live-data"
UD = ("update-data.yml", "scripts/*.py via Update market data")
PW = "power/full runs 12:30 and 17:30 UTC daily + full builds"
FU = "full builds (weekday morning catch-up + ~3 a day)"
REG = []


def ds(id, ref, path, builder, cron, checks, weekly=False):
    REG.append(dict(id=id, ref=ref, path=path, builder=builder, cron=cron, checks=checks))


ds("nordic-v2/prices.json", M, "nordic-v2/prices.json", "nordic-prices.yml (build.py --only prices)", "0 3,4 + 30 11,12,13 daily", [("observation", c_date(M, "nordic-v2/prices.json", "as_of", 2)), ("errors", c_errors(M, "nordic-v2/prices.json"))])
ds("nordic-v2/tomorrow.json", M, "nordic-v2/tomorrow.json", "nordic-prices.yml", "same", [("delivery", c_tomorrow)])
ds("nordic-v2/generation.json", M, "nordic-v2/generation.json", "nordic-prices.yml", "same", [("observation", c_date(M, "nordic-v2/generation.json", "as_of", 2))])
ds("nordic-v2/prices_normals.json", M, "nordic-v2/prices_normals.json", "nordic-prices.yml", "same", [("static", c_static)])
ds("nordic-v2/weather.json", M, "nordic-v2/weather.json", "nordic-weather.yml", "15 6, 15 18 daily", [("observation", c_date(M, "nordic-v2/weather.json", "as_of", 8))])
ds("nordic-v2/monthly_climate.json", M, "nordic-v2/monthly_climate.json", "nordic-weather.yml", "same", [("observation", c_date(M, "nordic-v2/monthly_climate.json", "as_of", 8))])
ds("nordic-v2/weather_normals.json", M, "nordic-v2/weather_normals.json", "nordic-weather.yml", "same", [("static", c_static)])
ds("nordic-v2/weather_grid.json", M, "nordic-v2/weather_grid.json", "nordic-weather.yml", "same", [("static", c_static)])
ds("nordic-v2/prices_de.json", M, "nordic-v2/prices_de.json", "nordic-de-prices.yml (build_de_prices.py)", "50 12,14 daily", [("observation", c_date(M, "nordic-v2/prices_de.json", "as_of", 1))])
ds("nordic-v2/hydrology.json (NO)", M, "nordic-v2/hydrology.json", "nordic-hydrology.yml (build.py --only hydrology,monthly)", "30 12,14 Wed + 30 6 Thu", [("NO observation vs NVE release", c_nve("nordic-v2/hydrology.json", "reservoirs/NO*/as_of", 23))])
ds("nordic-v2/hydrology.json (SE, FI)", M, "nordic-v2/hydrology.json", "same (ENTSO-E A72, trails about 10 days)", "same", [("SE/FI observation", c_date(M, "nordic-v2/hydrology.json", "reservoirs/SE/as_of", 21)), ("FI observation", c_date(M, "nordic-v2/hydrology.json", "reservoirs/FI/as_of", 21))])
ds("nordic-v2/monthly.json", M, "nordic-v2/monthly.json", "nordic-hydrology.yml", "same", [("as_of", c_date(M, "nordic-v2/monthly.json", "as_of", 10)), ("MTD snapshot", c_gen(M, "nordic-v2/monthly.json", "snapshots/nordic_mtd/generated_utc", 60))])
ds("nordic-climate/ (daily_grid_mean, mtd)", M, "nordic-climate/mtd.json", "nordic-climate-publish.yml (run_pipeline.py)", "05:05 Europe/Zurich Sat + 11th", [("built", c_gen(M, "nordic-climate/mtd.json", "generated", 9 * 24)), ("newest ERA5 day", c_date(M, "nordic-climate/mtd.json", "months/*/last_day", 13, agg=max))])

ds("power.json (spot, UMM, SvK, hydro)", L, "data/power.json", UD[1], PW, [("built", c_gen(L, "data/power.json", "generated_utc", 26)), ("in-file errors", c_errors(L, "data/power.json"))])
ds("power.json EEX settlements", L, "data/power.json", "scrape repo (EEX csv) -> build_power.py", "scrape ~19:00 UTC daily", [("settlement date", c_date(L, "data/power.json", "eex/asof", 2, "biz"))])
ds("power.json Energiforetagen SE reservoirs", L, "data/power.json", "build_power.py (weekly PDF)", "weekly PDF", [("week", c_se_pdf)])
ds("nordic_dash.json", L, "data/nordic_dash.json", UD[1], PW, [("built", c_gen(L, "data/nordic_dash.json", "generated_utc", 26)), ("in-file errors", c_errors(L, "data/nordic_dash.json"))])
ds("nordic_mtd.json", L, "data/nordic_mtd.json", UD[1], PW, [("built", c_gen(L, "data/nordic_mtd.json", "generated_utc", 26)), ("in-file errors", c_errors(L, "data/nordic_mtd.json"))])
ds("nordic_drivers.json", L, "data/nordic_drivers.json", UD[1], PW, [("built", c_gen(L, "data/nordic_drivers.json", "generated_utc", 26)), ("price date", c_date(L, "data/nordic_drivers.json", "prices/date", 2)), ("in-file errors", c_errors(L, "data/nordic_drivers.json"))])
ds("mr.json", L, "data/mr.json", UD[1], PW, [("built", c_gen(L, "data/mr.json", "generated_utc", 26)), ("in-file errors", c_errors(L, "data/mr.json"))])
ds("fi_wind.json", L, "data/fi_wind.json", UD[1], PW, [("built", c_gen(L, "data/fi_wind.json", "generated_utc", 26))])
ds("gas.json (GIE AGSI+)", L, "data/gas.json", UD[1], PW, [("built", c_gen(L, "data/gas.json", "generated_utc", 26)), ("gas day", c_date(L, "data/gas.json", "areas/EU/last", 4)), ("in-file errors", c_errors(L, "data/gas.json"))])
ds("nordic_precip.json", L, "data/nordic_precip.json", UD[1], PW, [("forecast built", c_gen(L, "data/nordic_precip.json", "generated_utc", 26)), ("monthly part", c_gen(L, "data/nordic_precip.json", "monthly/generated_utc", 24 * 40))])
ds("ttf_weather.json", L, "data/ttf_weather.json", "ttf-weather.yml", "40 8,10,20,22 daily", [("built", c_gen(L, "data/ttf_weather.json", "generated_utc", 26))])
ds("balancing.json (DE)", L, "data/balancing.json", UD[1], PW, [("built", c_gen(L, "data/balancing.json", "generated_utc", 26)), ("in-file errors", c_errors(L, "data/balancing.json"))])
ds("hydro_nordic.json", L, "data/hydro_nordic.json", "update-data 'Nordic hydro reservoirs' (weekly, full mode)", "weekly, refreshed Thursday morning", [("NO zones vs NVE release", c_nve("data/hydro_nordic.json", "zones/*/date", 47, ref=L))])
ds("nordic_monthly.json", L, "data/nordic_monthly.json", "update-data 'Nordic monthly overview' (monthly)", "monthly", [("built", c_gen(L, "data/nordic_monthly.json", "generated_utc", 24 * 40)), ("in-file errors", c_errors(L, "data/nordic_monthly.json"))])
ds("griddc.json", L, "data/griddc.json", "update-data 'Grid & DC' (full)", FU, [("built", c_gen(L, "data/griddc.json", "generated_utc", 60, True))])
ds("market.json", L, "data/market.json", "intraday.yml / update-data", "every 15 min weekdays", [("built", c_gen(L, "data/market.json", "generated_utc", 30, True)), ("in-file errors", c_errors(L, "data/market.json", 5))])
ds("news.json / dealflow.json / companies.json", L, "data/news.json", "update-data (full)", FU, [("news", c_gen(L, "data/news.json", "generated_utc", 36, True)), ("dealflow", c_gen(L, "data/dealflow.json", "generated_utc", 36, True)), ("companies", c_gen(L, "data/companies.json", "generated_utc", 36, True))])
ds("us_valuation.json", L, "data/us_valuation.json", "update-data (monthly)", "monthly", [("built", c_gen(L, "data/us_valuation.json", "generated_utc", 24 * 45))])
ds("metals.json / cot.json", L, "data/metals.json", "metals.yml", "weekly-ish, see workflow", [("metals", c_gen(L, "data/metals.json", "generated_utc", 24 * 8)), ("cot", c_gen(L, "data/cot.json", "generated_utc", 24 * 8))])
ds("curves/ (EEX Nordic system, TTF)", M, "curves/index.json", "scrape repo + ho-curve.yml archive", "weekdays 20:07 UTC", [("nordic-sys session", c_date(M, "curves/index.json", "curves/nordic-sys/latest", 2, "biz")), ("ttf session", c_date(M, "curves/index.json", "curves/ttf/latest", 2, "biz")), ("brent/ho session", c_date(M, "curves/index.json", "curves/brent/latest", 3, "biz"))])
ds("futures/ (EUA, TTF, HH, oil, metals, ags)", M, "futures/index.json", "futures.yml", "40 21 weekdays + 20 6 Sat", [("built", c_gen(M, "futures/index.json", "built_utc", 60, True)), ("newest EUA/TTF session", c_date(M, "futures/index.json", "by_sym/CL=F/asof", 3, "biz"))])

# workflows: file, max hours between STARTED SCHEDULED runs (weekend hours excluded when weekday_only), first expected start
WORKFLOWS = [
    ("update-data.yml", 26, False, None), ("nordic-prices.yml", 30, False, None), ("nordic-weather.yml", 30, False, None),
    ("ttf-weather.yml", 30, False, None), ("nordic-de-prices.yml", 30, False, "2026-11-26T14:50:00+00:00"),
    ("nordic-hydrology.yml", 24 * 8.5, False, "2026-12-02T14:30:00+00:00"), ("futures.yml", 36, True, None),
    ("ho-curve.yml", 36, True, None), ("data-health.yml", 9, False, "2026-11-26T12:00:00+00:00"), ("metals.yml", 24 * 8, False, None), ("ticker.yml", 14, True, None),
    ("intraday.yml", 12, True, None), ("nordic-climate-publish.yml", 24 * 9, False, "2026-11-28T05:05:00+00:00"),
]


def workflow_findings(now, simulate):
    out = []
    for wf, max_h, wk, first_due in WORKFLOWS:
        try:
            runs = json.loads(sh("gh", "api", f"repos/{REPO}/actions/workflows/{wf}/runs?per_page=40"))["workflow_runs"]
        except Exception as e:
            out.append(dict(id=f"workflow {wf}", check="runs", ok=False, latest="unreadable", note=str(e)[:100], kind="missing run"))
            continue
        sched = [r for r in runs if r["event"] == "schedule"]
        last = parse_ts(sched[0]["created_at"]) if sched else None
        ref_t = last
        if ref_t is None and first_due:
            ref_t = parse_ts(first_due)   # nothing scheduled yet: judged from the first expected start
        if first_due and parse_ts(first_due) > now and not sched:
            out.append(dict(id=f"workflow {wf}", check="schedule", ok=True, latest="first run due " + first_due[:16], note="not yet due", kind="missing run"))
        elif ref_t is None:
            out.append(dict(id=f"workflow {wf}", check="schedule", ok=False, latest="never", note="no scheduled run in the last 40 runs", kind="missing run"))
        else:
            a = age_h(ref_t, now, wk)
            lim = max_h if last else max_h
            out.append(dict(id=f"workflow {wf}", check="scheduled run started", ok=a <= lim, latest=ref_t.strftime("%Y-%m-%d %H:%MZ") if last else "none yet", note=f"{a:.0f} h since last scheduled start, allowed {lim:.0f} h" + (" (weekends excluded)" if wk else ""), kind="missing run"))
        done = [r for r in runs if r["status"] == "completed"]
        if done and not simulate:
            bad = 0
            for r in done:
                if r["conclusion"] == "failure":
                    bad += 1
                else:
                    break
            out.append(dict(id=f"workflow {wf}", check="latest run", ok=bad == 0, latest=f"{done[0]['conclusion']} {done[0]['created_at'][:16]}Z", note=f"{bad} consecutive failed run(s)" if bad else "last completed run succeeded", kind="failed workflow"))
    # steps that failed behind continue-on-error in the latest completed update-data builds
    try:
        runs = [r for r in json.loads(sh("gh", "api", f"repos/{REPO}/actions/workflows/update-data.yml/runs?per_page=6&status=completed"))["workflow_runs"] if r["conclusion"] == "success"][:3]
        bad = []
        for r in runs:
            for j in json.loads(sh("gh", "api", f"repos/{REPO}/actions/runs/{r['id']}/jobs?per_page=10"))["jobs"]:
                bad += [f"{s['name']} (run {r['id']})" for s in j.get("steps", []) if s.get("conclusion") == "failure"]
        out.append(dict(id="update-data hidden step failures", check="continue-on-error steps", ok=not bad, latest=f"{len(bad)} failed steps in last 3 green builds", note="; ".join(bad[:3]) or "none", kind="partial failure"))
    except Exception as e:
        out.append(dict(id="update-data hidden step failures", check="continue-on-error steps", ok=False, latest="unreadable", note=str(e)[:100], kind="partial failure"))
    return out


def manifest_findings():
    m = load(M, "nordic-v2/manifest.json")
    if isinstance(m, Exception):
        return [dict(id="nordic-v2/manifest.json", check="per-file entries", ok=False, latest="unreadable", note=str(m)[:80], kind="manifest outdated")]
    bad = []
    for name, e in m.get("files", {}).items():
        p = f"nordic-v2/{name}"
        if not os.path.exists(p):
            bad.append(f"{name} missing")
            continue
        raw = open(p, "rb").read()
        if hashlib.sha256(raw).hexdigest() != e.get("sha256"):
            try:
                a = json.loads(raw).get("as_of")
            except Exception:
                a = None
            bad.append(f"{name} as_of {e.get('as_of')} vs file {a}")
    return [dict(id="nordic-v2/manifest.json", check="per-file entries", ok=not bad, latest=f"{len(bad)} entries out of date", note="; ".join(bad[:4]) or "all entries match the files", kind="manifest outdated")]


def sync_manifest():
    """Refresh bytes/sha256/as_of of the per-file entries from the files themselves (additive; run on main only)."""
    p = "nordic-v2/manifest.json"
    m = json.load(open(p))
    changed = []
    for f in sorted(os.listdir("nordic-v2")):
        if not f.endswith(".json") or f == "manifest.json":
            continue
        raw = open(f"nordic-v2/{f}", "rb").read()
        e = dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), as_of=json.loads(raw).get("as_of"))
        if m["files"].get(f) != e:
            changed.append(f)
            m["files"][f] = e
    if changed:
        m["files_synced_at"] = dt.datetime.now(UTC).isoformat(timespec="seconds")
        json.dump(m, open(p, "w"), indent=1)
    return changed


# ------------------------------------------------------------------ evaluation, reporting, alerting
def evaluate(now, simulate):
    rows, alerts = [], []
    for r in REG:
        res = []
        for name, fn in r["checks"]:
            x = fn(now)
            x["check"] = name
            res.append(x)
        bad = [x for x in res if not x["ok"]]
        kind = "stale" if bad and any(x["check"] != "in-file errors" for x in bad) else "partial failure" if bad else "fresh"
        if r["checks"][0][1] is c_static:
            kind = "static"
        try:
            lc = sh("git", "log", "-1", "--format=%cI", "--", r["path"]).strip() if r["ref"] == "main" else "n/a (live-data is one force-pushed commit)"
        except Exception:
            lc = "n/a"
        rows.append(dict(dataset=r["id"], verdict=kind, latest="; ".join(f"{x['check']}: {x['latest']}" for x in res), builder=r["builder"], cron=r["cron"], last_commit=lc,
                         checks=[dict(check=x["check"], ok=x["ok"], latest=x["latest"], note=x["note"]) for x in res]))
        for x in bad:
            alerts.append(dict(id=f"{r['id']} / {x['check']}", kind="partial failure" if x["check"] == "in-file errors" else "stale observation" if "observation" in x["check"] or x["check"] in ("SE/FI observation", "FI observation", "settlement date", "week", "gas day", "price date", "delivery", "newest ERA5 day") or "session" in x["check"] else "stale build", latest=x["latest"], note=x["note"]))
    wf = workflow_findings(now, simulate)
    for x in wf + manifest_findings():
        if not x["ok"]:
            alerts.append(dict(id=f"{x['id']} / {x['check']}", kind=x["kind"], latest=x["latest"], note=x["note"]))
        rows.append(dict(dataset=x["id"], verdict="ok" if x["ok"] else x["kind"], latest=f"{x['check']}: {x['latest']}", builder="", cron="", last_commit="", checks=[dict(check=x["check"], ok=x["ok"], latest=x["latest"], note=x["note"])]))
    covered = set()
    for r in REG:
        covered.add(r["path"].split("/")[-1])
    return rows, alerts


def gh_issue(alerts, now, simulate, run_url):
    prefix = "[TEST] " if simulate else ""
    title = prefix + ISSUE_TITLE
    fp = hashlib.sha256("|".join(sorted(a["id"] for a in alerts)).encode()).hexdigest()[:12]
    found = json.loads(sh("gh", "issue", "list", "-R", REPO, "--state", "open", "--search", f'"{title}" in:title', "--json", "number,title,body", "-L", "20"))
    mine = [i for i in found if i["title"] == title]
    # a normal run clears any leftover test issue
    if not simulate:
        for i in found:
            if i["title"].startswith("[TEST] "):
                sh("gh", "issue", "close", str(i["number"]), "-R", REPO, "--comment", "Test alert cleared by the next normal health run.")
    if not alerts:
        for i in mine:
            sh("gh", "issue", "close", str(i["number"]), "-R", REPO, "--comment", f"All checks clear at {now.strftime('%Y-%m-%d %H:%MZ')} ({run_url}).")
        return False, "clear"
    rows = "\n".join(f"| {a['kind']} | {a['id']} | {a['latest']} | {a['note']} |" for a in alerts)
    body = (f"@MasterKano {len(alerts)} data alert(s) at {now.strftime('%Y-%m-%d %H:%MZ')}.\n\n| Kind | Dataset / check | Latest | Detail |\n|---|---|---|---|\n{rows}\n\n"
            f"Run: {run_url}\n\nThresholds follow each dataset's publication cadence and accepted lag (scripts/data_health.py). Closes itself when clear.\n"
            f"<!-- fp:{fp} ts:{now.isoformat(timespec='seconds')} -->")
    if mine:
        i = mine[0]
        m = re.search(r"fp:(\w+) ts:(\S+)", i["body"] or "")
        changed = not m or m.group(1) != fp
        remind = bool(m) and (now - parse_ts(m.group(2))).total_seconds() > 24 * H
        if changed or remind:
            sh("gh", "issue", "edit", str(i["number"]), "-R", REPO, "--body", body)
            sh("gh", "issue", "comment", str(i["number"]), "-R", REPO, "--body", ("Alert set changed. " if changed else "Still unresolved after 24 h. ") + f"Now {len(alerts)} alert(s). {run_url}")
            return True, "updated"
        return False, "unchanged"
    sh("gh", "issue", "create", "-R", REPO, "--title", title, "--body", body, "--assignee", "MasterKano")
    return True, "opened"


def publish_status(status):
    d = tempfile.mkdtemp()
    open(f"{d}/status.json", "w").write(json.dumps(status, indent=1))
    url = f"https://x-access-token:{os.environ['GH_TOKEN']}@github.com/{REPO}.git"
    for cmd in (["init", "-q", "-b", "data-health"], ["config", "user.name", "github-actions[bot]"], ["config", "user.email", "41898333+github-actions[bot]@users.noreply.github.com"],
                ["add", "status.json"], ["commit", "-q", "-m", f"data health {status['generated_utc']}"], ["push", "-q", "-f", url, "data-health"]):
        sh("git", "-C", d, *cmd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate-days", type=float, default=0)
    ap.add_argument("--no-alert", action="store_true", help="report only; no issue, no status branch, no manifest sync")
    a = ap.parse_args()
    simulate = a.simulate_days > 0
    now = dt.datetime.now(UTC) + dt.timedelta(days=a.simulate_days)
    if not a.no_alert and not simulate and os.path.exists("nordic-v2/manifest.json"):
        done = sync_manifest()
        if done:
            sh("git", "add", "nordic-v2/manifest.json")
            sh("git", "-c", "user.name=github-actions[bot]", "-c", "user.email=41898333+github-actions[bot]@users.noreply.github.com", "commit", "-q", "-m", f"manifest.json per-file entries synced ({', '.join(done)})")
            for i in range(6):
                if subprocess.run(["git", "push", "-q", "origin", "HEAD:main"]).returncode == 0:
                    break
                sh("git", "fetch", "-q", "origin", "main")
                sh("git", "reset", "-q", "--hard", "origin/main")
                _cache.clear()
                sync_manifest()
                sh("git", "add", "nordic-v2/manifest.json")
                sh("git", "-c", "user.name=github-actions[bot]", "-c", "user.email=41898333+github-actions[bot]@users.noreply.github.com", "commit", "-q", "-m", "manifest.json per-file entries synced")
        _cache.clear()
    rows, alerts = evaluate(now, simulate)
    run_url = f"https://github.com/{REPO}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '0')}"
    status = dict(generated_utc=now.isoformat(timespec="seconds"), simulated_days=a.simulate_days, alert_count=len(alerts), alerts=alerts, datasets=rows)
    sm = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [f"### EMMI data health: {len(alerts)} alert(s)", "", "| Dataset | Verdict | Latest |", "|---|---|---|"] + [f"| {r['dataset']} | {r['verdict']} | {r['latest'][:140]} |" for r in rows]
    if alerts:
        lines += ["", "**Alerts**"] + [f"- {x['kind']}: {x['id']} ({x['latest']}; {x['note']})" for x in alerts]
    print("\n".join(lines))
    if sm:
        open(sm, "a").write("\n".join(lines) + "\n")
    if a.no_alert:
        return 0
    if not simulate and os.environ.get("GH_TOKEN"):
        publish_status(status)
    notify, what = gh_issue(alerts, now, simulate, run_url)
    print(f"issue: {what}")
    return 1 if notify else 0


if __name__ == "__main__":
    sys.exit(main())

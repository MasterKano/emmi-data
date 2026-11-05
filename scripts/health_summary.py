"""End-of-build health summary (no network). Reads step outcomes (STEPS_JSON from the workflow) and the 'gaps'/'errors'
lists inside data files, writes a GitHub job summary and ::warning:: annotations, and exits 0. If anything failed it
writes ALERT=1 to GITHUB_OUTPUT so the workflow can open/update one issue per day."""
import json, os, subprocess
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
steps = json.loads(os.environ.get("STEPS_JSON") or "{}")
mode = os.environ.get("MODE", "?")
bad = [f"step '{k}' {v.get('outcome')}" for k, v in steps.items() if v.get("outcome") in ("failure", "cancelled")]
changed = {l[3:].strip() for l in subprocess.run(["git", "status", "--porcelain", "-uall", "--", "data"], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()}
gaps = []
for f in sorted(os.listdir(os.path.join(ROOT, "data"))):
    if not f.endswith(".json"):
        continue
    try:
        d = json.load(open(os.path.join(ROOT, "data", f)))
    except Exception as e:  # noqa: BLE001
        bad.append(f"{f} unreadable: {str(e)[:80]}"); continue
    if not isinstance(d, dict) or ("data/" + f) not in changed:
        continue
    g = d.get("gaps") or d.get("errors") or []
    if isinstance(g, dict):
        g = [f"{k}: {v}" for k, v in g.items()]
    for x in g[:5]:
        gaps.append(f"{f}: {str(x)[:140]}")
EXPECT = {"power": ["data/power.json", "data/nordic_dash.json", "data/mr.json"], "market": ["data/market.json"], "full": ["data/market.json", "data/power.json", "data/news.json"]}
for p in EXPECT.get(mode, []):
    if p not in changed:
        bad.append(f"{p} not updated by this {mode} build")
# size budgets (README "Size budgets"): initial load = what Home fetches on open; everything else is on demand
KB = 1024
INITIAL = ["data/market.json", "data/power.json", "data/dealflow.json", "data/griddc.json", "data/gas.json", "data/balancing.json",
           "data/griddc_events.json", "data/hydro_nordic.json", "data/fi_wind.json", "data/mastr_de.json", "data/powered_land_deals.json"]
INITIAL_MAX = 600 * KB
def budget(rel):
    if rel == "data/symbols.json":
        return 1024 * KB
    if rel.startswith("data/co/"):
        return 30 * KB
    if rel.startswith("data/gas/"):
        return 60 * KB
    return 300 * KB
sizes = {}
for dp, _, fs in os.walk(os.path.join(ROOT, "data")):
    for f in fs:
        if f.endswith(".json"):
            p = os.path.join(dp, f); sizes[os.path.relpath(p, ROOT).replace(os.sep, "/")] = os.path.getsize(p)
init = sum(sizes.get(p, 0) for p in INITIAL)
for rel, n in sorted(sizes.items()):
    if n > budget(rel):
        gaps.append(f"size: {rel} {n // KB} KB over its {budget(rel) // KB} KB budget")
if init > INITIAL_MAX:
    gaps.append(f"size: initial-load data {init // KB} KB over the {INITIAL_MAX // KB} KB budget")
lines = [f"## Build health ({mode})", "", f"Initial-load data {init // KB} KB (budget {INITIAL_MAX // KB} KB) · all data files {sum(sizes.values()) // KB} KB", "", f"**Failures:** {len(bad)} · **Source gaps reported in data:** {len(gaps)}", ""]
lines += [f"- ❌ {b}" for b in bad] + [f"- ⚠️ {g}" for g in gaps] + ([] if bad or gaps else ["- ✅ all steps ok, no gaps"])
open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a").write("\n".join(lines) + "\n")
for b in bad:
    print(f"::warning title=Build step problem::{b}")
for g in gaps:
    print(f"::notice title=Source gap::{g}")
with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as o:
    o.write(f"alert={'1' if bad else '0'}\n")
open(os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "health.md"), "w").write("\n".join(lines) + "\n")
print("\n".join(lines))

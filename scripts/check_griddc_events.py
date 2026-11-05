"""Weekly check of the hand-maintained Grid & DC events calendar (data/griddc_events.json).
Sets status upcoming/past from each item's date (a month-only date counts as past once the month has ended)
and lists items that turned past since the last check under "flags", so they can be reviewed and replaced.
Each event may carry "v" (YYYY-MM-DD): the date it was last verified by hand on its official page; without it the
site shows the file-level "checked" date. "auto_checked" is the dataset build date of this weekly run. No network access. Skips when the last check is less than 6.5 days old (pass --force to run anyway)."""
import calendar, datetime as dt, json, os, sys

P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "griddc_events.json")


def end(d):
    if len(d) == 7:
        y, m = map(int, d.split("-"))
        return dt.date(y, m, calendar.monthrange(y, m)[1])
    return dt.date.fromisoformat(d)


def main():
    j = json.load(open(P, encoding="utf-8"))
    today = dt.date.today()
    last = j.get("auto_checked")
    if last and (today - dt.date.fromisoformat(last)).days < 6.5 and "--force" not in sys.argv:
        print("griddc_events: checked", last, "- weekly check skipped"); return
    flags = []
    for e in j["events"]:
        st = "past" if end(e["d"]) < today else "upcoming"
        if e.get("status") == "upcoming" and st == "past":
            flags.append(f'{e["d"]} {e["c"]}: {e["t"]}')
        e["status"] = st
    j["auto_checked"] = today.isoformat()
    j["flags"] = flags
    j["upcoming"] = sum(e["status"] == "upcoming" for e in j["events"])
    json.dump(j, open(P, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"griddc_events: {j['upcoming']} upcoming, {len(flags)} newly past" + (": " + "; ".join(flags) if flags else ""))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Fetch Germany–Luxembourg (DE-LU) day-ahead prices into nordic-v2/prices_de.json.

Targeted ENTSO-E A44 pull only. Does not expand Nordic COUNTRIES and does not
rebuild weather, generation or hydrology. Period-weighted mean of complete
Europe/Zurich delivery days. Used by the EMMI area table; the site loads this
file optionally and leaves DE as not-available if it is missing.
"""
from __future__ import annotations
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import entsoe_client as ec
import pandas as pd

OUT = Path(__file__).resolve().parent / "prices_de.json"
CET = ZoneInfo("Europe/Zurich")
EIC = ec.ENTSOE_EIC["DE-LU"]


def daily_series(items):
    h = ec._combine(items)
    if h is None or not len(h):
        return pd.Series(dtype=float)
    diffs = h.index.to_series().diff().dropna().dt.total_seconds()
    res = int(diffs.mode().iloc[0])
    grouped = h.tz_convert(ec.CET).groupby(h.index.tz_convert(ec.CET).strftime("%Y-%m-%d"))
    vals = {}
    for ds, g in grouped:
        day = dt.date.fromisoformat(ds)
        a, b = ec._cet_bounds(day, day + dt.timedelta(days=1))
        need = int((b - a).total_seconds() / res)
        if int(g.notna().sum()) == need:
            vals[pd.Timestamp(ds)] = float(g.mean())
    return pd.Series(vals, dtype=float)


def fetch_range(start: dt.date, end: dt.date) -> pd.Series:
    """Inclusive start, exclusive end, month-sized windows."""
    parts = []
    cur = start
    while cur < end:
        nxt = min(end, (dt.date(cur.year + 1, 1, 1) if cur.month == 12 else dt.date(cur.year, cur.month + 1, 1)))
        params = dict(documentType="A44", in_Domain=EIC, out_Domain=EIC, **ec._period(cur, nxt))
        items = ec.entsoe_get(params)
        by = {}
        for it in items:
            by.setdefault(it["step"], []).append(it)
        for group in by.values():
            s = daily_series(group)
            if len(s):
                parts.append(s)
        cur = nxt
    if not parts:
        return pd.Series(dtype=float)
    daily = pd.concat(parts).sort_index()
    return daily[~daily.index.duplicated(keep="last")]


def main():
    today = dt.datetime.now(CET).date()
    # Previous MTD needs the prior month; keep a little extra headroom.
    start = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    start = start - dt.timedelta(days=5)
    end = today + dt.timedelta(days=3)
    daily = fetch_range(start, end)
    rows = [[str(pd.Timestamp(t).date()), round(float(v), 3)] for t, v in daily.items()]
    if not rows:
        raise SystemExit("No complete DE-LU delivery days returned")
    payload = {
        "schema_version": 1,
        "as_of": rows[-1][0],
        "timezone": "Europe/Zurich",
        "units": "EUR/MWh",
        "columns": ["date", "value"],
        "source": "ENTSO-E Transparency Platform, document A44, bidding zone DE-LU (10Y1001A1001A82H)",
        "definition": "Germany–Luxembourg bidding zone day-ahead. Period-weighted mean of every complete Europe/Zurich delivery day. Not a Nordic area.",
        "fetched_at": dt.datetime.now(CET).isoformat(timespec="seconds"),
        "regions": {
            "DE": {
                "as_of": rows[-1][0],
                "label": "DE",
                "definition": "Germany–Luxembourg bidding zone day-ahead, period mean of complete Europe/Zurich delivery days.",
                "source": "ENTSO-E A44, DE-LU 10Y1001A1001A82H",
                "rows": rows,
            }
        },
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    print(f"wrote {OUT} {rows[0][0]}..{rows[-1][0]} ({len(rows)} days)")


if __name__ == "__main__":
    main()

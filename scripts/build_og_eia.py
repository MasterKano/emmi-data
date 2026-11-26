#!/usr/bin/env python3
"""Refresh the EIA parts of the oil and mining bundle: og-mining-v2/spot_daily.json (Brent, WTI, Henry Hub daily spot) and
og-mining-v2/us_weekly.json (US weekly petroleum stocks, production, products supplied, Lower-48 gas storage), plus their entries in
og-mining-v2/manifest.json. Source: public EIA dnav .xls files, no key. The calculations are those of build_eia_weekly.py in
emmi-v2-og-mining (12 Nov 2026), unchanged: weekly = rolling 1 year + 5-year average and range for the same ISO week (prior 5 full
years); daily = rolling 2 months + 30-day moving average. Nothing else in the bundle is touched.

Idempotent: a file is rewritten only when its content (ignoring the 'built' date) changes, so a run with no new EIA data changes nothing.
  --raw DIR   read eia_<series>.xls from DIR instead of downloading (tests)
  --out DIR   write somewhere else than og-mining-v2/ (tests)"""
import argparse, datetime as dt, hashlib, json, os, sys, time
import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = {"RBRTEd": "pet", "RWTCd": "pet", "RNGWHHDd": "ng", "WCESTUS1w": "pet", "WCRSTUS1w": "pet", "WGTSTUS1w": "pet", "WDISTUS1w": "pet",
       "WCRFPUS2w": "pet", "WRPUPUS2w": "pet", "NW2_EPG0_SWO_R48_BCFw": "ng"}
WEEKLY = {"WCESTUS1w": ("us_commercial_crude_stocks", "US commercial crude stocks excl. SPR", "million bbl", 1 / 1000),
          "WCRSTUS1w": ("us_total_crude_stocks_incl_spr", "US crude stocks incl. SPR", "million bbl", 1 / 1000),
          "WGTSTUS1w": ("us_gasoline_stocks", "US total gasoline stocks", "million bbl", 1 / 1000),
          "WDISTUS1w": ("us_distillate_stocks", "US distillate stocks", "million bbl", 1 / 1000),
          "WCRFPUS2w": ("us_crude_production", "US crude oil field production", "kb/d", 1),
          "WRPUPUS2w": ("us_product_supplied", "US total products supplied (demand proxy)", "kb/d", 1),
          "NW2_EPG0_SWO_R48_BCFw": ("us_gas_storage_l48", "US Lower-48 working gas in storage", "Bcf", 1)}
DAILY = {"RBRTEd": ("brent_spot", "Europe Brent spot FOB", "USD/bbl"), "RWTCd": ("wti_spot", "WTI Cushing spot", "USD/bbl"),
         "RNGWHHDd": ("henry_hub_spot", "Henry Hub natural gas spot", "USD/MMBtu")}


def fetch(series, raw):
    path = os.path.join(raw, f"eia_{series}.xls")
    if os.path.exists(path) and not getattr(fetch, "download", False):
        return path
    u = f"https://www.eia.gov/dnav/{URL[series]}/hist_xls/{series}.xls"
    for i in range(4):
        try:
            r = requests.get(u, headers={"User-Agent": "Mozilla/5.0 (EMMI data refresh)"}, timeout=60)
            r.raise_for_status()
            if not r.content.startswith(b"\xd0\xcf\x11\xe0"):
                raise ValueError("not an xls file")
            open(path, "wb").write(r.content)
            return path
        except Exception as e:  # noqa: BLE001
            if i == 3:
                raise RuntimeError(f"{series}: {e}") from e
            time.sleep(5 * (i + 1))


def load(series, raw):
    x = pd.read_excel(fetch(series, raw), sheet_name="Data 1", header=2)
    x.columns = ["date", "value"] + list(x.columns[2:])
    x = x[["date", "value"]].dropna()
    x["date"] = pd.to_datetime(x.date)
    return x.sort_values("date").reset_index(drop=True)


def build(raw, today):
    out = dict(meta=dict(source="US EIA Weekly Petroleum Status Report / Weekly Natural Gas Storage Report via dnav .xls", url="https://www.eia.gov/dnav/",
                         built=today, normal="5-year average and min/max for the same ISO week, prior 5 full years"))
    for f, (k, label, unit, mult) in WEEKLY.items():
        x = load(f, raw)
        x["value"] = x.value * mult
        x["wk"] = x.date.dt.isocalendar().week.astype(int)
        x["yr"] = x.date.dt.year
        last = x.iloc[-1]
        y = int(last.yr)
        norm = x[(x.yr >= y - 5) & (x.yr <= y - 1)].groupby("wk").value.agg(["mean", "min", "max"])
        one = x[x.date > last.date - pd.Timedelta(days=365)]
        n = norm.loc[int(last.wk)] if int(last.wk) in norm.index else None
        out[k] = dict(label=label, unit=unit, as_of=last.date.date().isoformat(), latest=round(float(last.value), 2),
                      wow=round(float(last.value - x.iloc[-2].value), 2), yoy=round(float(last.value - x[x.date <= last.date - pd.Timedelta(days=364)].iloc[-1].value), 2),
                      normal_5y=None if n is None else round(float(n["mean"]), 2),
                      vs_normal_pct=None if n is None else round((last.value / n["mean"] - 1) * 100, 1),
                      range_5y=None if n is None else [round(float(n["min"]), 2), round(float(n["max"]), 2)],
                      series_1y=[[d.date().isoformat(), round(float(v), 2), round(float(norm.loc[w, "mean"]), 2) if w in norm.index else None] for d, v, w in zip(one.date, one.value, one.wk)])
    dd = dict(meta=dict(source="US EIA spot prices (Thomson Reuters via EIA), dnav .xls", url="https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm",
                        window="rolling 2 months of daily values + 30-day moving average; EIA spot data lag ~1 week"))
    for f, (k, label, unit) in DAILY.items():
        x = load(f, raw)
        x["ma30"] = x.value.rolling(30, min_periods=20).mean()
        last = x.iloc[-1]
        w = x[x.date > last.date - pd.Timedelta(days=62)]
        mm = x.assign(ym=x.date.dt.to_period("M")).groupby("ym").value.mean()
        cur, prev = mm.iloc[-1], mm.iloc[-2]
        hist = x[(x.date.dt.year >= last.date.year - 5) & (x.date.dt.year <= last.date.year - 1)]
        dd[k] = dict(label=label, unit=unit, as_of=last.date.date().isoformat(), latest=round(float(last.value), 2), ma30=round(float(last.ma30), 2),
                     mtd_avg=round(float(cur), 2), prev_month_avg=round(float(prev), 2), mtd_vs_prev_pct=round((cur / prev - 1) * 100, 1),
                     avg_5y=round(float(hist.value.mean()), 2), vs_5y_pct=round((last.value / hist.value.mean() - 1) * 100, 1),
                     series_2m=[[d.date().isoformat(), round(float(v), 2), round(float(a), 2)] for d, v, a in zip(w.date, w.value, w.ma30)])
    return dd, out


def same(a, b):
    def strip(d):
        d = json.loads(json.dumps(d))
        d.get("meta", {}).pop("built", None)
        return d
    return strip(a) == strip(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw")
    ap.add_argument("--out", default=os.path.join(ROOT, "og-mining-v2"))
    a = ap.parse_args()
    raw = a.raw
    if not raw:
        import tempfile
        raw = tempfile.mkdtemp()
        fetch.download = True
    spot, weekly = build(raw, dt.date.today().isoformat())
    man_p = os.path.join(a.out, "manifest.json")
    man = json.load(open(man_p))
    changed = []
    for name, new in (("spot_daily.json", spot), ("us_weekly.json", weekly)):
        p = os.path.join(a.out, name)
        old = json.load(open(p)) if os.path.exists(p) else None
        asof = max(v["as_of"] for k, v in new.items() if isinstance(v, dict) and "as_of" in v)
        if old is not None and same(old, new):
            print(f"{name}: unchanged (as_of {asof})")
            continue
        json.dump(new, open(p, "w"), indent=1)
        b = open(p, "rb").read()
        man["files"][name] = dict(bytes=len(b), sha256=hashlib.sha256(b).hexdigest(), as_of=asof)
        changed.append(name)
        print(f"{name}: updated, as_of {asof}")
    if changed:
        man["eia_refreshed_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        json.dump(man, open(man_p, "w"), separators=(",", ":"))
    print("changed:", ", ".join(changed) or "none")


if __name__ == "__main__":
    sys.exit(main())

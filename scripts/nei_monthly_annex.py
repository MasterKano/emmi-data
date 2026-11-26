#!/usr/bin/env python3
"""NEI Monthly Nordic power-market annex.

Reusable, month-parameterised draft for Jerry's Margin Report. The script
reads stored EMMI series, calculates every figure, chart and CSV, and does
not fetch sources, write site files, change schedules, or alter methodology.

    python scripts/nei_monthly_annex.py --month 2026-10 --out /path/to/review

Weather normals stay the documented Nordic v2 baseline, 2006–2025
(nordic-v2/weather.json and monthly_climate.json). The 1991–2020 climate
workbook is not read.

EEX Nordic zone contracts in master/eex_master.csv are outright base futures
(product Base, short codes such as FBBM / 1NBM / 1DBM). A zone outright is the
settlement itself. SYS is never added to it. The spread, when both settlements
exist, is zone minus SYS for the same delivery and the same trade date.
SYS + EPAD is used only if a row is labelled as an EPAD.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import io
import json
import math
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

ZURICH = ZoneInfo("Europe/Zurich")  # same delivery-day clock as scripts/build_power.py CET
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ZONES = ["SYS", "NO1", "NO2", "NO3", "NO4", "NO5", "SE1", "SE2", "SE3", "SE4", "FI", "DK1", "DK2"]
COUNTRIES = ["NO", "SE", "FI", "DK"]
FUELS = ["hydro", "nuclear", "wind", "solar", "thermal"]
# Countries that actually have the fuel in the existing Nordic aggregate.
APPLICABLE = {
    "hydro": ["NO", "SE", "FI"],
    "nuclear": ["SE", "FI"],
    "wind": ["NO", "SE", "FI", "DK"],
    "solar": ["NO", "SE", "FI", "DK"],
    "thermal": ["NO", "SE", "FI", "DK"],
}
EMBER_AREA = {"NO": "Norway", "SE": "Sweden", "FI": "Finland", "DK": "Denmark"}
WEATHER_METRICS = ("wind100", "temp", "rain")
FORWARD_AREAS = ["Nordic", "NO1", "NO2", "NO3", "NO4", "NO5", "SE1", "SE2", "SE3", "SE4", "FI", "DK1", "DK2", "DE"]
AREA_ZONE = {"Nordic": "SYS", "DE": "DE"}


def repo_root():
    return Path(__file__).resolve().parent.parent


def round_half_up(value, places):
    """Same half-away-from-zero rounding as nordic-v2/price_monthlies.py."""
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def parse_month(text):
    year, month = text.split("-")
    year, month = int(year), int(month)
    if month < 1 or month > 12:
        raise SystemExit(f"bad month {text}")
    return year, month


def month_end(year, month):
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def month_days(year, month):
    return [dt.date(year, month, day) for day in range(1, calendar.monthrange(year, month)[1] + 1)]


def _last_sunday(year, month):
    last = month_end(year, month)
    return last - dt.timedelta(days=(last.weekday() + 1) % 7)


def day_hours(day):
    """Europe/Zurich delivery-day length. Same rule as scripts/build_mr.py hours().

    The EU clock change is the last Sunday in March (23 hours) and the last
    Sunday in October (25 hours). It is written out so a stale zoneinfo file
    cannot flatten those days to 24 hours.
    """
    if day == _last_sunday(day.year, 3):
        return 23
    if day == _last_sunday(day.year, 10):
        return 25
    return 24


def month_hours(year, month):
    return sum(day_hours(day) for day in month_days(year, month))


def hour_weighted(pairs):
    """Time-weighted mean. Any missing value makes the result missing, never zero."""
    if not pairs:
        return None
    if any(value is None or hours in (None, 0) for value, hours in pairs):
        return None
    den = sum(hours for _, hours in pairs)
    if not den:
        return None
    return sum(value * hours for value, hours in pairs) / den


def tenor_label(year, month):
    return f"{MONTHS[month - 1]}-{year % 100:02d}"


def tenor_start(delivery):
    """'Nov-26' -> date, 'Q1-27' -> date, 'Cal-27' -> date. Mirrors build_power.tenor_start."""
    import re
    text = delivery.split()[-1] if delivery else ""
    match = re.match(r"^(Cal|Q[1-4]|Winter|Summer|[A-Z][a-z]{2})-(\d{2})$", text)
    if not match:
        return None, None
    kind, yy = match.group(1), 2000 + int(match.group(2))
    if kind == "Cal":
        return dt.date(yy, 1, 1), "Y"
    if kind[0] == "Q" and kind[1:].isdigit():
        return dt.date(yy, 3 * (int(kind[1]) - 1) + 1, 1), "Q"
    if kind == "Winter":
        return dt.date(yy, 10, 1), "S"
    if kind == "Summer":
        return dt.date(yy, 4, 1), "S"
    return dt.date(yy, MONTHS.index(kind) + 1, 1), "M"


def quarter_hours(year, quarter):
    start_month = 3 * (quarter - 1) + 1
    return sum(month_hours(year, start_month + i) for i in range(3))


def remainder_months(asof):
    """Calendar months in asof.year whose delivery starts after asof."""
    out = []
    for month in range(1, 13):
        start = dt.date(asof.year, month, 1)
        if start > asof:
            out.append((tenor_label(asof.year, month), month_hours(asof.year, month)))
    return out


def parse_number(text):
    if text is None:
        return None
    raw = str(text).strip().replace(",", "")
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def instrument_kind(row):
    """Label in the scrape, not a guess from the price level.

    Product Base + commodity POWER is the outright the EEX master file stores
    (Nordic FBBM, zone *NBM/*SBM/*DBM). EPAD is used only when the row says so.
    """
    blob = " ".join(
        str(row.get(key) or "") for key in ("product", "shortCode", "delivery", "commodity", "area", "marketGroup")
    ).upper()
    if "EPAD" in blob:
        return "epad"
    if row.get("marketGroup") == "POWER" and row.get("product") == "Base" and row.get("commodity") == "POWER":
        return "outright"
    return "other"


def quote_price(kind, settlement, sys_settlement):
    """Outright stays the outright. SYS is added only to a labelled EPAD."""
    if settlement is None:
        return None, "missing"
    if kind == "outright":
        return settlement, "direct_settlement"
    if kind == "epad":
        if sys_settlement is None:
            return None, "epad_without_sys"
        return settlement + sys_settlement, "sys_plus_epad"
    return None, "unclassified"


def implied_spread(zone_outright, sys_outright):
    if zone_outright is None or sys_outright is None:
        return None
    return zone_outright - sys_outright


def delivered_month_check(settlement, spot, sys_spot):
    """The month contract on the month's last session should match the spot month if it is an outright."""
    if settlement is None or spot is None:
        return "unchecked"
    if abs(settlement - spot) <= 1.0:
        return "matches_spot_outright"
    spread = None if sys_spot is None else spot - sys_spot
    if spread is not None and abs(settlement - spot) > 5 and abs(settlement - spread) <= 1.0:
        return "looks_like_epad"
    return "unconfirmed"


def liquidity_flag(volume, open_interest):
    flags = []
    if volume is None:
        flags.append("volume_blank")
    if open_interest == 0:
        flags.append("zero_open_interest")
    elif open_interest is None:
        flags.append("open_interest_blank")
    return "|".join(flags) if flags else "quoted"


def last_session(dates, year, month):
    start = dt.date(year, month, 1)
    end = month_end(year, month)
    inside = [day for day in dates if start <= day <= end]
    return max(inside) if inside else None


def covers_month(start, end, year, month):
    if start is None or end is None:
        return False
    if start > dt.date(year, month, 1):
        return False
    return end >= month_end(year, month)


def load_json(path):
    with open(path) as handle:
        return json.load(handle)


def require_baseline(doc, path, expected=(2006, 2025)):
    got = tuple(doc.get("baseline") or ())
    if got != tuple(expected):
        raise SystemExit(f"{path} baseline is {got}; this annex requires {expected[0]}–{expected[1]} and will not switch baselines")


def _price_module():
    folder = repo_root() / "nordic-v2"
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    import price_monthlies as pm
    return pm


def found_from_prices(doc):
    """Stored Nord Pool monthlies in prices_normals.json. No network.

    A month whose last calendar day is after history_end is partial (the file
    was built mid-month). Those values stay out of final YTD totals.
    """
    history_end = dt.date.fromisoformat(str(doc["history_end"])[:10])
    found = {}
    for i, key in enumerate(doc["months"]):
        year, month = int(key[:4]), int(key[5:])
        end = month_end(year, month)
        found[key] = {
            "start": dt.date(year, month, 1),
            "end": end if end <= history_end else history_end,
            "values": {code: series[i] for code, series in doc["monthly"].items() if code not in ("SE", "NO", "DK")},
        }
    return found


def spot_table(found, year, month):
    rows = []
    prev = (year, month - 1) if month > 1 else (year - 1, 12)

    def value(y, m, zone):
        key = f"{y:04d}-{m:02d}"
        block = found.get(key)
        if not block:
            return None, False, None, None
        code = "Nordic" if zone == "SYS" else zone
        return block["values"].get(code), covers_month(block["start"], block["end"], y, m), block["start"], block["end"]

    for zone in ZONES:
        cur, cur_ok, cur_start, cur_end = value(year, month, zone)
        prv, prv_ok, _, _ = value(*prev, zone)
        ly, ly_ok, _, _ = value(year - 1, month, zone)
        ytd_pairs, ytd_ok = [], True
        prior_pairs, prior_ok = [], True
        for m in range(1, month + 1):
            v, ok, _, _ = value(year, m, zone)
            ytd_pairs.append((v, month_hours(year, m)))
            ytd_ok = ytd_ok and ok and v is not None
            pv, pok, _, _ = value(year - 1, m, zone)
            prior_pairs.append((pv, month_hours(year - 1, m)))
            prior_ok = prior_ok and pok and pv is not None
        sys_cur, _, _, _ = value(year, month, "SYS")
        status = "final" if cur_ok else ("provisional" if cur is not None else "missing")
        rows.append({
            "zone": zone,
            "month": f"{year:04d}-{month:02d}",
            "eur_mwh": None if not cur_ok else cur,
            "published_eur_mwh": cur,
            "status": status,
            "delivery_start": None if cur_start is None else cur_start.isoformat(),
            "delivery_end": None if cur_end is None else cur_end.isoformat(),
            "previous_month": f"{prev[0]:04d}-{prev[1]:02d}",
            "previous_eur_mwh": None if not prv_ok else prv,
            "previous_status": "final" if prv_ok else ("provisional" if prv is not None else "missing"),
            "change_vs_previous": None if not (cur_ok and prv_ok) else cur - prv,
            "ly_month": f"{year - 1:04d}-{month:02d}",
            "ly_eur_mwh": None if not ly_ok else ly,
            "ly_status": "final" if ly_ok else ("provisional" if ly is not None else "missing"),
            "change_vs_ly": None if not (cur_ok and ly_ok) else cur - ly,
            "ytd": f"{year:04d}-01..{year:04d}-{month:02d}",
            "ytd_eur_mwh": hour_weighted(ytd_pairs) if ytd_ok else None,
            "ytd_status": "final" if ytd_ok else "missing",
            "prior_ytd": f"{year - 1:04d}-01..{year - 1:04d}-{month:02d}",
            "prior_ytd_eur_mwh": hour_weighted(prior_pairs) if prior_ok else None,
            "prior_ytd_status": "final" if prior_ok else "missing",
            "change_ytd": None if not (ytd_ok and prior_ok) else hour_weighted(ytd_pairs) - hour_weighted(prior_pairs),
            "spread_vs_sys": None if zone == "SYS" or not cur_ok or sys_cur is None else cur - sys_cur,
            "unit": "EUR/MWh",
            "source": "Nord Pool AggregatePrices multiAreaMonthlyAggregates (Monthly Aggregate); SYS is the system price",
            "weighting": "Month is Nord Pool's period mean. YTD is the delivery-hour-weighted mean of those complete months.",
            "cutoff": None if cur_end is None else cur_end.isoformat(),
        })
    return rows


def _rows_in_month(rows, year, month, date_idx=0):
    prefix = f"{year:04d}-{month:02d}"
    return [row for row in rows if str(row[date_idx]).startswith(prefix)]


def weather_table(weather, year, month):
    require_baseline(weather, "weather.json")
    days_in_month = calendar.monthrange(year, month)[1]
    out = []
    for region, block in weather["regions"].items():
        for metric in WEATHER_METRICS:
            series = block["metrics"][metric]["rows"]
            picked = _rows_in_month(series, year, month)
            values = [row[1] for row in picked]
            normals = [row[3] for row in picked]
            complete = len(picked) == days_in_month and all(v is not None for v in values) and all(n is not None for n in normals)
            value = sum(values) / len(values) if complete else None
            normal = sum(normals) / len(normals) if complete else None
            diff = None if value is None or normal is None else value - normal
            pct = None
            if diff is not None and metric != "temp" and normal not in (None, 0):
                pct = diff / normal * 100
            out.append({
                "region": region,
                "metric": metric,
                "month": f"{year:04d}-{month:02d}",
                "value": value,
                "normal": normal,
                "difference": diff,
                "difference_pct": pct,
                "month_total": sum(values) if complete and metric == "rain" else None,
                "days_covered": sum(v is not None for v in values),
                "days_in_month": days_in_month,
                "status": "final" if complete else "missing",
                "unit": {"wind100": "m/s", "temp": "degC", "rain": "mm/day"}[metric],
                "total_unit": "mm" if metric == "rain" else "",
                "baseline": "2006-2025",
                "normal_definition": "Unweighted mean of the same UTC calendar days' 2006–2025 normals. Temperature anomaly is degrees C, not a percent.",
                "source": weather.get("source"),
                "method": weather.get("method"),
                "cutoff": block["metrics"][metric].get("as_of"),
                "geography": "Country and Nordic land-area weights in weather.json. Bidding-zone weather is not in this dataset.",
            })
    return out


def _index(months, key):
    try:
        return months.index(key)
    except ValueError:
        return None


def _daily_map(rows):
    return {row[0]: row[1] for row in rows}


def integrate_month(daily, year, month):
    """Mean MW over a complete Zurich delivery month -> TWh and average GW. A missing day stays missing."""
    present = _daily_map(daily)
    missing = 0
    energy_mwh = 0.0
    hours = 0
    for day in month_days(year, month):
        value = present.get(day.isoformat())
        if value is None:
            missing += 1
            continue
        h = day_hours(day)
        energy_mwh += float(value) * h
        hours += h
    if missing or not hours:
        return {"twh": None, "avg_gw": None, "hours": month_hours(year, month), "missing_days": missing, "status": "missing" if missing == len(month_days(year, month)) else "incomplete"}
    return {
        "twh": energy_mwh / 1e6,
        "avg_gw": (energy_mwh / hours) / 1000.0,
        "hours": hours,
        "missing_days": 0,
        "status": "final",
    }


def integrate_load_factor(daily, year, month):
    present = _daily_map(daily)
    missing = 0
    acc = 0.0
    hours = 0
    for day in month_days(year, month):
        value = present.get(day.isoformat())
        if value is None:
            missing += 1
            continue
        h = day_hours(day)
        acc += float(value) * h
        hours += h
    if missing or not hours:
        return None, missing
    return acc / hours, 0


def wind_energy_from_lf(lf_rows, cap_rows, year, month):
    lf = _daily_map(lf_rows)
    cap = {row[0]: row[1] for row in cap_rows}
    missing = 0
    energy_mwh = 0.0
    for day in month_days(year, month):
        key = day.isoformat()
        factor, capacity = lf.get(key), cap.get(key)
        if factor is None or capacity is None:
            missing += 1
            continue
        energy_mwh += (float(factor) / 100.0) * float(capacity) * day_hours(day)
    if missing:
        return None, missing
    return energy_mwh / 1e6, 0


def snap(gen, months, country, fuel, year, month):
    idx = _index(months, f"{year:04d}-{month:02d}")
    if idx is None:
        return None
    return gen[country][fuel][idx]


def incomplete_years(generation, country, metric, month, year_lo, year_hi):
    """Years whose published daily series does not cover this calendar month."""
    metrics = generation["regions"].get(country, {}).get("metrics", {})
    if metric not in metrics:
        return set()
    daily = _daily_map(metrics[metric]["rows"])
    bad = set()
    for year in range(year_lo, year_hi + 1):
        days = month_days(year, month)
        if not any(day.isoformat() in daily for day in days):
            continue  # outside the published daily window; the monthly snapshot is the record
        if any(daily.get(day.isoformat()) is None for day in days):
            bad.add(year)
    return bad


def mean_present(values):
    present = [v for v in values if v is not None]
    if not present:
        return None, 0
    return sum(present) / len(present), len(present)


def generation_tables(generation, monthly_snapshot, year, month):
    snap_doc = monthly_snapshot["snapshots"]["nordic_monthly"]
    months = snap_doc["months"]
    gen = snap_doc["gen"]
    y0, y1 = snap_doc.get("avg_years") or [2021, 2025]
    gen_rows = []
    nuclear_rows = []
    hours = month_hours(year, month)
    for fuel in FUELS:
        country_rows = []
        for country in COUNTRIES:
            applicable = country in APPLICABLE[fuel]
            base = {
                "country": country, "fuel": fuel, "month": f"{year:04d}-{month:02d}",
                "hours": hours, "unit_energy": "TWh", "unit_power": "GW",
                "applicable": applicable,
            }
            if not applicable:
                base.update(twh=None, avg_gw=None, status="not_applicable", source="no fleet in the existing Nordic aggregate",
                            cutoff=generation.get("as_of"), snapshot_twh=None, normal_twh=None, normal_n_years=0,
                            previous_twh=None, ly_twh=None, ytd_twh=None, prior_ytd_twh=None, load_factor_pct=None)
                country_rows.append(base)
                continue
            snapshot = snap(gen, months, country, fuel, year, month)
            prev_m = month - 1 if month > 1 else 12
            prev_y = year if month > 1 else year - 1
            previous = snap(gen, months, country, fuel, prev_y, prev_m)
            ly = snap(gen, months, country, fuel, year - 1, month)
            if fuel in ("hydro", "nuclear"):
                metric = fuel
                integrated = integrate_month(generation["regions"][country]["metrics"][metric]["rows"], year, month)
                primary = integrated["twh"]
                avg = integrated["avg_gw"]
                status = integrated["status"]
                source = "ENTSO-E A75 actual generation, daily mean MW in nordic-v2/generation.json, times Europe/Zurich delivery hours. Actual production, not availability."
                cutoff = generation["regions"][country]["metrics"][metric].get("as_of")
                ytd_vals, prior_vals = [], []
                ytd_status, prior_status = "final", "final"
                for m in range(1, month + 1):
                    cur_i = integrate_month(generation["regions"][country]["metrics"][metric]["rows"], year, m)
                    old_i = integrate_month(generation["regions"][country]["metrics"][metric]["rows"], year - 1, m)
                    ytd_vals.append(cur_i["twh"])
                    prior_vals.append(old_i["twh"])
                    if cur_i["status"] != "final":
                        ytd_status = "incomplete"
                    if old_i["status"] != "final":
                        prior_status = "incomplete"
                ytd = sum(ytd_vals) if ytd_status == "final" else None
                prior_ytd = sum(prior_vals) if prior_status == "final" else None
                ytd_hours = sum(month_hours(year, m) for m in range(1, month + 1))
                prior_hours = sum(month_hours(year - 1, m) for m in range(1, month + 1))
                base.update(
                    twh=primary, avg_gw=avg, status=status, source=source, cutoff=cutoff, snapshot_twh=snapshot,
                    missing_days=integrated["missing_days"],
                    ytd_twh=ytd, ytd_avg_gw=None if ytd is None else ytd * 1000 / ytd_hours,
                    ytd_status=ytd_status,
                    prior_ytd_twh=prior_ytd, prior_ytd_avg_gw=None if prior_ytd is None else prior_ytd * 1000 / prior_hours,
                    prior_ytd_status=prior_status,
                )
            elif fuel == "wind":
                lf, lf_missing = integrate_load_factor(generation["regions"][country]["metrics"]["wind_load_factor"]["rows"], year, month)
                cap_rows = (generation.get("capacity_daily") or {}).get(country, {}).get("rows") or []
                cross, cross_missing = wind_energy_from_lf(
                    generation["regions"][country]["metrics"]["wind_load_factor"]["rows"], cap_rows, year, month)
                primary = snapshot
                status = "final" if primary is not None else "missing"
                source = "Monthly TWh from nordic-v2/monthly.json nordic_monthly snapshot (ENTSO-E A75; DK wind from August 2026 is Energinet settlement). Load factor is the hour-weighted mean of daily capacity-adjusted load factors in generation.json."
                cutoff = snap_doc.get("generated_utc")
                base.update(twh=primary, avg_gw=None if primary is None else primary * 1000 / hours, status=status,
                            source=source, cutoff=cutoff, snapshot_twh=snapshot, load_factor_pct=lf,
                            load_factor_missing_days=lf_missing, lf_capacity_twh=cross, lf_capacity_missing_days=cross_missing)
                ytd = _ytd_snapshot(gen, months, country, fuel, year, month)
                prior = _ytd_snapshot(gen, months, country, fuel, year - 1, month)
                base.update(ytd_twh=ytd[0], ytd_status=ytd[1], prior_ytd_twh=prior[0], prior_ytd_status=prior[1],
                            ytd_avg_gw=None if ytd[0] is None else ytd[0] * 1000 / sum(month_hours(year, m) for m in range(1, month + 1)),
                            prior_ytd_avg_gw=None if prior[0] is None else prior[0] * 1000 / sum(month_hours(year - 1, m) for m in range(1, month + 1)))
            else:
                primary = snapshot
                status = "final" if primary is not None else "missing"
                if primary == 0:
                    status = "rounded_below_0.01_TWh"
                source = "nordic-v2/monthly.json nordic_monthly snapshot, ENTSO-E A75 monthly TWh rounded to 0.01. Solar/thermal are not in generation.json's daily metrics."
                cutoff = snap_doc.get("generated_utc")
                base.update(twh=primary, avg_gw=None if primary is None else primary * 1000 / hours, status=status,
                            source=source, cutoff=cutoff, snapshot_twh=snapshot, load_factor_pct=None)
                ytd = _ytd_snapshot(gen, months, country, fuel, year, month)
                prior = _ytd_snapshot(gen, months, country, fuel, year - 1, month)
                base.update(ytd_twh=ytd[0], ytd_status=ytd[1], prior_ytd_twh=prior[0], prior_ytd_status=prior[1],
                            ytd_avg_gw=None if ytd[0] is None else ytd[0] * 1000 / sum(month_hours(year, m) for m in range(1, month + 1)),
                            prior_ytd_avg_gw=None if prior[0] is None else prior[0] * 1000 / sum(month_hours(year - 1, m) for m in range(1, month + 1)))
            exclude = set()
            if fuel in ("hydro", "wind", "nuclear"):
                metric = "wind_load_factor" if fuel == "wind" else fuel
                exclude = incomplete_years(generation, country, metric, month, y0, y1)
            normal_vals = []
            for y in range(y0, y1 + 1):
                if y in exclude or y == year:
                    continue
                normal_vals.append(snap(gen, months, country, fuel, y, month))
            normal, n_years = mean_present(normal_vals)
            base.update(previous_twh=previous, ly_twh=ly, normal_twh=normal, normal_n_years=n_years,
                        normal_years=f"{y0}-{y1}", normal_excluded=";".join(str(y) for y in sorted(exclude)))
            country_rows.append(base)
        # Nordic total from the rows just built. A missing applicable country makes the total missing.
        pieces = [row for row in country_rows if row["applicable"]]
        total = None
        total_status = "final"
        if any(row["twh"] is None for row in pieces):
            total_status = "missing"
        else:
            total = sum(row["twh"] for row in pieces)
        prev_bits = [row["previous_twh"] for row in pieces]
        ly_bits = [row["ly_twh"] for row in pieces]
        ytd_bits = [row["ytd_twh"] for row in pieces]
        prior_bits = [row["prior_ytd_twh"] for row in pieces]
        normal_bits = [row["normal_twh"] for row in pieces]
        nordic = {
            "country": "Nordic", "fuel": fuel, "month": f"{year:04d}-{month:02d}", "hours": hours,
            "unit_energy": "TWh", "unit_power": "GW", "applicable": True,
            "twh": total, "avg_gw": None if total is None else total * 1000 / hours, "status": total_status,
            "source": "Sum of applicable countries in this table. Countries without that fleet are excluded, not entered as zero.",
            "cutoff": snap_doc.get("generated_utc"),
            "snapshot_twh": None if any(snap(gen, months, c, fuel, year, month) is None for c in APPLICABLE[fuel]) else sum(snap(gen, months, c, fuel, year, month) or 0 for c in APPLICABLE[fuel]),
            "previous_twh": None if any(v is None for v in prev_bits) else sum(prev_bits),
            "ly_twh": None if any(v is None for v in ly_bits) else sum(ly_bits),
            "ytd_twh": None if any(v is None for v in ytd_bits) else sum(ytd_bits),
            "prior_ytd_twh": None if any(v is None for v in prior_bits) else sum(prior_bits),
            "ytd_status": "final" if all(v is not None for v in ytd_bits) else "incomplete",
            "prior_ytd_status": "final" if all(v is not None for v in prior_bits) else "incomplete",
            "normal_twh": None if any(v is None for v in normal_bits) else sum(normal_bits),
            "normal_n_years": min((row["normal_n_years"] for row in pieces), default=0),
            "normal_years": f"{y0}-{y1}",
            "load_factor_pct": None,
        }
        if fuel == "wind":
            lf, missing = integrate_load_factor(generation["regions"]["Nordic"]["metrics"]["wind_load_factor"]["rows"], year, month)
            nordic["load_factor_pct"] = lf
            nordic["load_factor_missing_days"] = missing
        gen_rows.extend(country_rows)
        gen_rows.append(nordic)
        if fuel == "nuclear":
            nuclear_rows.extend(row for row in country_rows + [nordic])
    nuclear = nuclear_detail(generation, nuclear_rows, year, month)
    return gen_rows, nuclear


def _ytd_snapshot(gen, months, country, fuel, year, month):
    vals = [snap(gen, months, country, fuel, year, m) for m in range(1, month + 1)]
    if any(v is None for v in vals):
        return None, "incomplete"
    return sum(vals), "final"


def nuclear_detail(generation, rows, year, month):
    """Capacity utilisation uses installed A68 capacity. UMM availability is not production."""
    out = []
    hours = month_hours(year, month)
    ytd_hours = sum(month_hours(year, m) for m in range(1, month + 1))
    prior_hours = sum(month_hours(year - 1, m) for m in range(1, month + 1))
    caps = generation.get("capacity") or {}
    for row in rows:
        if row["country"] not in ("SE", "FI"):
            continue
        cap, cap_note = nuclear_capacity(caps.get(row["country"]) or {}, year)
        avg_mw = None if row["twh"] is None else row["twh"] * 1e6 / hours
        util = None if avg_mw is None or not cap else avg_mw / cap * 100
        ytd_mw = None if row.get("ytd_twh") is None else row["ytd_twh"] * 1e6 / ytd_hours
        prior_mw = None if row.get("prior_ytd_twh") is None else row["prior_ytd_twh"] * 1e6 / prior_hours
        out.append({
            **row,
            "installed_mw": cap,
            "installed_note": cap_note,
            "avg_mw": avg_mw,
            "utilisation_pct": util,
            "ytd_avg_mw": ytd_mw,
            "ytd_utilisation_pct": None if ytd_mw is None or not cap else ytd_mw / cap * 100,
            "prior_ytd_avg_mw": prior_mw,
            "prior_ytd_utilisation_pct": None if prior_mw is None or not cap else prior_mw / cap * 100,
            "outages": "missing",
            "outage_note": "October unit-level outages and return dates are not in the published files. nuclear_availability is a later rolling UMM snapshot and is not October production or October availability.",
        })
    return out


def nuclear_capacity(years, year):
    current = years.get(str(year)) or {}
    if current.get("nuclear_mw"):
        return current["nuclear_mw"], f"ENTSO-E A68 {year} nuclear_mw, as_of {current.get('as_of')}"
    prior = [y for y in years if y.isdigit() and int(y) < year and (years[y] or {}).get("nuclear_mw")]
    if not prior:
        return None, "installed nuclear capacity not published"
    use = max(prior)
    rec = years[use]
    return rec["nuclear_mw"], (
        f"ENTSO-E A68 {use} nuclear_mw held because {year} country A68 is {current.get('status') or 'unpublished'}. "
        "This is installed capacity for the production ratio, not REMIT available capacity."
    )


def linear_on(points, day):
    """Linear interpolation. No extrapolation. points are (date, value)."""
    known = [(d, v) for d, v in points if v is not None]
    known.sort()
    for d, v in known:
        if d == day:
            return v, "observed"
    prev = [item for item in known if item[0] < day]
    nxt = [item for item in known if item[0] > day]
    if not prev or not nxt:
        return None, "missing"
    d0, v0 = prev[-1]
    d1, v1 = nxt[0]
    span = (d1 - d0).days
    if span <= 0:
        return None, "missing"
    weight = (day - d0).days / span
    return v0 + (v1 - v0) * weight, f"interpolated {d0.isoformat()} to {d1.isoformat()}"


def _dated(rows, date_idx=0):
    out = []
    for row in rows:
        out.append((dt.date.fromisoformat(str(row[date_idx])[:10]), row))
    return out


def hydro_tables(hydro, year, month):
    end = month_end(year, month)
    prev = month_end(year, month - 1) if month > 1 else month_end(year - 1, 12)
    reservoirs = []
    for name, block in hydro["reservoirs"].items():
        cols = block.get("columns") or []
        dated = _dated(block.get("rows") or [])
        inside = [(d, row) for d, row in dated if d.year == year and d.month == month]
        last = max(inside, key=lambda item: item[0]) if inside else None
        numeric = {}
        for i, col in enumerate(cols):
            if col == "date" or i == 0:
                continue
            points = [(d, row[i] if i < len(row) else None) for d, row in dated]
            end_v, end_how = linear_on(points, end)
            prev_v, prev_how = linear_on(points, prev)
            numeric[col] = {
                "month_end": end_v, "month_end_method": end_how,
                "prior_month_end": prev_v, "prior_method": prev_how,
                "change": None if end_v is None or prev_v is None else end_v - prev_v,
            }
        band_row = None
        if last and block.get("band"):
            week = last[0].isocalendar()[1]
            for brow in block["band"]:
                if brow and brow[0] == week:
                    band_row = brow
                    break
        band_cols = block.get("band_columns") or []
        band = {}
        if band_row:
            for i, col in enumerate(band_cols):
                band[col] = band_row[i] if i < len(band_row) else None
        level_key = "fill_pct" if "fill_pct" in numeric else ("storage_twh" if "storage_twh" in numeric else None)
        level = numeric.get(level_key) or {}
        median = band.get("median")
        reservoirs.append({
            "reservoir": name,
            "month": f"{year:04d}-{month:02d}",
            "unit": block.get("unit"),
            "last_observation": None if last is None else last[0].isoformat(),
            "last_row": None if last is None else last[1],
            "columns": cols,
            "month_end": level.get("month_end"),
            "month_end_method": level.get("month_end_method"),
            "prior_month_end": level.get("prior_month_end"),
            "change": level.get("change"),
            "level_column": level_key,
            "storage_month_end_twh": (numeric.get("storage_twh") or {}).get("month_end"),
            "storage_change_twh": (numeric.get("storage_twh") or {}).get("change"),
            "fill_month_end_pct": (numeric.get("fill_pct") or {}).get("month_end"),
            "fill_change_pct": (numeric.get("fill_pct") or {}).get("change"),
            "band_week": band.get("iso_week"),
            "band_min": band.get("min"),
            "band_median": median,
            "band_max": band.get("max"),
            "band_n_years": band.get("n_years"),
            "vs_median": None if level.get("month_end") is None or median is None else level.get("month_end") - median,
            "status": "final" if level.get("month_end") is not None else "missing",
            "source": block.get("source") or hydro.get("source"),
            "cutoff": block.get("as_of"),
            "balance": "reservoir storage only",
            "note": "Month-end is linearly interpolated between weekly observations, the same idea as scripts/build_nordic_monthly.py storage_monthend. It is not a full hydrological balance.",
        })
    inflow = []
    target = f"{year:04d}-{month:02d}"
    prev_key = f"{(month_end(year, month - 1) if month > 1 else month_end(year - 1, 12)).year:04d}-{prev.month:02d}"
    for country, block in (hydro.get("monthly_inflow") or {}).items():
        by = {row[0]: row for row in block.get("rows") or []}
        cur = by.get(target)
        old = by.get(prev_key)
        inflow.append({
            "area": country,
            "month": target,
            "inflow_twh": None if cur is None else cur[1],
            "normal_twh": None if cur is None else cur[2],
            "n_years": None if cur is None else cur[3],
            "previous_twh": None if old is None else old[1],
            "change_vs_previous": None if cur is None or old is None or cur[1] is None or old[1] is None else cur[1] - old[1],
            "vs_normal": None if cur is None or cur[1] is None or cur[2] is None else cur[1] - cur[2],
            "status": "final" if cur and cur[1] is not None else "missing",
            "unit": block.get("unit") or "TWh/month",
            "method": block.get("method"),
            "source": block.get("source"),
            "cutoff": block.get("as_of"),
            "not": "This is derived inflow (reservoir change + hydro generation). It is not the full water+snow+soil balance.",
        })
    return reservoirs, inflow, hydro.get("balance_definition"), hydro.get("omitted")


def read_eex(path):
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows


def power_quotes(rows):
    dates = set()
    book = {}
    for row in rows:
        if row.get("marketGroup") != "POWER" or row.get("status") != "ok" or row.get("product") != "Base":
            continue
        try:
            day = dt.date.fromisoformat(row["tradeDate"][:10])
        except ValueError:
            continue
        dates.add(day)
        kind = instrument_kind(row)
        price = parse_number(row.get("settlementPrice"))
        book[(day, row.get("area"), row.get("delivery"))] = {
            "kind": kind,
            "settlement": price,
            "short_code": row.get("shortCode"),
            "volume": parse_number(row.get("volume")),
            "open_interest": parse_number(row.get("grossOpenInterest")),
            "product": row.get("product"),
            "pricing": row.get("pricing"),
        }
    return dates, book


def forward_tables(book, dates, year, month, later_dates):
    current = last_session(dates, year, month)
    prev_year, prev_month = (year, month - 1) if month > 1 else (year - 1, 12)
    previous = last_session(dates, prev_year, prev_month)
    later = max(later_dates) if later_dates else None
    sessions = []
    if previous:
        sessions.append((previous, "prior_month_end"))
    if current:
        sessions.append((current, "month_end"))
    if later and later != current:
        sessions.append((later, "later_update"))
    # Tenors: delivered month, remainder of the year, next-year months through March, quarters, next two cals.
    wanted = []
    for y, m in ((year, month), (prev_year, prev_month)):
        wanted.append(tenor_label(y, m))
    for m in range(month, 13):
        wanted.append(tenor_label(year, m))
    for m in range(1, 4):
        wanted.append(tenor_label(year + 1, m))
    for q, yy in ((4, year), (1, year + 1), (2, year + 1), (3, year + 1), (4, year + 1)):
        wanted.append(f"Q{q}-{yy % 100:02d}")
    wanted.append(f"Cal-{(year + 1) % 100:02d}")
    wanted.append(f"Cal-{(year + 2) % 100:02d}")
    # preserve order, drop duplicates
    tenors = []
    for item in wanted:
        if item not in tenors:
            tenors.append(item)
    long_rows = []
    for day, role in sessions:
        sys_by_tenor = {}
        for tenor in tenors:
            sys_rec = book.get((day, "Nordic", tenor))
            sys_px, sys_how = (None, "missing")
            if sys_rec:
                sys_px, sys_how = quote_price(sys_rec["kind"], sys_rec["settlement"], None)
            sys_by_tenor[tenor] = (sys_px, sys_how, sys_rec)
        for area in FORWARD_AREAS:
            zone = AREA_ZONE.get(area, area)
            for tenor in tenors:
                rec = book.get((day, area, tenor))
                if area == "Nordic":
                    price, how = sys_by_tenor[tenor][0], sys_by_tenor[tenor][1]
                    kind = sys_by_tenor[tenor][2]["kind"] if sys_by_tenor[tenor][2] else "missing"
                    short = None if not sys_by_tenor[tenor][2] else sys_by_tenor[tenor][2]["short_code"]
                    volume = None if not sys_by_tenor[tenor][2] else sys_by_tenor[tenor][2]["volume"]
                    oi = None if not sys_by_tenor[tenor][2] else sys_by_tenor[tenor][2]["open_interest"]
                elif rec is None:
                    price, how, kind, short, volume, oi = None, "missing", "missing", None, None, None
                else:
                    price, how = quote_price(rec["kind"], rec["settlement"], sys_by_tenor[tenor][0])
                    kind, short, volume, oi = rec["kind"], rec["short_code"], rec["volume"], rec["open_interest"]
                start, typ = tenor_start(tenor)
                long_rows.append({
                    "zone": zone,
                    "source_area": area,
                    "tenor": tenor,
                    "tenor_type": typ,
                    "delivery_start": None if start is None else start.isoformat(),
                    "session_role": role,
                    "trade_date": day.isoformat(),
                    "instrument_kind": kind,
                    "construction": how,
                    "outright_eur_mwh": price,
                    "sys_eur_mwh": sys_by_tenor[tenor][0],
                    "spread_vs_sys": None if zone in ("SYS", "DE") else implied_spread(price, sys_by_tenor[tenor][0]),
                    "short_code": short,
                    "volume": volume,
                    "open_interest": oi,
                    "liquidity": liquidity_flag(volume, oi) if rec or area == "Nordic" else "missing",
                    "unit": "EUR/MWh",
                    "source": "EEX settlement via MasterKano/scrape master/eex_master.csv. Zone prices are outright base futures. Spread is zone minus SYS. DE is a continental reference, not a Nordic zone.",
                    "cutoff": day.isoformat(),
                })
    summary, remainder = summarise_forwards(long_rows, year, month, current, previous)
    return long_rows, summary, remainder, current, previous, later


def summarise_forwards(long_rows, year, month, current, previous):
    summary = []
    zones = []
    for row in long_rows:
        if row["zone"] not in zones:
            zones.append(row["zone"])
    tenors = []
    for row in long_rows:
        if row["session_role"] == "month_end" and row["tenor"] not in tenors:
            tenors.append(row["tenor"])
    by = {(row["zone"], row["tenor"], row["session_role"]): row for row in long_rows}
    for zone in zones:
        for tenor in tenors:
            cur = by.get((zone, tenor, "month_end"))
            prv = by.get((zone, tenor, "prior_month_end"))
            summary.append({
                "zone": zone,
                "tenor": tenor,
                "month_end_date": None if current is None else current.isoformat(),
                "month_end_eur_mwh": None if cur is None else cur["outright_eur_mwh"],
                "month_end_spread": None if cur is None else cur["spread_vs_sys"],
                "month_end_liquidity": None if cur is None else cur["liquidity"],
                "month_end_construction": None if cur is None else cur["construction"],
                "prior_date": None if previous is None else previous.isoformat(),
                "prior_eur_mwh": None if prv is None else prv["outright_eur_mwh"],
                "prior_spread": None if prv is None else prv["spread_vs_sys"],
                "change_eur_mwh": None if cur is None or prv is None or cur["outright_eur_mwh"] is None or prv["outright_eur_mwh"] is None else cur["outright_eur_mwh"] - prv["outright_eur_mwh"],
                "unit": "EUR/MWh",
                "source": "EEX outright base settlement. Not SYS+EPAD.",
                "cutoff": None if current is None else current.isoformat(),
            })
    remainder = []
    for role, day in (("month_end", current), ("prior_month_end", previous)):
        if day is None:
            continue
        basket = remainder_months(day)
        # Like-for-like Nov-Dec of the reporting year, so the two sessions can be compared on the same hours.
        matched = [(tenor_label(year, m), month_hours(year, m)) for m in (11, 12) if not (year == day.year and m < 1)]
        if month < 11:
            matched = [(tenor_label(year, 11), month_hours(year, 11)), (tenor_label(year, 12), month_hours(year, 12))]
        for zone in zones:
            if zone == "DE":
                continue
            for label, tenors in (("remainder_of_session_year", basket), ("nov_dec_matched", matched)):
                pairs = []
                missing = []
                for tenor, hours in tenors:
                    row = by.get((zone, tenor, role))
                    price = None if row is None else row["outright_eur_mwh"]
                    if price is None:
                        missing.append(tenor)
                    pairs.append((price, hours))
                remainder.append({
                    "zone": zone,
                    "basket": label,
                    "session_role": role,
                    "trade_date": day.isoformat(),
                    "tenors": " ".join(tenor for tenor, _ in tenors),
                    "hours": " ".join(str(hours) for _, hours in tenors),
                    "eur_mwh": hour_weighted(pairs),
                    "status": "final" if not missing else "missing",
                    "missing_tenors": " ".join(missing),
                    "unit": "EUR/MWh",
                    "weighting": "Delivery hours in Europe/Zurich. A missing tenor makes the basket missing. November–December is the like-for-like basket; the session-year remainder at September still includes October.",
                    "source": "EEX outright settlements",
                    "cutoff": day.isoformat(),
                })
    return summary, remainder


def driver_rows(rows, current, previous):
    keep_groups = {"GAS_FUTURES", "GAS_SPOT", "EUA_FUTURES", "ICE_COAL_FUTURES"}
    wanted = {
        "Gas Month Nov-26", "Gas Month Dec-26", "Gas Winter-27", "Gas Cal-27", "TTF Day-Ahead",
        "EUA Dec-26", "Coal Dec-26",
    }
    out = []
    sessions = [("prior_month_end", previous), ("month_end", current)]
    for role, day in sessions:
        if day is None:
            continue
        for row in rows:
            if row.get("tradeDate", "")[:10] != day.isoformat():
                continue
            if row.get("status") != "ok" or row.get("marketGroup") not in keep_groups:
                continue
            if row.get("delivery") not in wanted:
                continue
            price = parse_number(row.get("settlementPrice"))
            unit = "USD/t" if row.get("marketGroup") == "ICE_COAL_FUTURES" else ("EUR/t" if "EUA" in (row.get("delivery") or "") else "EUR/MWh")
            out.append({
                "session_role": role,
                "trade_date": day.isoformat(),
                "market": row.get("marketGroup"),
                "contract": row.get("delivery"),
                "price": price,
                "status": "final" if price is not None else "missing",
                "unit": unit,
                "source": "EEX / ICE settlement in master/eex_master.csv, same sessions as the power curve",
                "cutoff": day.isoformat(),
                "note": "Explanatory fuel and carbon context. Not a Nordic zone price.",
            })
    return out


def ember_demand(path, year, month):
    rows = []
    if path is None or not Path(path).exists():
        return rows, "Ember file was not downloaded"
    latest = None
    by = {code: {} for code in EMBER_AREA}
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        area_key = next((f for f in fields if f.lower() == "area"), None)
        date_key = next((f for f in fields if f.lower() == "date"), None)
        source_key = next((f for f in fields if "source" in f.lower() or f.lower() == "variable"), None)
        value_key = next((f for f in fields if "twh" in f.lower()), None)
        if not all([area_key, date_key, source_key, value_key]):
            return [], f"Ember columns not recognised: {fields}"
        name_of = {name: code for code, name in EMBER_AREA.items()}
        for rec in reader:
            if rec.get(source_key) != "Demand":
                continue
            code = name_of.get(rec.get(area_key))
            if code is None:
                continue
            ym = str(rec.get(date_key) or "")[:7]
            value = parse_number(rec.get(value_key))
            by[code][ym] = value
            if value is not None and (latest is None or ym > latest):
                latest = ym
    hours = month_hours(year, month)
    target = f"{year:04d}-{month:02d}"
    prev = f"{(year if month > 1 else year - 1):04d}-{(month - 1 if month > 1 else 12):02d}"
    ly = f"{year - 1:04d}-{month:02d}"

    def ytd(code, y):
        vals = [by[code].get(f"{y:04d}-{m:02d}") for m in range(1, month + 1)]
        if any(v is None for v in vals):
            return None
        return sum(vals)

    built = []
    for code in COUNTRIES:
        cur = by[code].get(target)
        built.append({
            "country": code,
            "month": target,
            "demand_twh": cur,
            "avg_gw": None if cur is None else cur * 1000 / hours,
            "previous_twh": by[code].get(prev),
            "ly_twh": by[code].get(ly),
            "ytd_twh": ytd(code, year),
            "prior_ytd_twh": ytd(code, year - 1),
            "status": "final" if cur is not None else "missing",
            "unit": "TWh",
            "source": "Ember monthly electricity data, Electricity source = Demand, CC BY 4.0",
            "cutoff": latest,
            "note": "Country demand. No temperature/structural split is published here; the weather table is the temperature evidence.",
        })
    if all(row["demand_twh"] is not None for row in built):
        nordic_twh = sum(row["demand_twh"] for row in built)
    else:
        nordic_twh = None
    def total(key):
        vals = [row[key] for row in built]
        if any(v is None for v in vals):
            return None
        return sum(vals)
    built.append({
        "country": "Nordic", "month": target, "demand_twh": nordic_twh,
        "avg_gw": None if nordic_twh is None else nordic_twh * 1000 / hours,
        "previous_twh": total("previous_twh"), "ly_twh": total("ly_twh"),
        "ytd_twh": total("ytd_twh"), "prior_ytd_twh": total("prior_ytd_twh"),
        "status": "final" if nordic_twh is not None else "missing",
        "unit": "TWh", "source": "Sum of NO, SE, FI and DK Ember demand. A missing country makes the total missing.",
        "cutoff": latest, "note": "",
    })
    return built, None if any(row["status"] == "final" and row["country"] != "Nordic" for row in built) or built[0]["demand_twh"] is not None else f"Ember has no {target} demand (latest {latest})"


def outright_sanity(book, spot_rows, year, month, current, previous):
    checks = []
    spot = {row["zone"]: row for row in spot_rows}
    prev_year, prev_month = (year, month - 1) if month > 1 else (year - 1, 12)
    plan = []
    if current:
        plan.append((current, tenor_label(year, month), "eur_mwh", "month_end"))
    if previous:
        plan.append((previous, tenor_label(prev_year, prev_month), "previous_eur_mwh", "prior_month_end"))
    for day, tenor, spot_key, role in plan:
        sys_rec = book.get((day, "Nordic", tenor))
        sys_spot = (spot.get("SYS") or {}).get(spot_key)
        sys_settle = None if sys_rec is None else sys_rec["settlement"]
        for zone in ZONES:
            area = "Nordic" if zone == "SYS" else zone
            rec = book.get((day, area, tenor))
            settlement = None if rec is None else rec["settlement"]
            zone_spot = (spot.get(zone) or {}).get(spot_key)
            flag = delivered_month_check(settlement, zone_spot, sys_spot if zone != "SYS" else zone_spot)
            checks.append({
                "zone": zone, "tenor": tenor, "trade_date": day.isoformat(), "session_role": role,
                "settlement": settlement, "spot": zone_spot, "check": flag,
                "kind": None if rec is None else rec["kind"],
                "note": "Passed only when the delivered-month outright is within 1 EUR/MWh of the Nord Pool month. A spread-shaped quote is not added to SYS.",
            })
    return checks


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = {}
            for field in fields:
                value = row.get(field)
                if isinstance(value, float):
                    out[field] = "" if math.isnan(value) else round_half_up(value, 6)
                elif value is None:
                    out[field] = ""
                else:
                    out[field] = value
            writer.writerow(out)


def fmt(value, places=2):
    if value is None:
        return "missing"
    return f"{round_half_up(value, places):.{places}f}"


def md_table(rows, columns):
    head = "| " + " | ".join(columns) + " |"
    rule = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in rows:
        cells = []
        for col in columns:
            value = row.get(col, "")
            if isinstance(value, float):
                value = fmt(value, 2)
            if value is None:
                value = "missing"
            cells.append(str(value).replace("|", "/").replace("\n", " "))
        body.append("| " + " | ".join(cells) + " |")
    return "\n".join([head, rule, *body])


def calculated_reading(spot, weather, generation, nuclear, reservoirs, inflow, remainder, drivers):
    lines = []
    sys = next((row for row in spot if row["zone"] == "SYS"), None)
    if sys and sys["eur_mwh"] is not None:
        lines.append(
            f"SYS {sys['month']} was {fmt(sys['eur_mwh'])} EUR/MWh"
            f" ({fmt(sys['change_vs_previous'])} versus {sys['previous_month']},"
            f" {fmt(sys['change_vs_ly'])} versus {sys['ly_month']})."
            f" Hour-weighted YTD is {fmt(sys['ytd_eur_mwh'])} against {fmt(sys['prior_ytd_eur_mwh'])} a year earlier."
        )
    spreads = [row for row in spot if row["zone"] != "SYS" and row["spread_vs_sys"] is not None]
    if spreads:
        rich = max(spreads, key=lambda row: row["spread_vs_sys"])
        cheap = min(spreads, key=lambda row: row["spread_vs_sys"])
        lines.append(
            f"Widest October premia to SYS: {rich['zone']} {fmt(rich['spread_vs_sys'])} EUR/MWh"
            f" (outright {fmt(rich['eur_mwh'])})."
            f" Widest discount: {cheap['zone']} {fmt(cheap['spread_vs_sys'])} EUR/MWh"
            f" (outright {fmt(cheap['eur_mwh'])})."
        )
    nordic_wx = [row for row in weather if row["region"] == "Nordic" and row["status"] == "final"]
    if nordic_wx:
        bits = []
        for row in nordic_wx:
            if row["metric"] == "temp":
                bits.append(f"temperature {fmt(row['difference'], 2)} degC versus the 2006–2025 normal")
            elif row["metric"] == "wind100":
                bits.append(f"100m wind {fmt(row['difference'], 2)} m/s ({fmt(row['difference_pct'], 1)}%)")
            elif row["metric"] == "rain":
                bits.append(f"rain {fmt(row['difference'], 2)} mm/day ({fmt(row['difference_pct'], 1)}%)")
        lines.append("Nordic area, " + "; ".join(bits) + ".")
    for fuel in ("hydro", "wind", "nuclear"):
        row = next((item for item in generation if item["country"] == "Nordic" and item["fuel"] == fuel), None)
        if row and row.get("twh") is not None:
            lines.append(
                f"Nordic {fuel} {fmt(row['twh'])} TWh ({fmt(row['avg_gw'])} GW average)"
                f" against a {row.get('normal_years')} same-month normal of {fmt(row.get('normal_twh'))} TWh"
                f" and {fmt(row.get('ly_twh'))} TWh a year earlier."
            )
    for row in nuclear:
        if row.get("utilisation_pct") is not None:
            lines.append(
                f"{row['country']} nuclear ran at {fmt(row['utilisation_pct'], 1)}% of {fmt(row['installed_mw'], 0)} MW installed"
                f" ({fmt(row['twh'])} TWh). Prior-year YTD utilisation {fmt(row.get('prior_ytd_utilisation_pct'), 1)}%."
            )
    no = next((row for row in reservoirs if row["reservoir"] == "NO" and row["status"] == "final"), None)
    if no and no.get("fill_month_end_pct") is not None:
        lines.append(
            f"Norway reservoir fill, interpolated to month-end, {fmt(no['fill_month_end_pct'], 1)}%"
            f" ({fmt(no['fill_change_pct'], 1)} percentage points over the month;"
            f" {fmt(no['vs_median'], 1)} points versus the week-{no.get('band_week')} median)."
        )
    nose = next((row for row in inflow if row["area"] == "NOSE"), None)
    if nose and nose.get("inflow_twh") is not None:
        lines.append(
            f"Derived NO+SE inflow {fmt(nose['inflow_twh'])} TWh versus a normal of {fmt(nose['normal_twh'])} TWh."
            " That is reservoir change plus hydro generation, not a full hydrological balance."
        )
    matched = [row for row in remainder if row["basket"] == "nov_dec_matched" and row["zone"] == "SYS"]
    by_role = {row["session_role"]: row for row in matched}
    if by_role.get("month_end") and by_role.get("prior_month_end"):
        a, b = by_role["month_end"], by_role["prior_month_end"]
        change = None if a["eur_mwh"] is None or b["eur_mwh"] is None else a["eur_mwh"] - b["eur_mwh"]
        lines.append(
            f"SYS Nov–Dec outright, hour-weighted, {fmt(a['eur_mwh'])} EUR/MWh on {a['trade_date']}"
            f" versus {fmt(b['eur_mwh'])} on {b['trade_date']} ({fmt(change)}). "
            "September's full remainder still includes October and is a different basket."
        )
    return lines


def render_markdown(ctx):
    y, m = ctx["year"], ctx["month"]
    title_month = f"{calendar.month_name[m]} {y}"
    lines = [
        f"# NEI Monthly — Nordic power annex, {title_month}",
        "",
        "Private draft for Jerry's Margin Report. Figures, charts and CSVs are calculated by `scripts/nei_monthly_annex.py`. Nothing in the tables was typed by hand.",
        "",
        f"Run: `--month {y:04d}-{m:02d}`. Weather baseline **2006–2025**. EEX zone futures are **outright** settlements; the spread is zone minus SYS. SYS is not added to a zone quote.",
        "",
        "## Corporate and investment review",
        "",
        "*Marin — placeholder.* Material October developments, transactions, financing and project milestones, as known at month-end, and the November catalysts as they stood then. Do not fold later news back into this section.",
        "",
        "## Commentary",
        "",
        "*Marin — placeholder.* Board-ready judgement on what drove prices. The calculated reading below only restates the tables.",
        "",
        "## Calculated reading",
        "",
    ]
    for line in ctx["reading"]:
        lines.append(f"- {line}")
    lines += [
        "",
        "## 1. Spot prices",
        "",
        "Nord Pool Monthly Aggregate, EUR/MWh. SYS is the system price. A month is final only when the published delivery window covers the calendar month. YTD weights those months by Europe/Zurich delivery hours.",
        "",
        md_table([{
            "Zone": row["zone"], "Oct": fmt(row["eur_mwh"]), "Sep": fmt(row["previous_eur_mwh"]),
            "vs Sep": fmt(row["change_vs_previous"]), "Oct LY": fmt(row["ly_eur_mwh"]),
            "vs LY": fmt(row["change_vs_ly"]), "YTD": fmt(row["ytd_eur_mwh"]),
            "YTD LY": fmt(row["prior_ytd_eur_mwh"]), "Spread vs SYS": "" if row["zone"] == "SYS" else fmt(row["spread_vs_sys"]),
        } for row in ctx["spot"]], ["Zone", "Oct", "Sep", "vs Sep", "Oct LY", "vs LY", "YTD", "YTD LY", "Spread vs SYS"]),
        "",
        "![October spot by zone](charts/01_spot.png)",
        "",
        "![Spread to SYS](charts/02_spreads.png)",
        "",
        "## 2. Weather",
        "",
        "ERA5 via the existing Nordic v2 geography: 100 m wind, 2 m temperature, precipitation. Normal is 2006–2025, the baseline stored in `weather.json`. This is not the 1991–2020 workbook. Bidding-zone weather is not in the dataset. Rain `value` is mm/day; the CSV also has the monthly millimetre total.",
        "",
        md_table([{
            "Region": row["region"], "Metric": row["metric"], "Value": fmt(row["value"], 2),
            "Normal": fmt(row["normal"], 2), "Anomaly": fmt(row["difference"], 2),
            "Anomaly %": "" if row["metric"] == "temp" else fmt(row["difference_pct"], 1),
            "Days": f"{row['days_covered']}/{row['days_in_month']}", "Status": row["status"],
        } for row in ctx["weather"]], ["Region", "Metric", "Value", "Normal", "Anomaly", "Anomaly %", "Days", "Status"]),
        "",
        "![Weather anomalies](charts/03_weather.png)",
        "",
        "## 3. Generation",
        "",
        "TWh and average GW. Hydro and nuclear are actual production: daily mean MW from `generation.json` times delivery hours. Wind TWh is the monthly snapshot, with Danish wind on the Energinet settlement from August 2026; the load factor is capacity-adjusted. Solar and thermal are the monthly snapshot only. A rounded 0.00 is below the snapshot's 0.01 TWh resolution, not a filled-in gap. Countries without a fleet are `not_applicable`.",
        "",
        md_table([{
            "Area": row["country"], "Fuel": row["fuel"], "TWh": fmt(row.get("twh")), "GW": fmt(row.get("avg_gw")),
            "Sep": fmt(row.get("previous_twh")), "LY": fmt(row.get("ly_twh")),
            "Normal": fmt(row.get("normal_twh")), "n": row.get("normal_n_years"),
            "LF %": fmt(row.get("load_factor_pct"), 1), "Status": row.get("status"),
        } for row in ctx["generation"] if row["country"] in ("Nordic", "NO", "SE", "FI", "DK")],
            ["Area", "Fuel", "TWh", "GW", "Sep", "LY", "Normal", "n", "LF %", "Status"]),
        "",
        "![Generation](charts/04_generation.png)",
        "",
        "### Nuclear production and utilisation",
        "",
        "Utilisation divides actual average MW by ENTSO-E A68 installed capacity. Sweden has no 2026 country A68, so the 2025 nuclear megawatts are held and labelled. This is not REMIT available capacity. October outage and return dates are missing.",
        "",
        md_table([{
            "Area": row["country"], "TWh": fmt(row["twh"]), "Avg MW": fmt(row.get("avg_mw"), 0),
            "Installed MW": fmt(row.get("installed_mw"), 0), "Utilisation %": fmt(row.get("utilisation_pct"), 1),
            "YTD TWh": fmt(row.get("ytd_twh")), "YTD util %": fmt(row.get("ytd_utilisation_pct"), 1),
            "Prior YTD TWh": fmt(row.get("prior_ytd_twh")), "Prior YTD util %": fmt(row.get("prior_ytd_utilisation_pct"), 1),
        } for row in ctx["nuclear"]], ["Area", "TWh", "Avg MW", "Installed MW", "Utilisation %", "YTD TWh", "YTD util %", "Prior YTD TWh", "Prior YTD util %"]),
        "",
        "![Nuclear](charts/05_nuclear.png)",
        "",
        ctx["nuclear_later_note"],
        "",
        "## 4. Demand",
        "",
        ctx["demand_note"],
        "",
        md_table([{
            "Area": row["country"], "TWh": fmt(row["demand_twh"]), "GW": fmt(row["avg_gw"]),
            "Sep": fmt(row["previous_twh"]), "LY": fmt(row["ly_twh"]),
            "YTD": fmt(row["ytd_twh"]), "YTD LY": fmt(row["prior_ytd_twh"]), "Status": row["status"],
        } for row in ctx["demand"]], ["Area", "TWh", "GW", "Sep", "LY", "YTD", "YTD LY", "Status"]),
        "",
        "## 5. Reservoirs and inflow",
        "",
        "Reservoir stocks are weekly observations. The month-end figure is interpolated and labelled. Derived inflow is the existing estimator (change in reservoir energy plus hydro generation). The full water, snow and soil balance is not in the pipeline and is not inferred from the reservoir deviation.",
        "",
        md_table([{
            "Reservoir": row["reservoir"], "Last print": row.get("last_observation") or "missing",
            "Fill %": fmt(row.get("fill_month_end_pct"), 1), "Storage TWh": fmt(row.get("storage_month_end_twh")),
            "Change": fmt(row.get("change"), 2), "Week": row.get("band_week") or "",
            "Median": fmt(row.get("band_median"), 2), "vs median": fmt(row.get("vs_median"), 2),
            "Status": row["status"],
        } for row in ctx["reservoirs"]], ["Reservoir", "Last print", "Fill %", "Storage TWh", "Change", "Week", "Median", "vs median", "Status"]),
        "",
        md_table([{
            "Area": row["area"], "Inflow TWh": fmt(row["inflow_twh"]), "Normal": fmt(row["normal_twh"]),
            "vs normal": fmt(row["vs_normal"]), "Sep": fmt(row["previous_twh"]), "Status": row["status"],
        } for row in ctx["inflow"]], ["Area", "Inflow TWh", "Normal", "vs normal", "Sep", "Status"]),
        "",
        "![Reservoirs](charts/06_hydro.png)",
        "",
        f"Balance definition from the hydrology file: {ctx['balance']}",
        "",
        "## 6. Cross-border, gas, coal, carbon",
        "",
        "Historical cross-border flows and transmission outages for the month were not re-queried. ENTSO-E is not called in this run (no token in the environment). Fuel and carbon settlements below are the same EEX file and the same two sessions, included because southern zones and DK cleared far above SYS.",
        "",
        md_table([{
            "Session": row["trade_date"], "Contract": row["contract"], "Price": fmt(row["price"]),
            "Unit": row["unit"], "Status": row["status"],
        } for row in ctx["drivers"]], ["Session", "Contract", "Price", "Unit", "Status"]),
        "",
        "## 7. Forwards",
        "",
        f"Month-end session {ctx['current']}. Prior month-end session {ctx['previous']}. "
        "Both are the last POWER base settlement dates in those months in `eex_master.csv`. "
        "Zone outright is the EEX base settlement (short codes FBBM, 1NBM, 4SBM, 1DBM and the same family). "
        "Spread = zone outright − SYS outright, same tenor and same date. "
        f"A later file date {ctx['later']} is in `csv/08_forwards.csv` with session_role later_update and is not in the comparison chart.",
        "",
        "The Nov–Dec column is hour-weighted and like-for-like. The session remainder at the September date still includes October, so it is not the same basket.",
        "",
        md_table([{
            "Zone": row["zone"], "Basket": row["basket"], "Session": row["trade_date"],
            "EUR/MWh": fmt(row["eur_mwh"]), "Status": row["status"], "Missing": row["missing_tenors"],
        } for row in ctx["remainder"] if row["zone"] in ("SYS", "NO1", "NO4", "SE1", "SE4", "FI", "DK1", "DK2")],
            ["Zone", "Basket", "Session", "EUR/MWh", "Status", "Missing"]),
        "",
        "![SYS forwards](charts/07_forwards.png)",
        "",
        "![Nov–Dec outright](charts/08_remainder.png)",
        "",
        "Full zone and tenor grid, including liquidity flags (zero open interest is flagged, not dropped): `csv/08_forwards.csv` and `csv/09_forwards_summary.csv`.",
        "",
        "## Coverage and gaps",
        "",
        md_table([{"Item": row["item"], "Status": row["status"], "Detail": row["detail"]} for row in ctx["coverage"]],
                 ["Item", "Status", "Detail"]),
        "",
        f"Wall time {ctx['seconds']} seconds. Stages: {ctx['stages']}.",
        "",
        "Sources and cutoffs are columns on every CSV. Workbook normal 1991–2020 was not used.",
        "",
    ]
    return "\n".join(lines)


def render_html(ctx):
    md = ctx["markdown"]
    # A readable HTML twin. Charts are the same PNGs; the CSV files are the editable data.
    parts = ["<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>NEI Monthly annex</title>",
             "<style>body{font:16px/1.45 Georgia,serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#1c1c1c}",
             "img{max-width:100%;height:auto} table{border-collapse:collapse;width:100%;font:13px ui-sans-serif,sans-serif}",
             "td,th{border-bottom:1px solid #ddd;padding:4px 6px;text-align:right} td:first-child,th:first-child{text-align:left}",
             "h1,h2{font-family:ui-sans-serif,sans-serif}</style></head><body>",
             "<pre style='white-space:pre-wrap;font:15px/1.45 Georgia,serif'>"]
    parts.append(md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    parts.append("</pre></body></html>")
    return "".join(parts)


def chart_spot(path, spot, year, month):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    labels = [row["zone"] for row in spot]
    x = range(len(labels))
    width = 0.25
    fig, ax = plt.subplots(figsize=(12.5, 5.8))
    ax.bar([i - width for i in x], [row["eur_mwh"] or 0 for row in spot], width, label=f"{year}-{month:02d}", color="#1f4e79")
    # A missing month must not be drawn as zero. Mask with nan.
    import math as _math
    def series(key):
        return [row[key] if row[key] is not None else _math.nan for row in spot]
    ax.clear()
    ax.bar([i - width for i in x], series("ly_eur_mwh"), width, label=f"{year-1}-{month:02d}", color="#b7c3ce")
    ax.bar(list(x), series("previous_eur_mwh"), width, label="previous month", color="#7f93a3")
    ax.bar([i + width for i in x], series("eur_mwh"), width, label=f"{year}-{month:02d}", color="#1f4e79")
    ax.set_xticks(list(x), labels, rotation=0)
    ax.set_ylabel("EUR/MWh")
    ax.set_title(f"Nordic day-ahead, {calendar.month_name[month]} {year}")
    ax.legend(frameon=False, ncol=3)
    ax.axhline(0, color="#888", linewidth=0.6)
    fig.text(0.01, 0.01, "Nord Pool Monthly Aggregate, EUR/MWh. SYS is the system price. Blank means missing, not zero.", fontsize=8, color="#444")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(path, dpi=160)
    fig.savefig(path.with_suffix(".svg"))
    plt.close(fig)


def chart_spreads(path, spot, year, month):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    rows = [row for row in spot if row["zone"] != "SYS"]
    labels = [row["zone"] for row in rows]
    vals = [row["spread_vs_sys"] if row["spread_vs_sys"] is not None else float("nan") for row in rows]
    colors = ["#1f4e79" if (v or 0) >= 0 else "#a33b32" for v in vals]
    fig, ax = plt.subplots(figsize=(12.5, 5.2))
    ax.bar(labels, vals, color=colors)
    ax.axhline(0, color="#333", linewidth=0.7)
    ax.set_ylabel("EUR/MWh versus SYS")
    ax.set_title(f"Zone outright minus SYS, {calendar.month_name[month]} {year}")
    fig.text(0.01, 0.01, "Spread of Nord Pool monthly prices. Not a forward EPAD.", fontsize=8, color="#444")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(path, dpi=160)
    fig.savefig(path.with_suffix(".svg"))
    plt.close(fig)


def chart_weather(path, weather):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    regions = []
    for row in weather:
        if row["region"] not in regions:
            regions.append(row["region"])
    fig, axes = plt.subplots(1, 3, figsize=(12.5, 4.6))
    titles = {"wind100": "100 m wind, m/s", "temp": "Temperature, deg C", "rain": "Rain, mm/day"}
    for ax, metric in zip(axes, WEATHER_METRICS):
        vals = []
        for region in regions:
            row = next(item for item in weather if item["region"] == region and item["metric"] == metric)
            vals.append(row["difference"] if row["difference"] is not None else float("nan"))
        ax.bar(regions, vals, color="#1f4e79")
        ax.axhline(0, color="#333", linewidth=0.6)
        ax.set_title(titles[metric])
    fig.suptitle("Anomaly versus the 2006–2025 normal")
    fig.text(0.01, 0.01, "ERA5, Nordic v2 country geography. Not 1991–2020. Temperature is degrees, not percent.", fontsize=8, color="#444")
    fig.tight_layout(rect=(0, 0.04, 1, 0.92))
    fig.savefig(path, dpi=160)
    fig.savefig(path.with_suffix(".svg"))
    plt.close(fig)


def chart_generation(path, generation, year, month):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    countries = ["NO", "SE", "FI", "DK"]
    colors = {"hydro": "#2f6f8f", "nuclear": "#c47b2b", "wind": "#3d7a4a", "solar": "#d4a017", "thermal": "#6e6259"}
    fig, ax = plt.subplots(figsize=(10, 5.4))
    import numpy as np
    x = np.arange(len(countries))
    bottom = np.zeros(len(countries))
    for fuel in FUELS:
        vals = []
        for country in countries:
            row = next(item for item in generation if item["country"] == country and item["fuel"] == fuel)
            vals.append(0.0 if row.get("twh") is None else row["twh"])
        ax.bar(countries, vals, bottom=bottom, color=colors[fuel], label=fuel)
        bottom += np.array(vals)
    ax.set_ylabel("TWh")
    ax.set_title(f"Generation, {calendar.month_name[month]} {year}")
    ax.legend(frameon=False, ncol=5)
    fig.text(0.01, 0.01, "Not-applicable fuels are omitted (drawn as no stack). Hydro/nuclear from daily actuals; wind/solar/thermal from the monthly snapshot.", fontsize=8, color="#444")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=160)
    fig.savefig(path.with_suffix(".svg"))
    plt.close(fig)


def chart_nuclear(path, generation, year, month):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(10.5, 4.8))
    for country, color in (("SE", "#1f4e79"), ("FI", "#c47b2b")):
        rows = generation["regions"][country]["metrics"]["nuclear"]["rows"]
        xs, ys = [], []
        for m in range(1, month + 1):
            block = integrate_month(rows, year, m)
            xs.append(MONTHS[m - 1])
            ys.append(block["twh"] if block["twh"] is not None else float("nan"))
        ax.plot(xs, ys, marker="o", label=f"{country} {year}", color=color)
        xs2, ys2 = [], []
        for m in range(1, month + 1):
            block = integrate_month(rows, year - 1, m)
            xs2.append(MONTHS[m - 1])
            ys2.append(block["twh"] if block["twh"] is not None else float("nan"))
        ax.plot(xs2, ys2, marker="o", linestyle="--", label=f"{country} {year-1}", color=color, alpha=0.45)
    ax.set_ylabel("TWh")
    ax.set_title("Nuclear actual production, not availability")
    ax.legend(frameon=False, ncol=4)
    fig.text(0.01, 0.01, "Daily ENTSO-E A75 mean MW × delivery hours. UMM availability is not on this chart.", fontsize=8, color="#444")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=160)
    fig.savefig(path.with_suffix(".svg"))
    plt.close(fig)


def chart_hydro(path, reservoirs):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 3, figsize=(12.5, 4.4))
    panels = [
        ("NO", "fill_month_end_pct", "Norway fill, %"),
        ("SE", "storage_month_end_twh", "Sweden, TWh"),
        ("FI", "storage_month_end_twh", "Finland, TWh"),
    ]
    for ax, (name, key, title) in zip(axes, panels):
        rows = [row for row in reservoirs if row["reservoir"] == name or (name == "NO" and str(row["reservoir"]).startswith("NO"))]
        labels = [row["reservoir"] for row in rows]
        vals = [row.get(key) if row.get(key) is not None else float("nan") for row in rows]
        ax.bar(labels, vals, color="#2f6f8f")
        ax.set_title(title)
        ax.tick_params(axis="x", labelrotation=45)
    fig.suptitle("Reservoir stock interpolated to month-end")
    fig.text(0.01, 0.01, "Weekly NVE / ENTSO-E A72 observations, interpolated. Not the full hydrological balance. Sweden and Finland have no fill percentage.", fontsize=8, color="#444")
    fig.tight_layout(rect=(0, 0.05, 1, 0.9))
    fig.savefig(path, dpi=160)
    fig.savefig(path.with_suffix(".svg"))
    plt.close(fig)


def chart_forwards(path, summary, current, previous):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    tenors = []
    for row in summary:
        if row["zone"] == "SYS" and row["tenor"] not in tenors:
            tenors.append(row["tenor"])
    # Keep the board view short: delivered month, rest of year, next year quarter and year.
    show = [t for t in tenors if t.startswith(("Oct", "Nov", "Dec", "Q1", "Q2", "Q4", "Cal-27", "Cal-28")) or t.startswith("Q")]
    show = [t for t in tenors if any(t.startswith(p) for p in ("Oct", "Nov", "Dec", "Jan", "Q1", "Q2", "Q3", "Q4", "Cal"))]
    fig, ax = plt.subplots(figsize=(12.2, 5.2))
    import numpy as np
    x = np.arange(len(show))
    def grab(zone, key):
        out = []
        for tenor in show:
            row = next((item for item in summary if item["zone"] == zone and item["tenor"] == tenor), None)
            out.append(float("nan") if row is None or row[key] is None else row[key])
        return out
    width = 0.35
    ax.bar(x - width / 2, grab("SYS", "prior_eur_mwh"), width, label=str(previous), color="#7f93a3")
    ax.bar(x + width / 2, grab("SYS", "month_end_eur_mwh"), width, label=str(current), color="#1f4e79")
    ax.plot(x, grab("DE", "month_end_eur_mwh"), color="#a33b32", marker="o", label=f"DE {current}")
    ax.set_xticks(x, show, rotation=40, ha="right")
    ax.set_ylabel("EUR/MWh outright")
    ax.set_title("SYS outright, prior month-end versus month-end")
    ax.legend(frameon=False)
    fig.text(0.01, 0.01, "EEX base outright. DE is a reference and is not added to SYS. The later file date is not on this chart.", fontsize=8, color="#444")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=160)
    fig.savefig(path.with_suffix(".svg"))
    plt.close(fig)


def chart_remainder(path, summary):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    zones = [z for z in ZONES]
    fig, ax = plt.subplots(figsize=(12.2, 5.2))
    import numpy as np
    x = np.arange(len(zones))
    width = 0.35
    def basket(key):
        out = []
        for zone in zones:
            # Reconstruct from Nov-26 and Dec-26 summary rows if both exist. The remainder CSV is authoritative;
            # this chart uses those two tenors only so the picture matches the like-for-like basket.
            nov = next((row for row in summary if row["zone"] == zone and row["tenor"] == "Nov-26"), None)
            dec = next((row for row in summary if row["zone"] == zone and row["tenor"] == "Dec-26"), None)
            if nov is None or dec is None or nov[key] is None or dec[key] is None:
                out.append(float("nan"))
            else:
                out.append(hour_weighted([(nov[key], month_hours(2026, 11)), (dec[key], month_hours(2026, 12))]))
        return out
    ax.bar(x - width / 2, basket("prior_eur_mwh"), width, label="30 Sep Nov–Dec", color="#7f93a3")
    ax.bar(x + width / 2, basket("month_end_eur_mwh"), width, label="30 Oct Nov–Dec", color="#1f4e79")
    ax.set_xticks(x, zones)
    ax.set_ylabel("EUR/MWh")
    ax.set_title("Nov–Dec outright, hour-weighted")
    ax.legend(frameon=False)
    fig.text(0.01, 0.01, "Weights: November and December delivery hours. A missing tenor is left blank. Not SYS plus an EPAD.", fontsize=8, color="#444")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=160)
    fig.savefig(path.with_suffix(".svg"))
    plt.close(fig)


def build(args):
    started = time.perf_counter()
    stages = {}
    year, month = parse_month(args.month)
    root = Path(args.data_root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "_cache"
    cache.mkdir(exist_ok=True)
    gaps = []

    def stage(name):
        stages[name] = round(time.perf_counter() - started, 1)

    t_spot = time.perf_counter()
    spot_err = None
    prices = load_json(root / "nordic-v2" / "prices_normals.json")
    try:
        spot = spot_table(found_from_prices(prices), year, month)
        for row in spot:
            row["source"] = (
                "Stored nordic-v2/prices_normals.json Nord Pool Monthly Aggregate. "
                f"File built {prices.get('built_at')}; history_end {prices.get('history_end')}. Not a new fetch."
            )
            row["cutoff"] = str(prices.get("history_end") or prices.get("built_at") or "")
    except Exception as exc:  # noqa: BLE001 — record the failure, do not invent prices
        spot, spot_err = [], f"{type(exc).__name__}: {exc}"
        gaps.append({"item": "spot", "status": "missing", "detail": spot_err})
    stages["spot_s"] = round(time.perf_counter() - t_spot, 1)

    weather = load_json(root / "nordic-v2" / "weather.json")
    climate = load_json(root / "nordic-v2" / "monthly_climate.json")
    require_baseline(weather, root / "nordic-v2" / "weather.json")
    require_baseline(climate, root / "nordic-v2" / "monthly_climate.json")
    wx = weather_table(weather, year, month)
    # Guard: the annex must agree with the published monthly climate file, which uses the same normal.
    for row in wx:
        if row["status"] != "final":
            continue
        published = climate["regions"][row["region"]]["metrics"][row["metric"]]["rows"]
        match = next((item for item in published if item[0] == row["month"]), None)
        if match and match[1] is not None and abs(match[1] - row["value"]) > 0.05:
            gaps.append({"item": f"weather {row['region']} {row['metric']}", "status": "check",
                         "detail": f"daily mean {row['value']:.3f} vs monthly_climate {match[1]}"})
    stages["weather_s"] = round(time.perf_counter() - started, 1)

    generation = load_json(root / "nordic-v2" / "generation.json")
    monthly = load_json(root / "nordic-v2" / "monthly.json")
    gen_rows, nuclear = generation_tables(generation, monthly, year, month)
    for row in gen_rows:
        if row["country"] == "Nordic" or row["fuel"] not in ("hydro", "nuclear"):
            continue
        if row.get("twh") is not None and row.get("snapshot_twh") is not None:
            if abs(row["twh"] - row["snapshot_twh"]) > 0.05:
                gaps.append({"item": f"generation {row['country']} {row['fuel']}", "status": "check",
                             "detail": f"daily integral {row['twh']:.3f} TWh vs monthly snapshot {row['snapshot_twh']}"})
    availability = generation.get("nuclear_availability") or {}
    later_note = (
        f"Later UMM snapshot, not used for {calendar.month_name[month]} production: "
        f"observed {availability.get('as_of')}. "
        f"{availability.get('definition') or 'Available capacity from REMIT, not measured generation.'} "
        "Messages in that extract are listed in csv/13_nuclear_availability_later.csv and are not October outages."
    )
    later_reactors = []
    for unit in availability.get("reactors_out") or []:
        later_reactors.append({
            **unit,
            "label": "later_update",
            "not_october_production": "yes",
            "source": availability.get("source"),
            "observed_at": availability.get("observed_at"),
            "cutoff": availability.get("as_of"),
        })
    stages["generation_s"] = round(time.perf_counter() - started, 1)

    hydro = load_json(root / "nordic-v2" / "hydrology.json")
    reservoirs, inflow, balance, omitted = hydro_tables(hydro, year, month)
    gaps.append({"item": "full hydrological balance", "status": "missing",
                 "detail": " ".join(omitted or []) or (balance or "not in the pipeline")})

    t_eex = time.perf_counter()
    eex_path = Path(args.eex_csv) if args.eex_csv else None
    if eex_path is None or not eex_path.exists():
        raise SystemExit("Pass --eex-csv pointing at the stored scrape master/eex_master.csv. This script does not download it.")
    eex_rows = read_eex(eex_path)
    dates, book = power_quotes(eex_rows)
    later_dates = [day for day in dates if day > month_end(year, month)]
    forwards, summary, remainder, current, previous, later = forward_tables(book, dates, year, month, later_dates)
    checks = outright_sanity(book, spot, year, month, current, previous) if spot else []
    for check in checks:
        if check["check"] == "looks_like_epad":
            gaps.append({"item": f"forward {check['zone']} {check['tenor']}", "status": "excluded",
                         "detail": "Delivered-month settlement looks like a spread. It was not added to SYS and is not used as an outright."})
        elif check["check"] == "unconfirmed":
            gaps.append({"item": f"forward {check['zone']} {check['tenor']}", "status": "unconfirmed",
                         "detail": f"Settlement {check['settlement']} vs spot {check['spot']} differs by more than 1 EUR/MWh."})
    drivers = driver_rows(eex_rows, current, previous)
    stages["forwards_s"] = round(time.perf_counter() - t_eex, 1)

    ember_error = None
    demand_note = (
        "Demand is missing from the stored Nordic monthlies. "
        "Ember and ENTSO-E A65 were not fetched. The cell is blank, not zero."
    )
    demand = [{"country": c, "month": args.month, "demand_twh": None, "avg_gw": None, "previous_twh": None,
               "ly_twh": None, "ytd_twh": None, "prior_ytd_twh": None, "status": "missing", "unit": "TWh",
               "source": "not in stored emmi-data monthlies", "cutoff": None, "note": demand_note}
              for c in COUNTRIES + ["Nordic"]]
    gaps.append({"item": "demand", "status": "missing", "detail": demand_note})
    stages["demand_s"] = 0

    gaps.append({"item": "cross-border transmission", "status": "missing",
                 "detail": "No stored October flow or outage series in the Nordic monthlies. Not fetched. Not filled with zero."})
    gaps.append({"item": "zone weather", "status": "missing",
                 "detail": "weather.json is country and Nordic only. No bidding-zone ERA5 series is invented."})
    if any(row["status"] != "final" for row in spot):
        bad = [row["zone"] for row in spot if row["status"] != "final"]
        gaps.append({"item": "spot completeness", "status": "check", "detail": "Not final: " + ", ".join(bad)})
    epad_rows = [row for row in forwards if row["instrument_kind"] == "epad"]
    if epad_rows:
        gaps.append({"item": "epad rows", "status": "check", "detail": f"{len(epad_rows)} rows were labelled EPAD and combined with SYS only where SYS existed."})
    else:
        gaps.append({"item": "instrument label", "status": "outright",
                     "detail": "Every POWER Base row used here is an outright. No SYS was added. Spread is zone minus SYS."})
    gaps.append({"item": "weather baseline", "status": "2006-2025",
                 "detail": f"weather.json as_of {weather.get('as_of')}; monthly_climate.json as_of {climate.get('as_of')}. 1991–2020 file was not read."})

    charts = out / "charts"
    charts.mkdir(exist_ok=True)
    chart_errors = []
    drawers = [
        ("01_spot.png", lambda p: chart_spot(p, spot, year, month)),
        ("02_spreads.png", lambda p: chart_spreads(p, spot, year, month)),
        ("03_weather.png", lambda p: chart_weather(p, wx)),
        ("04_generation.png", lambda p: chart_generation(p, gen_rows, year, month)),
        ("05_nuclear.png", lambda p: chart_nuclear(p, generation, year, month)),
        ("06_hydro.png", lambda p: chart_hydro(p, reservoirs)),
        ("07_forwards.png", lambda p: chart_forwards(p, summary, current, previous)),
        ("08_remainder.png", lambda p: chart_remainder(p, summary)),
    ]
    for name, draw in drawers:
        try:
            draw(charts / name)
        except Exception as exc:  # noqa: BLE001
            chart_errors.append(f"{name}: {type(exc).__name__}: {exc}")
    if chart_errors:
        gaps.append({"item": "charts", "status": "failed", "detail": " | ".join(chart_errors)})

    csv_dir = out / "csv"
    write_csv(csv_dir / "01_spot.csv", spot, list(spot[0].keys()) if spot else ["zone", "status"])
    write_csv(csv_dir / "02_weather.csv", wx, list(wx[0].keys()) if wx else ["region"])
    write_csv(csv_dir / "03_generation.csv", gen_rows, sorted({key for row in gen_rows for key in row}))
    write_csv(csv_dir / "04_nuclear.csv", nuclear, sorted({key for row in nuclear for key in row}) if nuclear else ["country"])
    write_csv(csv_dir / "05_demand.csv", demand, list(demand[0].keys()) if demand else ["country"])
    res_flat = []
    for row in reservoirs:
        res_flat.append({key: ("" if isinstance(value, (list, dict)) else value) for key, value in row.items()})
    write_csv(csv_dir / "06_hydro_reservoirs.csv", res_flat, list(res_flat[0].keys()) if res_flat else ["reservoir"])
    write_csv(csv_dir / "07_hydro_inflow.csv", inflow, list(inflow[0].keys()) if inflow else ["area"])
    write_csv(csv_dir / "08_forwards.csv", forwards, list(forwards[0].keys()) if forwards else ["zone"])
    write_csv(csv_dir / "09_forwards_summary.csv", summary, list(summary[0].keys()) if summary else ["zone"])
    write_csv(csv_dir / "10_remainder.csv", remainder, list(remainder[0].keys()) if remainder else ["zone"])
    write_csv(csv_dir / "11_drivers.csv", drivers, list(drivers[0].keys()) if drivers else ["contract"])
    write_csv(csv_dir / "12_coverage.csv", gaps, ["item", "status", "detail"])
    write_csv(csv_dir / "13_nuclear_availability_later.csv", later_reactors, sorted({k for r in later_reactors for k in r}) if later_reactors else ["label"])
    write_csv(csv_dir / "14_outright_checks.csv", checks, list(checks[0].keys()) if checks else ["zone", "check"])

    seconds = round(time.perf_counter() - started, 1)
    reading = calculated_reading(spot, wx, gen_rows, nuclear, reservoirs, inflow, remainder, drivers)
    ctx = {
        "year": year, "month": month, "spot": spot, "weather": wx, "generation": gen_rows, "nuclear": nuclear,
        "demand": demand, "demand_note": demand_note, "reservoirs": reservoirs, "inflow": inflow,
        "balance": balance or "", "remainder": remainder, "drivers": drivers, "reading": reading,
        "coverage": gaps, "seconds": seconds, "stages": json.dumps(stages),
        "current": None if current is None else current.isoformat(),
        "previous": None if previous is None else previous.isoformat(),
        "later": None if later is None else later.isoformat(),
        "nuclear_later_note": later_note,
    }
    markdown = render_markdown(ctx)
    ctx["markdown"] = markdown
    (out / "ANNEX.md").write_text(markdown)
    (out / "annex.html").write_text(render_html(ctx))
    meta = {
        "month": args.month, "seconds": seconds, "stages": stages, "spot_error": spot_err, "ember_error": ember_error,
        "eex_rows": len(eex_rows), "sessions": {"month_end": ctx["current"], "prior": ctx["previous"], "later": ctx["later"]},
        "baseline": [2006, 2025], "outright": "zone settlement used directly; spread = zone - SYS",
        "weather_as_of": weather.get("as_of"), "generation_as_of": generation.get("as_of"),
        "monthly_snapshot_generated": (monthly.get("snapshots") or {}).get("nordic_monthly", {}).get("generated_utc"),
    }
    (out / "RUN.json").write_text(json.dumps(meta, indent=2))
    print(f"nei monthly annex {args.month}: {seconds}s -> {out}")
    print(f"sessions month-end {ctx['current']} prior {ctx['previous']} later {ctx['later']}")
    for row in gaps:
        if row["status"] in ("missing", "failed", "excluded", "unconfirmed"):
            print(f"gap [{row['status']}] {row['item']}: {row['detail'][:180]}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build one NEI Monthly Nordic power annex")
    parser.add_argument("--month", required=True, help="Reporting month, YYYY-MM")
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument("--data-root", default=str(repo_root()), help="emmi-data checkout (nordic-v2 lives here)")
    parser.add_argument("--eex-csv", required=True, help="Stored scrape master/eex_master.csv. Not downloaded.")
    args = parser.parse_args(argv)
    return build(args)


if __name__ == "__main__":
    raise SystemExit(main())

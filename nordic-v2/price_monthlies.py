"""Nord Pool monthly day-ahead prices for prices_normals.json.

The monthly figure is Nord Pool's Monthly Aggregate: the unweighted mean of
every day-ahead delivery period whose Europe/Zurich delivery day falls in the
calendar month. It is not the mean of daily averages. A 100-period clock-change
day therefore counts for 100 periods, not for one day.

Nord Pool is the source for SYS (stored as Nordic) and for NO1–NO5, SE1–SE4,
FI, DK1 and DK2. The public DayAheadPrices API only serves about the last two
months; older months that Nord Pool still publishes come from AggregatePrices
multiAreaMonthlyAggregates, which is that same Monthly Aggregate. Years before
2024 return HTTP 401 and are not invented.

SE, NO and DK are not an official Nord Pool country price. They are the
equal-weight of their bidding zones at period level, and only on periods where
every zone has a price.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

CET = ZoneInfo('Europe/Zurich')
API = 'https://dataportal-api.nordpoolgroup.com/api/'
HEADERS = {
    'Accept': 'application/json',
    'Origin': 'https://data.nordpoolgroup.com',
    'Referer': 'https://data.nordpoolgroup.com/',
    'User-Agent': 'MasterKano-emmi-data/nordic-v2',
}
AREAS = ['SYS', 'NO1', 'NO2', 'NO3', 'NO4', 'NO5', 'SE1', 'SE2', 'SE3', 'SE4', 'FI', 'DK1', 'DK2']
CODE_OF = {'SYS': 'Nordic', **{a: a for a in AREAS if a != 'SYS'}}
GROUPS = {
    'SE': ['SE1', 'SE2', 'SE3', 'SE4'],
    'NO': ['NO1', 'NO2', 'NO3', 'NO4', 'NO5'],
    'DK': ['DK1', 'DK2'],
}
REGION_ORDER = ['NO1', 'NO2', 'NO3', 'NO4', 'NO5', 'SE1', 'SE2', 'SE3', 'SE4', 'FI', 'DK1', 'DK2', 'Nordic', 'SE', 'NO', 'DK']
NORDPOOL_FROM = 2024  # AggregatePrices and DayAheadPrices both return HTTP 401 before this year
SOURCE = (
    'Nord Pool DayAheadPrices (period mean) and AggregatePrices monthly aggregates '
    '(official Monthly Aggregate) for monthly day-ahead prices from 2024; '
    'SYS is stored as Nordic. Daily seasonal normals remain ENTSO-E A44 bidding zones '
    'and the Nord Pool system price. SE, NO and DK months are not a Nord Pool country publication.'
)
METHOD = (
    'Daily normal: unsmoothed average of the same month/day in 2021–2025. '
    'Feb 29 uses the available leap years. A day is the mean of all complete '
    'Europe/Zurich delivery-hour or 15-minute prices. '
    'Monthly: Nord Pool Monthly Aggregate, the unweighted mean of every day-ahead '
    'delivery period whose Europe/Zurich delivery day falls in that calendar month. '
    'Each period counts once (15-minute now, hourly in earlier auctions), including '
    'the 92-period spring-forward day and the 100-period fall-back day. '
    'This is not the mean of daily averages. Missing periods are not filled or interpolated. '
    'A month still inside the public DayAheadPrices window (about two months) is that '
    'period mean. Older months Nord Pool publishes use AggregatePrices '
    'multiAreaMonthlyAggregates, the same Monthly Aggregate. A month with no clock '
    'change keeps a higher-precision mean when that mean already rounds to the '
    'published aggregate; a clock-change month never does. '
    'SE, NO and DK are the equal-weight of their bidding zones at period level, '
    'and only on periods where every zone has a price. They are not load-weighted. '
    'Where period prices are outside the public window, the country month is the '
    'equal-weight of the official zonal Monthly Aggregates. Nordic is the Nord Pool system price (SYS).'
)


class NordPoolError(RuntimeError):
    pass


def zurich_today():
    return dt.datetime.now(CET).date()


def round_half_up(value, places):
    """Round half away from zero, so 88.056319 → 88.056 → 88.06 and 131.991289 → 131.99."""
    quantum = Decimal('1').scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def period_mean(values):
    """Unweighted mean of delivery-period prices. None entries stay missing."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return math.fsum(vals) / len(vals)


def monthly_mean(days):
    """Period-weighted month. `days` is a list of per-day period-price sequences.

    A 100-period day contributes 100 prices. It is not collapsed to one daily average first.
    """
    return period_mean(p for day in days for p in day)


def equal_weight_periods(zone_prices):
    """Equal-weight of bidding zones at each delivery period.

    `zone_prices` maps a zone to `(delivery_start, price_or_none)` pairs.
    A period is included only when every zone has a price. Nothing is interpolated.
    """
    if not zone_prices:
        return []
    zones = list(zone_prices)
    index = {z: {t: p for t, p in pairs} for z, pairs in zone_prices.items()}
    times = sorted(set().union(*(index[z] for z in zones)))
    out = []
    for t in times:
        prices = [index[z].get(t) for z in zones]
        if any(p is None for p in prices):
            continue
        out.append(math.fsum(prices) / len(prices))
    return out


def _as_date(value):
    return dt.date.fromisoformat(str(value)[:10])


def _get(url, timeout=90):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.status == 204 or not raw.strip():
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as err:
        if err.code in (401, 204, 404):
            return None
        raise


def _get_retry(url):
    last = None
    for attempt in range(3):
        try:
            return _get(url)
        except Exception as err:
            last = err
            time.sleep(1.5 * (attempt + 1))
    raise NordPoolError(f'{url} failed: {last}')


def _cache_path(cache_dir, name):
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / name


def _cached_json(path, url, refresh, sleep_s):
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    if sleep_s:
        time.sleep(sleep_s)
    js = _get_retry(url)
    if js is not None:
        path.write_text(json.dumps(js, separators=(',', ':')))
    return js


def _day_url(day):
    areas = ','.join(AREAS)
    return f'{API}DayAheadPrices?date={day.isoformat()}&market=DayAhead&deliveryArea={areas}&currency=EUR'


def _agg_url(year):
    areas = ','.join(AREAS)
    return f'{API}AggregatePrices?year={year}&market=DayAhead&deliveryArea={areas}&currency=EUR'


def _parse_day(js, day):
    if not js or not js.get('multiAreaEntries'):
        return None
    got = _as_date(js.get('deliveryDateCET') or day.isoformat())
    if got != day:
        raise NordPoolError(f'DayAheadPrices for {day} returned delivery day {got}')
    final = set()
    for block in js.get('areaStates') or []:
        if block.get('state') == 'Final':
            final.update(block.get('areas') or [])
    areas = {}
    for area in AREAS:
        periods = []
        for entry in js['multiAreaEntries']:
            price = (entry.get('entryPerArea') or {}).get(area)
            periods.append((entry.get('deliveryStart'), None if price is None else float(price)))
        areas[area] = periods
    return {'date': day, 'final': all(a in final for a in AREAS), 'areas': areas}


def fetch_delivery_days(today, cache_dir, sleep_s=0.3):
    """Final public DayAheadPrices days in the anonymous window, oldest first.

    Days before the window are HTTP 401. A missing day after the window has opened,
    on or before `today`, is an error. Tomorrow may be unpublished.
    """
    start = today - dt.timedelta(days=80)
    out = {}
    seen_ok = False
    day = start
    while day <= today + dt.timedelta(days=1):
        path = _cache_path(cache_dir, f'day-{day.isoformat()}.json')
        # A past Final day does not change. Today and tomorrow are refreshed.
        refresh = day >= today or not path.exists()
        # Negative-cache 401s for past days so a rebuild does not re-probe the locked history.
        miss = _cache_path(cache_dir, f'day-{day.isoformat()}.miss')
        if day < today and miss.exists() and not path.exists():
            day += dt.timedelta(days=1)
            continue
        network = not path.exists() and not (day < today and miss.exists())
        js = _cached_json(path, _day_url(day), refresh=refresh and not (day < today and path.exists()), sleep_s=sleep_s if network else 0)
        if network:
            print(f'nordpool day {day} {"periods" if js and js.get("multiAreaEntries") else "unavailable"}', flush=True)
        if js is None:
            if path.exists() and refresh:
                path.unlink()
            if day < today:
                miss.write_text('401')
            if not seen_ok:
                day += dt.timedelta(days=1)
                continue
            if day > today:
                break
            raise NordPoolError(f'No DayAheadPrices for {day}, inside the public window')
        seen_ok = True
        if miss.exists():
            miss.unlink()
        rec = _parse_day(js, day)
        if rec is None:
            if day > today:
                break
            raise NordPoolError(f'DayAheadPrices for {day} had no periods')
        out[day] = rec
        day += dt.timedelta(days=1)
    if not seen_ok:
        raise NordPoolError('DayAheadPrices returned nothing for the last 80 days')
    return out


def fetch_monthly_aggregates(today, cache_dir, sleep_s=0.3):
    """Official multiAreaMonthlyAggregates from 2024 through the current year.

    Each value is Nord Pool's published Monthly Aggregate for that delivery-day
    range (full month, or month-to-date). This is not a mean of the daily aggregates.
    """
    found = {}
    for year in range(NORDPOOL_FROM, today.year + 1):
        path = _cache_path(cache_dir, f'agg-{year}.json')
        refresh = year == today.year or not path.exists()
        js = _cached_json(path, _agg_url(year), refresh=refresh and not (year < today.year and path.exists()), sleep_s=sleep_s if not path.exists() else 0)
        if js is None:
            if year == today.year:
                raise NordPoolError(f'AggregatePrices {year} is not public')
            continue
        for row in js.get('multiAreaMonthlyAggregates') or []:
            start = _as_date(row['deliveryStart'])
            end = _as_date(row['deliveryEnd'])
            avg = row.get('averagePerArea') or {}
            found[start.strftime('%Y-%m')] = {
                'start': start,
                'end': end,
                'values': {CODE_OF[a]: (None if avg.get(a) is None else float(avg[a])) for a in AREAS if a in avg},
            }
    if not found:
        raise NordPoolError('Nord Pool published no monthly aggregates')
    return found


def _contiguous_final(days, month_start):
    """Final delivery days from the first of the month with no gap. None if day 1 is missing."""
    if month_start.month == 12:
        month_end = dt.date(month_start.year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        month_end = dt.date(month_start.year, month_start.month + 1, 1) - dt.timedelta(days=1)
    last = None
    day = month_start
    while day <= month_end:
        rec = days.get(day)
        if rec is None or not rec['final']:
            break
        last = day
        day += dt.timedelta(days=1)
    if last is None:
        return None
    return month_start, last


def _records(days, start, end):
    out = []
    day = start
    while day <= end:
        out.append(days[day])
        day += dt.timedelta(days=1)
    return out


def _zone_month_mean(records, code):
    return monthly_mean([rec['areas'][code] and [p for _, p in rec['areas'][code]] for rec in records])


def _country_month_mean(records, zones):
    merged = {z: [] for z in zones}
    for rec in records:
        for z in zones:
            merged[z].extend(rec['areas'][z])
    return period_mean(equal_weight_periods(merged))


def _store(value):
    if value is None:
        return None
    return round_half_up(value, 3)


def build_price_monthlies(today, baseline, legacy, cache_dir, sleep_s=0.3):
    """Monthly tables for prices_normals.json.

    `legacy` is `{region: {YYYY-MM: number|None}}` from the previous file or from
    the daily series. It is used only for months Nord Pool does not publish
    (before 2024). It is never the source for a month Nord Pool publishes.
    """
    cache_dir = Path(cache_dir)
    days = fetch_delivery_days(today, cache_dir, sleep_s=sleep_s)
    official = fetch_monthly_aggregates(today, cache_dir, sleep_s=sleep_s)
    period_values = {code: {} for code in REGION_ORDER}
    period_meta = {}
    official_values = {code: {} for code in REGION_ORDER}

    for ym, row in official.items():
        for code, value in row['values'].items():
            official_values[code][ym] = value
        for group, zones in GROUPS.items():
            members = [row['values'].get(z) for z in zones]
            if any(z not in row['values'] for z in zones):
                continue
            if any(v is None for v in members):
                official_values[group][ym] = None
            else:
                official_values[group][ym] = math.fsum(members) / len(members)

    for ym, row in official.items():
        span = _contiguous_final(days, row['start'])
        if span is None:
            continue
        start, last = span
        # Use periods only when they cover every day Nord Pool included, and any
        # newer Final day in an open month. A gap stays on the published aggregate.
        if last < row['end']:
            continue
        records = _records(days, start, last)
        for area, code in CODE_OF.items():
            period_values[code][ym] = _zone_month_mean(records, area)
        for group, zones in GROUPS.items():
            period_values[group][ym] = _country_month_mean(records, zones)
        counts = {}
        for area, code in CODE_OF.items():
            counts[code] = sum(p is not None for rec in records for _, p in rec['areas'][area])
        for group, zones in GROUPS.items():
            merged = {z: [] for z in zones}
            for rec in records:
                for z in zones:
                    merged[z].extend(rec['areas'][z])
            counts[group] = len(equal_weight_periods(merged))
        period_meta[ym] = {
            'start': start.isoformat(),
            'end': last.isoformat(),
            'official_end': row['end'].isoformat(),
            'periods': counts,
        }

    chosen = {}
    for code in REGION_ORDER:
        chosen[code] = {}
        months = set(legacy.get(code, {})) | set(official_values.get(code, {})) | set(period_values.get(code, {}))
        for ym in months:
            period = period_values.get(code, {}).get(ym)
            published_known = ym in official_values.get(code, {})
            published = official_values[code][ym] if published_known else None
            legacy_value = legacy.get(code, {}).get(ym) if ym in legacy.get(code, {}) else None
            if period is not None:
                chosen[code][ym] = _store(period)
            elif published_known:
                # Nord Pool published this month. Keep a non-clock-change mean only when it
                # already rounds to that aggregate; otherwise the published figure wins.
                # Clock-change months are never the mean of daily averages.
                clock_change = ym[5:7] in ('03', '10')
                if (legacy_value is not None and published is not None and not clock_change
                        and round_half_up(float(legacy_value), 2) == round_half_up(float(published), 2)):
                    chosen[code][ym] = legacy_value
                else:
                    chosen[code][ym] = _store(published)
            elif legacy_value is not None or ym in legacy.get(code, {}):
                chosen[code][ym] = legacy_value
            else:
                chosen[code][ym] = None

    months = sorted({ym for code in chosen for ym in chosen[code]})
    monthly = {code: [chosen[code].get(ym) for ym in months] for code in REGION_ORDER if code in chosen}
    y0, y1 = baseline
    monthly_normal = {}
    for code, series in chosen.items():
        by = {}
        for ym, value in series.items():
            if value is None:
                continue
            year, month = (int(part) for part in ym.split('-'))
            if year < y0 or year > y1 or year == today.year:
                continue
            by.setdefault(month, []).append(float(value))
        monthly_normal[code] = {}
        for month in range(1, 13):
            vs = by.get(month, [])
            monthly_normal[code][f'{month:02d}'] = {
                'normal': None if not vs else round_half_up(math.fsum(vs) / len(vs), 3),
                'min': None if not vs else round_half_up(min(vs), 3),
                'max': None if not vs else round_half_up(max(vs), 3),
                'n_years': len(vs),
            }

    history_end = None
    for meta in period_meta.values():
        history_end = meta['end'] if history_end is None else max(history_end, meta['end'])
    if history_end is None:
        ends = [row['end'].isoformat() for row in official.values()]
        history_end = max(ends) if ends else None

    covered = ', '.join(f'{ym} through {period_meta[ym]["end"]}' for ym in sorted(period_meta))
    notes = [
        'Nordic (official Nord Pool system price) monthly history starts 2024-01-01. '
        'DayAheadPrices and AggregatePrices return HTTP 401 for years before 2024, so SYS is not extended earlier. '
        'Bidding-zone daily normals use 2021–2025. Nordic monthly normals use the years actually on file, 2024–2025.',
        'From 2024-01, monthly figures follow Nord Pool\'s period-weighted Monthly Aggregate: the unweighted mean of '
        'day-ahead delivery periods, not the mean of daily averages. Months fully inside the public DayAheadPrices '
        f'window are the calculated period mean ({covered or "none on this run"}). Earlier published months use '
        'AggregatePrices multiAreaMonthlyAggregates. A month with no clock change keeps the previous higher-precision '
        'mean when that mean already rounds to the published aggregate. Missing periods are not filled.',
        '2021-01 through 2023-12 bidding-zone, SE, NO and DK monthlies remain the mean of ENTSO-E A44 daily averages. '
        'Nord Pool\'s public API does not publish those years (HTTP 401). March and October in 2021–2023 are still '
        'day-weighted and are not the official Nord Pool Monthly Aggregate.',
        'SE, NO and DK are an equal-weight of their bidding zones at delivery-period level, included only when every '
        'zone has that period. They are not an official Nord Pool country price and are not load-weighted. Outside the '
        'public period window the country month is the equal-weight of the official zonal Monthly Aggregates.',
        f'Monthly coverage runs through {history_end}, the last complete Final delivery day included in a period mean '
        'or, if no period month was open, the last day of Nord Pool\'s published Monthly Aggregate.',
    ]
    return {
        'months': months,
        'monthly': monthly,
        'monthly_normal': monthly_normal,
        'source': SOURCE,
        'method': METHOD,
        'notes': notes,
        'history_end': history_end,
        'period_values': {code: dict(vals) for code, vals in period_values.items()},
        'official_values': official_values,
        'period_meta': period_meta,
    }


def legacy_from_document(doc):
    months = doc['months']
    return {code: {ym: value for ym, value in zip(months, values)} for code, values in doc['monthly'].items()}


def apply_to_document(doc, pack):
    doc['source'] = pack['source']
    doc['method'] = pack['method']
    doc['notes'] = pack['notes']
    doc['history_end'] = pack['history_end']
    doc['months'] = pack['months']
    doc['monthly'] = pack['monthly']
    doc['monthly_normal'] = pack['monthly_normal']
    return doc


def _check_known_october(pack):
    """Refuse to write if a full period month disagrees with Nord Pool's published aggregate.

    October 2026 is also pinned to the period count and the unrounded means already
    checked against the public 15-minute history (2980 periods).
    """
    areas = ['Nordic', 'NO1', 'NO2', 'NO3', 'NO4', 'NO5', 'SE1', 'SE2', 'SE3', 'SE4', 'FI', 'DK1', 'DK2']
    for ym, meta in pack['period_meta'].items():
        if meta['end'] != meta['official_end']:
            continue
        for code in areas:
            raw = pack['period_values'][code].get(ym)
            pub = pack['official_values'][code].get(ym)
            if raw is None or pub is None:
                raise NordPoolError(f'{ym} missing for {code}')
            if round_half_up(raw, 2) != round_half_up(pub, 2):
                raise NordPoolError(f'{code} {ym} period mean {raw} does not match Nord Pool {pub}')
    # Pinned while October 2026 is still inside the public period window. Once it ages
    # out, AggregatePrices multiAreaMonthlyAggregates remains the Monthly Aggregate.
    meta = pack['period_meta'].get('2026-10')
    if not meta:
        return
    expected = {'Nordic': (88.056319, 88.06, 2980), 'NO1': (131.991289, 131.99, 2980)}
    for code, (full, published, n_periods) in expected.items():
        raw = pack['period_values'][code].get('2026-10')
        if meta['periods'].get(code) != n_periods:
            raise NordPoolError(f'{code} October 2026 has {meta["periods"].get(code)} periods, expected {n_periods}')
        if abs(raw - full) > 5e-4:
            raise NordPoolError(f'{code} October 2026 period mean {raw} is not {full}')
        if round_half_up(raw, 2) != published:
            raise NordPoolError(f'{code} October 2026 rounds to {round_half_up(raw, 2)}, not {published}')


def write_prices_normals(path, today=None, sleep_s=0.3):
    path = Path(path)
    doc = json.loads(path.read_text())
    legacy = legacy_from_document(doc)
    pack = build_price_monthlies(today or zurich_today(), (doc['baseline'][0], doc['baseline'][1]), legacy, path.parent / 'cache' / 'nordpool', sleep_s=sleep_s)
    _check_known_october(pack)
    apply_to_document(doc, pack)
    text = json.dumps(doc, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    path.write_text(text)
    manifest = path.parent / 'manifest.json'
    if manifest.exists():
        man = json.loads(manifest.read_text())
        entry = man.get('files', {}).get(path.name)
        if entry is not None:
            raw = text.encode()
            entry['bytes'] = len(raw)
            entry['sha256'] = hashlib.sha256(raw).hexdigest()
            manifest.write_text(json.dumps(man, separators=(',', ':'), ensure_ascii=False, allow_nan=False))
    return pack


def _print_report(doc_before, pack):
    months = doc_before['months']
    old = {code: {ym: value for ym, value in zip(months, values)} for code, values in doc_before['monthly'].items()}
    codes = ['Nordic', 'NO1', 'NO2', 'NO3', 'NO4', 'NO5', 'SE1', 'SE2', 'SE3', 'SE4', 'FI', 'DK1', 'DK2', 'SE', 'NO', 'DK']

    def cell(code, ym):
        return old.get(code, {}).get(ym), pack['monthly'][code][pack['months'].index(ym)]

    print(f"history_end {pack['history_end']}")
    print(f"period months { {ym: pack['period_meta'][ym]['end'] for ym in sorted(pack['period_meta'])} }")
    print('October 2026 old -> stored (unrounded period mean)')
    for code in codes:
        before, after = cell(code, '2026-10')
        raw = pack['period_values'][code].get('2026-10')
        n = pack['period_meta'].get('2026-10', {}).get('periods', {}).get(code)
        print(f'  {code:7} {before} -> {after}  raw {raw}  periods {n}')
    print('March 2026 and September 2026 and November 2026')
    for ym in ('2026-03', '2026-09', '2026-11'):
        for code in ('Nordic', 'NO1', 'SE', 'NO', 'DK'):
            before, after = cell(code, ym)
            raw = pack['period_values'][code].get(ym)
            print(f'  {ym} {code:7} {before} -> {after}  raw {raw}')
    print('October normal min/normal/max/n  old -> new')
    for code in ('Nordic', 'NO1'):
        before = doc_before['monthly_normal'][code]['10']
        after = pack['monthly_normal'][code]['10']
        print(f'  {code} {before} -> {after}')
    print('March normal')
    for code in ('Nordic', 'NO1'):
        print(f'  {code} {doc_before["monthly_normal"][code]["03"]} -> {pack["monthly_normal"][code]["03"]}')
    moved = []
    for code in codes:
        for ym in pack['months']:
            if not ym.startswith('2024') and not ym.startswith('2025') and not ym.startswith('2026'):
                continue
            before = old.get(code, {}).get(ym)
            after = pack['monthly'][code][pack['months'].index(ym)]
            if before is None or after is None:
                continue
            if abs(float(before) - float(after)) > 0.02:
                moved.append((ym, code, before, after, round(float(after) - float(before), 3)))
    print(f'months moving by more than 0.02: {len(moved)}')
    for row in moved:
        if row[0][5:7] in ('03', '10') or row[0] == '2026-11':
            print('  ', row)


def main():
    path = Path(__file__).resolve().parent / 'prices_normals.json'
    before = json.loads(path.read_text())
    pack = write_prices_normals(path)
    _print_report(before, pack)


if __name__ == '__main__':
    main()

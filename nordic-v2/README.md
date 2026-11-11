# Nordic v2 — data contract (pilot, not a live-site migration)

This folder is the data layer for Jerry's Nordic redesign. Nothing here changes the EMMI site, Pulse, masters or routines. Read `manifest.json` before rendering. Sources can be delayed or incomplete: **null is unavailable, never zero**. Do not invent missing curves, use a country price as the system price, silently carry forward inputs, or substitute 10m wind.

## Run on the box

```
/workspace/brief/.venv/bin/python /home/box/data/nordic-v2/build.py
# Change weather baseline deliberately, without editing code:
/workspace/brief/.venv/bin/python /home/box/data/nordic-v2/build.py --baseline 2006:2025
```

Default weather baseline **2006–2025**, Jerry's 11 Nov correction. Output is `/home/box/data/nordic-v2/output/`. `--out PATH` changes the destination; `--only weather,prices,generation,hydrology,tomorrow,monthly` selects blocks. The script does not publish, send email, change scheduling or write to EMMI. Publication is a separate reviewed data-only PR.

Dependencies: box's existing brief virtualenv (pandas, numpy, requests, curl_cffi, shapely, openpyxl, beautifulsoup4); existing `/workspace/emmi-data/scripts` ENTSO-E/source helpers; existing `/home/box/data/nordic-climate/daily_products.py`. The tracked `daily_products.patch` records the additive box-only extension (legacy refresh logic is unchanged). That file remains the only climate product builder: its added `regional_raw_daily()` calls the existing ERA5 fetcher and never calls the legacy product refresh writer. API keys are environment-only: `ENTSOE_API_TOKEN`, optional `FINGRID_API_KEY`. No keys or raw hourly weather downloads are published. The full 1940+ cache remains on the box.

Every JSON has `schema_version: 1` and `as_of` (`YYYY-MM-DD`, or null). `as_of` is the observation/delivery coverage date, not a claim all components are current. Use each region/metric's own date. `built_at` / `observed_at` are ISO 8601 **Europe/Zurich** timestamps with offset. Day-ahead hours also carry the offset (DST-safe). Daily weather uses UTC calendar days; prices and generation use Europe/Zurich delivery days. All numbers retain up to 3 decimals for calculation; show one decimal in the UI. Dates are ISO strings. Array column order is embedded in each block's `columns`.

## Files and fields

### `manifest.json`

`built_at`, `baseline` (weather start/end year), `files` (filename → `bytes`, `sha256`, `as_of`), `errors` (`part`, `error`), `status` (`built` or `partial`), `policy`. Source gaps do not become fabricated values. A partial file is useful for the test page but must visibly show its missing/stale components.

### `prices.json`

`units: EUR/MWh`, `source`, `columns: [date,value,ma30]`, `regions`, `futures`.

Each `regions[code]`: `as_of`, `rows`, optional `definition`. Codes: `Nordic` (official Nord Pool **system** price), `SE1`–`SE4`, `NO1`–`NO5`, `FI`, `DK1`, `DK2`, plus `SE`, `NO`, `DK`. Country prices are **equal-weight bidding-zone averages**, not load-weighted national prices. Never label the country average as the system price. `ma30` needs all 30 calendar days. 123 plotted days are retained; older input days warm up the average.

`futures.front_month` and `futures.next_quarter`: `as_of` (settlement date), `contract` (e.g. Dec-26/Q1-27), `unit`, `regions`. A region value is the existing EEX helper's `{p,d1,w1,m1,s1,from?}`: settlement, absolute 1-day/7-day/28-day/since-first changes and optional first-history date. Null means no quote. These are fixed named contracts, not a synthetic continuous roll series. Source is MasterKano/scrape `master/eex_master.csv`, which starts 2026-09-01.

### `weather.json`

`baseline`, `history_start`, `units`, `source`, `method`, `grid`, `point_counts`, `columns: [date,value,ma30,normal,normal_ma30]`, `regions`.

Regions: **Nordic, SE, NO, FI, DK**. Each region: `as_of`, `metrics`. Each metric (`wind100`, `temp`, `rain`): `as_of`, `history_start`, `rows`, `summary`.

- `wind100`: raw ERA5 **100m** daily mean speed, m/s. It is never a 10m extrapolation/calibration.
- `temp`: raw ERA5 2m daily mean, degrees C.
- `rain`: raw ERA5 daily precipitation, mm/day.
- `normal`: unsmoothed average of the same month/day in 2006–2025 (Feb 29 uses the available leap years).
- `ma30`: mean of the last 30 complete calendar-day values.
- `normal_ma30`: average of the normal for those same 30 calendar dates, including when the window crosses New Year.
- `summary`: latest 30-day `value`, `normal`, `difference` in `difference_unit` (native units), and `difference_pct` for wind/rain. Temperature uses degrees C, never a percentage.

**Geography caveat:** fractional country intersections of Natural Earth's public-domain 1:50m country polygons with the existing 2° latitude × 4° longitude ERA5 grid cells, weighted by cos(latitude). The existing cells cover the mainland countries, including DK/FI; no extra API grid was needed. Count of intersecting cells: SE 24, NO 24, FI 18, DK 5; Nordic union 51. Open-Meteo may snap a requested point by approximately 0.25 degrees; weights belong to the original requested grid cells. This is a **coarse regional proxy**, not a dense national meteorological average or wind-farm weighting. Offshore islands outside 2–34°E/54–72°N are excluded. Nordic is now the land-area-weighted union of these four countries, **not the old 72-point land-and-sea box average**. Temperature/rain are raw, uncalibrated for comparable regional baselines; legacy monthly workbook calibration is not applied to countries.

### `weather_normals.json`

`baseline`, `source`, `method`, `columns: [month_day,wind100,temp,rain]`, `regions`. Each region contains 366 rows. Units and geography match `weather.json`. `as_of` is the baseline end date, not a stale-current-data warning. Used by the tomorrow temperature comparison.

### `generation.json`

`source`, `history_requested_start`, `history_normal`, `columns: [date,value,ma30,normal_ma30,normal_n_years]`, `units`, `regions`, `aggregate_members`, `capacity`, `capacity_latest`, `capacity_daily`, `capacity_zones`, `capacity_notes`, `nuclear_availability`.

Each region: `as_of`, actual `history_start`, `metrics`. Metrics: `wind_load_factor` (%), `hydro` (MW, reservoir + run-of-river; **excludes pumped-storage generation**), `nuclear` (actual MW). Metric objects have their own `as_of`, `history_start`, `rows`. A full-day, time-weighted mean is required, accounting for 23/25-hour delivery days. A missing day invalidates the 30-day average rather than shortening it.

`normal_ma30` is the mean of the same calendar-day trailing-30-day metric in all available **preceding years**, with `normal_n_years >= 3`. Generation cannot use the 20-year weather baseline: ENTSO-E starts in 2015. Wind uses output divided by the capacity applicable to that observation date; it does not divide historical output by today's turbines. Values outside 0–110% are flagged unavailable, not clipped into plausible values. Nuclear online availability is not smoothed.

`aggregate_members` declares Nordic fuel coverage: wind NO/SE/FI/DK, hydro NO/SE/FI (DK lacks a material reported hydro series), nuclear SE/FI. Missing applicable country data makes an aggregate null.

`capacity[country][year]`: `year`, `as_of` (effective 1 Jan annual A68 value, **not** a census at build time), `valid_through`, `wind_mw`, `nuclear_mw`, `source`, `source_url`, `psr_mw` (reported ENTSO-E production-type components). `capacity_zones[zone]`: `as_of`, `source`, `psr_mw`, `wind_mw`, `note`. Zone reporting scope may not reconcile with national figures; don't silently sum it to fill a country gap.

`capacity_latest[country]`: `as_of`, `wind_mw`, `source`, `source_url`, `status`, with optional `observed_at`, `stale_days`, `excluded_unknown_capacity` and annual metadata. This tells you **how current the denominator really is**. `capacity_daily[country]` has `columns: [date,wind_mw]`, `rows`, `latest_source`. National supplements: Fingrid dataset 268 for Finland; Danish Energy Agency current + retired turbine registers for Denmark, reconstructing the same daily denominator since 2015 from connection/disconnection dates (rather than mixing a broader national current denominator with a narrower historical one). A DK register denominator held beyond its coverage endpoint is explicitly provisional. Sweden's current country aggregate is missing: old capacity is reported for information but is **not used** to fabricate current SE/Nordic load factors. National Swedish PxWeb retrieval failed TLS validation and was not bypassed.

`nuclear_availability`: `as_of`, `observed_at`, `installed_mw` / `online_mw` (country maps, MW), `source`, `definition`, `reactors_out`. It reuses nuclear panel v3's exact Nord Pool UMM source/helper. Reactor fields: `n` name, `c` country, `cap` installed MW, `now` available MW, `min` minimum available MW in the forecast horizon, `start` / `back` ISO event timestamps, `planned` boolean, `why` public reason. Event timestamps are converted to Europe/Zurich with the applicable offset. **Online means available from REMIT messages, not measured generation.**

### `hydrology.json`

`source`, `reservoirs`, `monthly_inflow`, `balance_definition`, `omitted`.

`reservoirs.NO` and `NO1`–`NO5`: `as_of`, `history_start`, `unit: % full`, `band_period`, `columns: [date,fill_pct,storage_twh]`, `rows`, `band_columns: [iso_week,min,median,max]`, `band`, `reservoir_balance_twh`. Reservoir filling bands use NVE's published reference, not an invented 20-year weather normal.

`reservoirs.SE` / `FI`: `as_of`, `history_start`, `unit: TWh stored`, `source`, `columns: [date,storage_twh]`, `rows`, `band_columns: [iso_week,min,median,max,n_years]`, `band`, `band_period`, `reservoir_balance_twh`. No matched percentage-capacity denominator exists in this pipeline, so stored TWh must not be presented as percent full. ENTSO-E A72 history is fetched from 2015; some first ISO weeks begin in late December 2014.

`reservoir_balance_twh`: `value`, `normal`, `difference`. **This is reservoir storage only**, not the full water+snow+soil hydro balance.

`monthly_inflow[country]` (NO, SE, NOSE): `as_of`, `history_start`, `unit: TWh/month`, `baseline`, `columns: [month,value,normal,n_years]`, `rows`, `method`, `stale_snapshot`, `source`. It reuses the existing monthly inflow estimator: change in stored reservoir energy + hydro generation, with weekly readings interpolated to month ends. It is not measured natural inflow. The unchanged source snapshot currently ends September 2026; show that date. Normal uses available 2021–2025 same-month values.

**Not implemented:** snowpack vs normal and full water+snow hydro balance. Free seNorge/SMHI grids exist, but a validated Nordic hydropower-basin/energy conversion is not integrated; do not claim there are no free sources and do not relabel reservoir-only storage as the full balance. DK hydro reservoirs are not a meaningful regional block.

### `tomorrow.json`

`delivery_date`, `timezone`, `hours` (ISO delivery-hour timestamps), `units`, `source`, `regions`, `temperature`. `as_of` is the forecast retrieval day, distinct from the future `delivery_date`.

Each zone/country: `as_of`, arrays `price`, `wind_fc`, `solar_fc`, `load_fc`, `residual_load`; `complete` maps those fields to booleans. Arrays align to `hours`, including DST days. Prices are EUR/MWh; forecasts and residual load are MW. Residual load = load − wind − solar **only where all three forecasts exist**. No solar forecast is not assumed to be zero. Country prices are equal-weight means, power forecasts sum all applicable zones (with complete coverage required).

`temperature[region]`: `as_of`, `hourly`, `daily_mean`, `daily_normal`, `difference_degC`, `normal_baseline`, `note`. Raw Open-Meteo IFS forecast at the same weighted points; comparison is forecast local-day mean versus ERA5 UTC-day seasonal daily normal. That small day-boundary distinction is disclosed; these are not hourly climatological normals.

Tomorrow prices/load are available for all zones at build time, but wind/solar publication is incomplete for SE/FI/DK. Norway residual-load curves are complete. Other residual-load curves remain null and must display "forecast not available", not a zero curve.

### `monthly.json`

`source`, `stale`, `snapshots`, `climate_100m`, `wind_display_rule`, `note`. The two legacy objects (`snapshots.nordic_mtd`, `snapshots.nordic_monthly`) are copied **unchanged** from existing EMMI monthly-view inputs, including their native `generated_utc`, `days`/`months`, zone/country maps, `types`, generation, prices, weather, `inflow`, `gaps`, source metadata and build timings. Do not reinterpret native TWh/MW/price units. This is intentionally a marked-stale snapshot, not a newly refreshed source.

`climate_100m` is the existing climate `mtd.json`, also unchanged: native `generated`, `wind_height_m`, `basis`, `months`, `daily_coverage`, `variable_coverage`, notes. Month records retain `days_covered`, first/last dates, units, `wind100`/`temp`/`rain`, historical min/mean/max/ranks, and `same_dates` (legacy 1991–2020). **Only `climate_100m.wind100` is approved for monthly wind speed; never render any old wx wind fields as 100m.** Legacy monthly reference periods remain unchanged; the new daily weather block is 2006–2025. Display monthly numbers to one decimal and label the dashed average line with its value. Preserve existing regional/month buttons.

## Provenance/history and known limits

- ERA5 via Open-Meteo archive, raw 100m wind / temperature / precipitation: **1940-01-01** onward for wind/temperature; rain starts **1940-01-02** because the source has no first-day accumulation. Per-metric history starts are explicit. Roughly 5-day lag; all output dates are explicit. Natural Earth mask polygons are public domain.
- ENTSO-E A44 spot inputs: fetched 153 days back, retaining 123 daily points plus warmed 30-day averages. Nordic SYS comes from the existing Nord Pool aggregate source (calendar 2026). Sources are not substituted for each other.
- EEX scraper: **2026-09-01** onward; settlement date is retained (currently may lag more than one business day).
- ENTSO-E actual generation / annual capacities: requested **2015** onward. Actual country/metric start dates are in `generation.json` (SE first full day 2015-01-05, NO/FI 2015-01-01, DK 2015-01-02). DK recent actual generation has gaps.
- NVE reservoir history: available from **1995-01-08**; published min/median/max reference 2006–2025. SE/FI A72 query history starts 2015 (first week dated 2014-12-29).
- Existing derived monthly inflow: **2021-01** onward; unchanged source through September 2026.
- Nord Pool UMM: current availability + next 14 days, not historical plant output.
- ENTSO-E tomorrow forecasts: delivery-day horizon, subject to publication timing. Open-Meteo IFS: current forecast; no historical forecast archive in this folder.

No signup, subscription, card, paid data service or new hosting was created. Test-page consumers can fetch these small public files directly from `raw.githubusercontent.com/MasterKano/emmi-data/main/nordic-v2/` after the data PR merges. Old site/email consumers are not redirected.

## Verification

`/workspace/brief/.venv/bin/python /home/box/data/nordic-v2/verify.py` checks actual source values, 2006–2025 normals, requested-cell country masks, 100m height, 30 complete calendar days, DST delivery days, real capacity division, missing-data handling, unchanged monthly snapshots, file sizes and manifest hashes. `VERIFICATION.txt` records the successful snapshot check. Run on the box with its source caches, not in the page builder.

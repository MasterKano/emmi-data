# Nordic climate: daily 1940 onward, wind at 100m

Nordic box **55–71°N, 4–32°E**, land and sea: 72 fixed points (latitudes 55,57,…,71; longitudes 4,8,…,32), cosine-latitude area weights using the returned ERA5 cell latitudes, exactly as in the original temperature/rain calibration. This is a regional weather indicator, not a wind-farm-output-weighted index.

Jerry approved moving **all wind to 100m**, including Pulse, normals and history (11 Nov 2026 09:24 Zurich, card t1102s3502), then extending daily history as far back as possible (09:25 Zurich, t1103u). The old 10m workbook is not a calibration target for 100m wind.

## Compact files

| File | Contents |
|---|---|
| `daily_grid_mean.csv` | Date, `wind100` (m/s at 100m), `temp` (°C), `rain` (mm/day), from 1940-01-01 to the latest complete ERA5 day. Rounded to two decimals; this is the canonical daily series used to build other daily products. |
| `daily_normals.csv` | 1991–2020 exact-date mean, p10/p90, minimum, maximum and circular ±7-day smoothed mean for each variable. `wind100_*` is exclusively 100m. February 29 has 8 years; other dates have 30. The smoothed mean is a chart guide, not the same-dates comparison. |
| `monthly.csv` | Complete-month raw 100m wind means (`wind100`), plus temperature and rainfall. Existing post-1970 workbook temperature/rain are preserved; otherwise calibrated daily means are used. `wind10_legacy` preserves the old workbook reference only: it must never feed charts or comparisons. `wind_source` is separate from the temperature/rain `source`. |
| `mtd.json` | Latest two months, raw 100m wind under `wind100`, temperature and rain; legacy full-month 1970–2025 comparisons and exact-calendar-date 1991–2020 `same_dates` comparisons. Includes anomaly, empirical percentile and same dates last year. Rain has cumulative totals from 1 October. |

## Source and aggregation

[Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api), `models=era5`, `timezone=GMT`, `wind_speed_unit=ms`. Wind uses **hourly `wind_speed_100m`**, averaged only over complete 24-hour UTC days, then cosine-latitude weighted over all 72 points. This is mean scalar speed, not speed computed from averaged wind vectors. Temperature uses `temperature_2m_mean`; rainfall uses `precipitation_sum`.

All 72 points must be present and unique for a daily row. Wind and temperature are complete from 1940-01-01. The API returns no rainfall for 1940-01-01 at any of the 72 points; that one boundary value is left blank, never imputed. Rain starts 1940-01-02, with no subsequent gaps. `monthly.csv` includes `rain_days`, so January 1940 visibly has 30 rain days, not 31. Gaps from 1940 to the last available day block migration. Missing 1991–2020 days block normals. ERA5 normally lags about five days. UTC weather days differ from the CET/CEST ENTSO-E generation days; generation figures are not wind-speed measurements and remain unchanged.

Three coastal grid requests snap to nearby ERA5 cells: (55,12)→(55.25,12), (69,16)→(69,15.75), (69,28)→(69.25,28). These are the same cells used previously; the actual cell coordinates are retained in the raw point cache.

Raw hourly responses (gzip JSON), per-point daily caches and calibration files stay on the box, under `cache/openmeteo100m/`; only compact files above are published to `MasterKano/emmi-data/nordic-climate/` on main. Requests use 12-coordinate batches with quota tracking and 429 backoff; completed batches and months are reusable.

## Calibration

**100m wind is raw ERA5, with no linear calibration or extrapolation from 10m.** Temperature and rain retain the existing 2024-01..2026-09 monthly-workbook calibration, applied to daily values: temperature slope 1.029240/intercept −0.414674; rain slope 1.094392/intercept −0.099367. Exact coefficients remain in the local `calibration.json`. Rain is clipped to zero. Pre-1970 temperature/rain reuse this post-1970 calibration: these are calibrated reanalysis estimates, not newly recovered observations. This assumes the modern calibration transfers back in time; rainfall may differ from the monthly workbook.

The normal remains **1991–2020**, the standard recent 30-year climatology; a longer history does not justify changing that comparison baseline. Percentiles are the proportion of the 30 same-date yearly period means less than or equal to the current mean (not a percentile of individual days).

## Refresh

`update.py` is the monthly routine entry point; `fetch_daily_history.py` is the resumable downloader; `daily_products.py` is the only daily-products builder. The monthly routine preserves 100m data and refreshes normals/MTD/history together. Run locally without publication:

```
/workspace/brief/.venv/bin/python /home/box/data/nordic-climate/update.py --no-publish
```

Pulse's provisional IFS top-up also uses hourly 100m wind and a versioned 100m cache; the overlap correction is at the same height. It never applies the old 10m calibration. ERA5 overlap values come from the canonical daily CSV, so comparisons can be reproduced directly. IFS and the generation charts retain their existing separate provenance and lag notes.

# Nordic climate daily history and normals

Compact files for the Nordic box **55–71°N, 4–32°E**, so weather panels can compare the current period with the same calendar dates' 1991–2020 average, not month on month.

Raw per-point Open-Meteo downloads stay on the build machine and are not in this folder.

## Files

| File | What it is |
|---|---|
| `monthly.csv` | Monthly wind (m/s), temperature (°C) and rain (mm/d) from 1970, from Jerry's [mr-climate](https://github.com/MasterKano/mr-climate) history (KNMI/ERA5 workbook, later Copernicus CDS). Occasional trailing months may be a provisional calibrated Open-Meteo estimate until the history publishes them. |
| `daily_grid_mean.csv` | Daily area-mean wind, temperature and rain, 1991 to the latest available day, linearly calibrated to that history. Rain is clipped at 0 after calibration. |
| `daily_normals.csv` | For each day of the year, including 29 February: the 1991–2020 mean, 10th and 90th percentiles, min and max. `*_mean_smooth` is the unsmoothed mean passed through a circular ±7-day window (29 Feb is day 60 of a 366-day calendar). Use the unsmoothed exact-date statistics for figures; use the smoothed mean for a seasonal curve. 29 Feb uses the eight leap years in 1991–2020; every other day uses 30 years. |
| `mtd.json` | Month-to-date values. Legacy fields still compare a partial month with the full-month 1970–2025 climatology. `same_dates` compares the month-to-date mean with the 1991–2020 mean of those exact dates, plus the anomaly, the percentile in that 30-year sample, and the same dates last year. Rain also has `season_cumulative`: the sum since 1 October, in mm, against the same-dates normal. |

Units: wind in m/s, temperature in °C, rain in mm/day in the daily and monthly files. Cumulative rain in `mtd.json` is the sum of daily rain, in mm.

## Sources and grid

- Monthly history: [MasterKano/mr-climate](https://github.com/MasterKano/mr-climate), from the KNMI Climate Explorer workbook and Copernicus ERA5 (`reanalysis-era5-single-levels`, area `[71, 4, 55, 32]`).
- Daily values: [Open-Meteo archive API](https://open-meteo.com/) `models=era5`, free, no signup. Daily `wind_speed_10m_mean` (m/s), `temperature_2m_mean` (°C) and `precipitation_sum` (mm), timezone GMT.
- Grid: 72 points, latitude 55, 57, …, 71 (step 2°) and longitude 4, 8, …, 32 (step 4°), land and sea. A few coastal points may snap to a nearby land cell.
- Open-Meteo ERA5 typically lags about five days, so the latest month-to-date window ends then.

## Calibration

Open-Meteo daily area means are linearly calibrated to Jerry's monthly history over **2024-01..2026-09** (33 months). The fit is at monthly scale; the same slope and intercept are applied to each daily value, then rain is clipped at 0. Each variable uses a cosine-latitude weighted mean:

| Variable | Scheme | Slope | Intercept | Correlation | Calibrated RMSE |
|---|---|---:|---:|---:|---:|
| Wind (m/s) | coslat | 0.912145 | 0.167952 | 0.9979 | 0.0392 |
| Temperature (°C) | coslat | 1.029240 | -0.414674 | 0.9997 | 0.1696 |
| Rain (mm/d) | coslat | 1.094392 | -0.099367 | 0.9933 | 0.0757 |

## Update schedule

The Grok Bot routine **Monthly Nordic climate update** runs `/home/box/data/nordic-climate/update.py`. That script refreshes the Open-Meteo cache for any new complete month, rebuilds these compact files, and publishes this folder to `MasterKano/emmi-data` on `main`. It does not commit the raw 72-point cache or any secrets.

Re-run locally:

```bash
/workspace/brief/.venv/bin/python /home/box/data/nordic-climate/update.py
```

Use `--no-publish` to rebuild local files without pushing.

## Sanity check and caveats

On 2024-01..2026-09 (the calibration window) the monthly means of the daily series match `monthly.csv` with essentially zero bias and the same RMSE as the table above.

Over 1991–2020, wind stays close (bias +0.006 m/s, RMSE 0.048) and temperature is a little cool (bias −0.077 °C, RMSE 0.146). Rain is systematically about 0.20 mm/d drier than `monthly.csv` (RMSE 0.215). That offset almost disappears from 2021. The month-to-date `same_dates` figures are still internally consistent, because both the current period and the 1991–2020 normal come from the same calibrated daily series. The rain offset only matters if those daily monthly means are compared with `monthly.csv`.

Coverage at the last rebuild: 13,093 consecutive days, 1991-01-01 to 2026-11-05, with a complete 72-point grid every day. 2026-11 is not cached until the month is complete; the monthly routine adds the published days of the current month on the fly.

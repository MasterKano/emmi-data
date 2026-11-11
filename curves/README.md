# Futures curve archive

One small CSV per curve per settlement date, for the EMMI copy-to-Excel and date-picker block (built separately).

```
curves/<curve>/<YYYY-MM-DD>.csv
curves/<curve>/index.json
```

Built 2026-11-11 15:41 Europe/Zurich. Files are data only. This folder lives on `main` (not the force-pushed `live-data` branch) so history is kept.

## Schema

Every CSV has these columns:

| Column | Meaning |
|---|---|
| `contract` | Stable id. Nordic/TTF: `Dec-26`, `Q1-27`, `Cal-27` (and `Winter-27` for TTF). HO: `HOZ26`. Brent: `BRNF27` (ICE). |
| `label` | Short display name (`Dec-26`, `Jan-27`). |
| `expiry` | ICE/NYMEX last trading day where known (Brent, HO). For EEX Nordic and TTF this is the **first day of the delivery period**, not the last trading day (EEX last-trade dates are not in the source file). |
| `settle` | Settlement or delayed last (see source). Never interpolated or carried forward. |
| `change` | Versus the previous archived session of the same contract. Blank on the first date that contract appears. |
| `currency` / `unit` | Per curve, also listed below. |
| `source` | `eex` (official EEX settlement via [MasterKano/scrape](https://github.com/MasterKano/scrape) `master/eex_master.csv`); `tradingview_scanner` (TradingView public futures scanner, delayed last); `tradingview_scanner_history` (backfill from the existing EMMA/emmi-data HO history file, which recorded session last at fetch time). |
| `as_of` | The settlement or session date, equal to the filename date. |

A missing date or a missing contract on a date means no observation was recovered. Do not invent or interpolate a curve.

## Curves

### `nordic-sys` — Nordic system-price futures

- Unit: EUR EUR/MWh
- Earliest date: 2026-09-01
- Latest date: 2026-11-10
- Dates archived: 51
- Weekday gaps: none between earliest and latest
- Sources present: eex

### `ttf` — Dutch TTF gas futures

- Unit: EUR EUR/MWh
- Earliest date: 2026-09-01
- Latest date: 2026-11-10
- Dates archived: 51
- Weekday gaps: none between earliest and latest
- Sources present: eex

### `brent` — ICE Brent crude futures

- Unit: USD USD/bbl
- Earliest date: 2026-11-11
- Latest date: 2026-11-11
- Dates archived: 1
- Weekday gaps: none between earliest and latest
- Sources present: tradingview_scanner

### `ho` — NY Harbor ULSD (heating oil) futures

- Unit: USD USD/gal
- Earliest date: 2026-10-27
- Latest date: 2026-11-11
- Dates archived: 14
- Weekday gaps: none between earliest and latest
- Sources present: tradingview_scanner, tradingview_scanner_history


## Sources and caveats

**Nordic system-price futures (`nordic-sys`) and TTF (`ttf`).** Official EEX settlements from `MasterKano/scrape` `master/eex_master.csv`, which itself starts on 2026-09-01. All listed months, quarters and years are kept; TTF also keeps listed seasons (`Winter-YY`). In-delivery contracts that still have a settlement that day are included. Rows with no settlement price are dropped (for example a front TTF month may already have stopped settling). The scrape workflow runs weekdays at 09:00 UTC and can lag the exchange by a session; this archive writes a date only when that trade date is in the master (or in a local scrape run). Free sources do not provide EEX history before 1 September 2026; Nasdaq and EEX sell the earlier tape.

**NY Harbor ULSD (`ho`).** Live snapshots come from the TradingView NYMEX HO scanner (the same public source as `scripts/ho_curve.py`). Historical dates from 2026-10-27 are backfilled from `data/ho_curve_history.csv` on the `live-data` branch. Those history rows are **session last at fetch time**, often mid-session (~15:00–16:00 UTC), not the official NYMEX settle. Two dates (2026-11-02 and 2026-11-09) have a shorter listed strip (~27 months instead of ~38). The history file also has Saturday snapshots on 2026-10-31 and 2026-11-07, which are kept. Official CME settlement pages refuse this environment (HTTP 403). Yahoo per-contract history was checked for backfill before 27 October 2026, but expired front months are gone or return unusable bars past expiry, so publishing those dates would have produced a curve missing the front. They were not written.

**ICE Brent (`brent`).** Live snapshots from the TradingView ICEEUR BRN scanner (the official ICE Brent contract, not the CME last-day financial lookalike). Prices are delayed last, not official ICE daily settlements (ICE delayed settlement pages are also blocked from this environment). There was no existing all-months Brent curve archive to backfill; Yahoo `BZx.NYM` history cannot reconstruct a complete historical ICE curve once front months have expired. Shallow history starts with the first successful snapshot.

**Do not use the `live-data` branch for this archive.** That branch is an orphan, force-pushed on every price refresh, and would erase curve history.

## Daily command

Run on the box, after settlements. The script is idempotent and only refreshes a date when the new file is as complete or better.

```
python3 /home/box/data/curves/snapshot.py --fetch \
  --eex-csv https://raw.githubusercontent.com/MasterKano/scrape/main/master/eex_master.csv \
  --out /home/box/data/curves/output
```

Recommended schedule (weekdays, Europe/Zurich):

1. **22:00 Europe/Zurich** (21:00 UTC in winter) — primary run, after ICE Brent and NYMEX HO settlements (~20:30 Zurich in winter). Writes `ho` and `brent` for that session. EEX Nordic/TTF for the same day are often not in the scrape master yet.
2. **10:30 Europe/Zurich** (09:30 UTC in winter) — catch-up after the MasterKano/scrape job at 09:00 UTC, which is when official EEX Nordic and TTF settlements usually appear. Refreshes `nordic-sys` and `ttf` for the previous session without shrinking already-written HO/Brent files.

If only one slot is available, use 22:00 and accept that EEX dates land on the next weekday's run. Copy the new files into `MasterKano/emmi-data` `curves/` on `main` (open a PR or fast-forward) so the public archive stays current. Do not publish this folder to `live-data`.

To rebuild from local caches only (no live fetch):

```
python3 /home/box/data/curves/snapshot.py \
  --eex-csv /home/box/data/curves/cache/eex_master.csv \
  --ho-history /home/box/data/curves/cache/ho_curve_history.csv \
  --out /home/box/data/curves/output
```

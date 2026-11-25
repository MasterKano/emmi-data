# WP02 — Nordic climate migration

Draft only. Nothing on `main` was published, Mark's routine was not changed,
and `nordic-climate/` on `main` is byte-for-byte the files that were already
there. `mr-climate` was not touched.

Evidence commit: `fba8a0585ca01d425ed90fd7d48908fd7df2bb4b`.
This report is a later commit on the same branch. The pipeline code those
runs executed is that SHA.

## What was built

A port of `production/nordic-climate/` from `newsletter-archive` `handover`
`edc94177` into `scripts/nordic-climate/` on `emmi-data`, plus five workflows.

| Workflow | Permission | Result on the evidence commit |
|---|---|---|
| `.github/workflows/nordic-climate-dry-run.yml` | `contents: read` | success |
| `.github/workflows/nordic-climate-live.yml` | `contents: read` | success |
| `.github/workflows/nordic-climate-seed.yml` | `contents: read` | failure, missing secret (intended) |
| `.github/workflows/nordic-climate-failure-probe.yml` | `contents: read` | failure while `FAILURE_PROBE` existed (intended) |
| `.github/workflows/nordic-climate-publish.yml` | `contents: write`, `actions: write` | did not run |

Publishing is off in two independent ways:

1. The four cron lines (Saturday and the 11th, each at both 03:05 and 04:05 UTC
   so 05:05 Europe/Zurich is hit in CEST and in CET) are commented, so GitHub
   has not registered a schedule.
2. The publish job is skipped unless the repository variable
   `NORDIC_CLIMATE_PUBLISH` is exactly `1`. That variable is not set.

`schedule_window.py` drops the UTC hour that is not 04:50–05:20 in Zurich, so
the off-season hour does not publish and does not fail. A manual dispatch is
not tied to the clock. Unchanged files do not create a commit. The committer,
once publishing is enabled, is `github-actions[bot]`.

How to turn it on, the single-publisher cutover, and rollback are in
`scripts/nordic-climate/OPERATIONS.md`. Do not enable this while Mark's
routine is still authorised to push.

## Monthly CSVs are enough

`update.om_month` returns `om_YYYY_MM.csv` when the file exists and does not
open `hourly_raw/`. `load_point_cache` and `month_complete` read only those
CSVs. `hourly_raw/` is a resume cache inside `om_fetch`, used only when the
month CSV is absent. `cache/openmeteo` is the retired 10 m tree and is not
read. The restore script extracts only `om_YYYY_MM.csv`.

The durable seed stays the private release
`MasterKano/newsletter-archive` tag `nordic-climate-seed-20261125`
(tarball 147,831,212 bytes, GitHub digest and pinned sha256
`7df4f51d980d894a8fee8b0c3c1aa4ecd09cdac1d1ada24d060c744588a48026`).
It is not committed here. The Actions cache is a separate evictable copy
(10 GB per repo, unused entries drop after 7 days) and is written only by
the publish workflow, which is disabled. Dry runs do not upload the cache:
artifacts on this public repo are world-readable.

This environment's git token cannot download that private asset (the release
is visible to the user's GitHub connection, and the shell token is not).
The extract layout is therefore checked in unit tests against a stand-in
tarball, and on Actions it fails closed until the secret below exists.
Once the secret is set, the seed workflow checks the real span
`om_1940_01.csv` through `om_2026_10.csv` and refuses anything that is not
a monthly CSV.

## Runs

Hosted runner, pull request #29. `workflow_dispatch` is in the files but
GitHub only offers it from the default branch, so these are `pull_request` runs.

| Run | URL | Conclusion |
|---|---|---|
| Dry run (unit tests + fixture) | https://github.com/MasterKano/emmi-data/actions/runs/45556507910 | success |
| Live Open-Meteo overlap | https://github.com/MasterKano/emmi-data/actions/runs/45556507774 | success |
| Seed restore | https://github.com/MasterKano/emmi-data/actions/runs/45556507801 | failure: `NORDIC_CLIMATE_SEED_TOKEN` missing |
| Forced failure probe | https://github.com/MasterKano/emmi-data/actions/runs/45556507798 | failure, exit 1, Actions error annotation |

An earlier seed run (https://github.com/MasterKano/emmi-data/actions/runs/45556383688)
was cancelled in 2 seconds because three workflows shared one concurrency
group and GitHub drops an older pending run in that group. They now have
separate groups (`nordic-climate-dry-run`, `nordic-climate-live`,
`nordic-climate-seed`, `nordic-climate-publish`), each with
`cancel-in-progress: false`.

Artifacts:

- `nordic-climate-fixture` (132,741 bytes) on the dry run. Contains the five
  compact files, `daily_vs_monthly_sanity.csv`, `daily_vs_monthly_sanity.json`,
  and `run_record.json`. Fixture clock 2026-11-21, coverage 1940-01-01 to
  2026-11-21, 31,737 days, `gaps: []`, `publish_enabled: false`.
- `nordic-climate-live-overlap` (561 bytes) on the live run.

The fixture series is synthetic. It proves the hosted runner installs
dependencies, builds a complete cache, and writes the five files. It is not
a numeric match to production, and it was not published.

## Comparison with the published files

Live run on 2026-11-25, artifact `nordic-climate-live-overlap`, and the same
check on this machine before the push:

- Rebuilt 2026-10-01 through 2026-11-15 (the recent end of emmi-data
  `540c0178`) from Open-Meteo ERA5 with the vendored calibration.
- 46 overlapping days, 0 mismatches at 2 decimal places.
- Freshness probe day 2026-11-19 (`today` minus 6) was present as a complete
  UTC day.
- `nordic-climate/` on `main` was not modified. `git diff origin/main -- nordic-climate/`
  is empty.

A full 1940-to-present rebuild on Actions, which is what would refresh
`mtd.json` and `monthly.csv` from the seed, has not run. It needs
`NORDIC_CLIMATE_SEED_TOKEN`. The local synthetic rebuild (same code, 22
seconds, 1,042 complete months) did write all five files and the sanity
output. Wind in that sanity file matches the daily series (rmse 0).
Temperature and rain do not, because the fixture weather is not ERA5 while
`monthly.csv` still keeps the real workbook columns. That is expected and
is not a calibration change. The vendored `calibration.json` still has
temperature calibrated RMSE 0.170 and rain 0.076, and `wind100` slope 1,
intercept 0.

## Tests

`python -m pytest -q scripts/nordic-climate/tests` — **24 passed** locally
and on the hosted dry run (1.66s).

Covered negatives:

- interior gap from 1940 fails the contiguous check
- 1991–2020 missing a day fails normals
- a complete series that ends in 2020 passes the gap check and fails freshness
- lag of 6 days passes, 7 fails, a future end fails
- a partial month is not written as a complete cache file
- an hourly response with a null hour is not gzip-cached
- a connection timeout is retried
- numpy int64 climatology values dump as JSON
- unchanged files produce no commit
- a changed file commits as `github-actions[bot]`
- publishing with the variable unset, or with a stale or gapped series, does not commit
- the legacy Kano publisher refuses to run
- a second process holding the run lock fails
- Zurich Saturday 05:05 in January (CET) and July (CEST) is in window; the other UTC hour is not; 10 Dec is not; 11 Dec 05:05 CET is
- seed extract keeps only `om_YYYY_MM.csv`; a bad sha256 and a missing token fail
- dry-run workflows are `contents: read`; only the publish workflow has `contents: write`; crons are commented

## Credentials and config still required

Names only.

| Name | Kind | State |
|---|---|---|
| `NORDIC_CLIMATE_SEED_TOKEN` | Actions secret. Fine-grained PAT, Contents read-only, repository `MasterKano/newsletter-archive` only | **not set. Jerry has to add it. Nothing here created a token.** |
| `NORDIC_CLIMATE_PUBLISH` | Actions variable, value `1` | **must stay unset until cutover** |

No CDS key, no Open-Meteo key, no mail secret.

## Costs

No new paid service. Public-repo standard GitHub-hosted runners are free.
Artifacts are the compact files (well under a megabyte) with 14-day retention.
The monthly CSV cache, once the publish workflow is allowed to write it, is
on the order of 100 MB against the 10 GB per-repo cache allowance. No larger
runner. An incremental Open-Meteo fetch is a handful of calls. A cold
1940 rebuild is what the seed is for; it is not the routine path.

## Licensing

Open-Meteo API data are CC BY 4.0. Attribution stays with the displayed
figures: weather data by Open-Meteo.com (https://open-meteo.com/). Upstream
ERA5 is Copernicus Climate Change Service / ECMWF.

The free API tier is published as non-commercial, with limits of 10,000
calls/day, 5,000/hour and 600/minute (https://open-meteo.com/en/terms, read
25 Nov 2026). This migration calls the same unauthenticated endpoint the box
already uses and does not add a key. Whether EMMi's use is commercial is
Jerry's decision. A paid plan would be that pre-existing terms question, not
a new architecture and not a cost this PR introduces. The raw cache is not
republished.

## Freshness lag

Nominal lag 5 days, from the Open-Meteo Historical Weather API data-sources
table ("ERA5 … Daily with 5 days delay") and the same sentence in the
published `mtd.json`. Ceiling used by the code: 6 days, because emmi-data
`540c0178` (21 Nov 2026) ended on 2026-11-15. A hole-free series older than
that fails the job. The constant and the citation live in
`scripts/nordic-climate/freshness.py`.

## Risks

- Until `NORDIC_CLIMATE_SEED_TOKEN` exists, a runner with an empty cache
  cannot rebuild 1940–present. The live overlap check does not need the seed.
  Add the secret before relying on this Action for 11 Dec.
- The real tarball's internal paths have not been listed from this
  environment. The seed job fails closed if they are not
  `openmeteo100m/om_YYYY_MM.csv`. The first successful seed run is that check.
- GitHub-hosted runners share egress. Open-Meteo may be slower or stricter
  there than on the box. Connection timeouts are retried. A hard block fails
  the job rather than being bypassed.
- ERA5T can be revised when the final ERA5 month is released. The 46-day
  overlap matched on 25 Nov 2026. A later revision would show up as a
  mismatch and fail the run, which is the right alarm.
- The daily IFS top-up still reads the box copies. After the bot stops, those
  copies freeze unless something refreshes them. See below.
- Two publishers at once are avoided by leaving this Action unable to push
  until Jerry has retired the bot. Do not reverse that order.

## IFS top-up (not implemented)

Leave it on the box. It does not publish to emmi-data.

`production/brief/lib/nordic_topup.py` reads `/home/box/data/nordic-climate/`:
`mtd.json`, `monthly.csv` (also `lib/nordic_windspeed.py`), `daily_grid_mean.csv`
(ERA5 wind on the overlap is replaced by that canonical series),
`calibration.json` (rain only; wind stays raw 100 m), and it writes
`topup/<month>_100m_run<date>.json`. The fetch is Open-Meteo `ecmwf_ifs` on
the same 72-point grid. It does not use the monthly point cache.

Recommendation: after the Action is the publisher, a box job pulls the five
compact files and the vendored `calibration.json` back onto
`/home/box/data/nordic-climate/`. Keep the IFS fetch and `topup/` on the box.
Do not put IFS into the ERA5 workflow.

## Recovery

1. Delete `NORDIC_CLIMATE_PUBLISH` or set it to anything but `1`.
2. Comment the four cron lines if they were uncommented.
3. Jerry resumes Mark's routine. The box scripts, cache, and `gh` login were
   not changed by this work.
4. Revert a bad `nordic-climate/` commit on `main` only if a published file
   is wrong. Do not delete the cache on the box.

## Readiness for 11 Dec 2026

The Action can be reviewed now. It is not the publisher. The bot remains the
production path, which is what the 11 Dec deadline requires if this is not
yet accepted. Before any cutover: add `NORDIC_CLIMATE_SEED_TOKEN`, re-run the
seed workflow so the real tarball is verified, then follow OPERATIONS.md so
only one system is authorised to push.

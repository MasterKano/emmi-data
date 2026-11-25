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

The monthly CSVs are not stored in this public repo's Actions cache and are
not uploaded as artifacts. A token on emmi-data must not be able to read
`newsletter-archive`. The recommended store is a new private repo
`MasterKano/nordic-climate-seed` and a fine-grained read-only token for that
repo only. Details are in the review section below and in
`scripts/nordic-climate/OPERATIONS.md`. The old mixed tarball remains on
`newsletter-archive` tag `nordic-climate-seed-20261125` (147,831,212 bytes,
sha256 `7df4f51d980d894a8fee8b0c3c1aa4ecd09cdac1d1ada24d060c744588a48026`)
and is not what the workflow reads.

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

The hosted 1940-present rebuild is the pull-request workflow
`nordic-climate-history.yml`. It uses the public seed and no token. The
local synthetic rebuild (same code, 22
seconds, 1,042 complete months) did write all five files and the sanity
output. Wind in that sanity file matches the daily series (rmse 0).
Temperature and rain do not, because the fixture weather is not ERA5 while
`monthly.csv` still keeps the real workbook columns. That is expected and
is not a calibration change. The vendored `calibration.json` still has
temperature calibrated RMSE 0.170 and rain 0.076, and `wind100` slope 1,
intercept 0.

## Tests

`python -m pytest -q scripts/nordic-climate/tests` — **26 passed** locally
and on the hosted dry run
https://github.com/MasterKano/emmi-data/actions/runs/45563079765 (1.40s).

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
- a late runner the same Zurich day still publishes; a time before 04:50 does not; a second run the same Zurich date is skipped; 11 July 2026 (Saturday and the 11th) does not publish twice; 10 Dec does not run
- seed extract keeps only `om_YYYY_MM.csv`; a bad sha256 and a missing token fail
- dry-run workflows are `contents: read`; only the publish workflow has `contents: write`; crons are commented

## Credentials and config still required

Names only.

| Name | Kind | State |
|---|---|---|
| `NORDIC_CLIMATE_SEED_TOKEN` | Not used. The seed release is public. | Do not add it. |
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

- The monthly seed is the public release. No token is required to restore
  it. `newsletter-archive` stays private and is not read.
- The first successful seed run is also the check that the slim tarball's
  paths are `openmeteo100m/om_YYYY_MM.csv`. The job fails closed otherwise.
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

`scripts/nordic-climate/sync_box_copy.py` is that copy. It is not deployed.
`--dry-run` lists the public raw URLs and writes nothing. Keep the IFS fetch
and `topup/` on the box. Do not put IFS into the ERA5 workflow.

## Recovery

1. Delete `NORDIC_CLIMATE_PUBLISH` or set it to anything but `1`.
2. Comment the two cron lines if they were uncommented.
3. Jerry resumes Mark's routine. The box scripts, cache, and `gh` login were
   not changed by this work.
4. Revert a bad `nordic-climate/` commit on `main` only if a published file
   is wrong. Do not delete the cache on the box.

## Review corrections (provisional acceptance)

ChatGPT's review is applied on this branch. Publishing stays disabled.
`nordic-climate/` on `main` is still untouched. Mark's routine and
`mr-climate` were not changed. No token, repo, or public copy was created.
No provider was purchased.

### Cache access

Giving emmi-data a credential for all of private `newsletter-archive` is the
wrong scope. That repo is not only this cache.

(a) Dedicated repo `MasterKano/nordic-climate-seed`, containing only the
monthly `om_YYYY_MM.csv` files. Jerry later made this repo public, which is
allowed by the licences below. Restore downloads that release with no token.
The runner still discards the files with the job. There is no Actions cache
of the point files.

(b) A public copy of those monthly CSVs was assessed and not made. Open-Meteo
CC BY 4.0 allows redistribution of the data with attribution. The Copernicus
licence allows redistribution of ERA5 with attribution and a note that the
product was adapted. The files are weather reanalysis, not a customer list.
The existing tarball's own text says "Not for public redistribution" and it
also contains `hourly_raw/` and the 10 m cache, so that file stays private.
A later public release of only the monthly CSVs, with both attributions,
would need Jerry's approval. It is not required for the private-repo option.

Jerry approves and creates: the private repo, the slim release, the scoped
token, and (before `NORDIC_CLIMATE_PUBLISH=1`) the Actions environment
`nordic-climate-publish` with himself as a required reviewer.

### Schedule and publish guard

Commented crons are `5 5 * * 6` and `5 5 11 * *` with `timezone: Europe/Zurich`.
A runner that starts after 05:05 the same Zurich day still publishes. A
second fire that day sees `mtd.json` `generated` already equal to that date
and exits 0. Times before 04:50 Zurich are skipped.

The publish job runs only when `github.ref` is `refs/heads/main`,
`NORDIC_CLIMATE_PUBLISH` is `1`, and either the event is `workflow_dispatch`
or the event is `schedule` and `NORDIC_CLIMATE_CUTOVER_DONE` is `1`. A
schedule with cutover unset is skipped and does not enter
`nordic-climate-publish`. A manual run before cutover uses
`nordic-climate-cutover`. The checkout is `main`. The push step refuses a
HEAD that is not `main`.

### Full history

The hosted job that rebuilds 1940-present and compares every overlapping
day is the second step of `nordic-climate-seed.yml`. It does not run until
the private seed repo and token exist. It uploads
`nordic-climate-history-comparison` only (counts and mismatches, not the
point files).

The public-API rebuild on this machine finished without the private seed
and without writing anything into the repo. Open-Meteo ERA5, 1940-01-01
through 2026-11-19 (31,735 days). Compared with the published files from
`540c0178`:

- 31,731 overlapping days, 0 mismatches, maximum absolute difference 0 for
  wind100, temp and rain.
- No published day missing. 1940-01-01 rain still blank.
- Four new days only: 2026-11-16, 2026-11-17, 2026-11-18, 2026-11-19.
  Nothing on a shared date changed, so there is no ERA5 revision to record
  on this overlap.
- `daily_normals.csv` and `README.md` are byte-identical (48,107 and 5,202
  bytes).
- `monthly.csv`: 1,042 overlapping months, maximum absolute difference 0.
- Calibration-window sanity on the rebuild: wind100 bias 0, temperature
  RMSE 0.170, rain RMSE 0.076.

The point files stayed in `/tmp` and were not committed, cached, or uploaded.
The hosted equivalent is still the second step of `nordic-climate-seed.yml`,
which does not run until Jerry creates the private seed repo.

### Box sync and failure mail

`scripts/nordic-climate/sync_box_copy.py` copies the five published files and
`calibration.json` from public raw URLs onto the box path the IFS top-up
reads. `--dry-run` writes nothing. It is not deployed.

Scheduled-failure mail goes to the GitHub user who last edited the cron
(https://docs.github.com/en/actions/monitoring-and-troubleshooting-workflows/notifications-for-workflow-runs).
That user must have Actions email enabled at
https://github.com/settings/notifications. This work confirmed a failed
check and an Actions error annotation. It could not read MasterKano's inbox,
so email delivery is a setting Jerry confirms, not a fact this run proved.

### Licensing

CC BY 4.0 is the licence on the data (share and adapt, including
commercially, with attribution). The free API terms are the contract for
calling the service without a subscription, and they exclude commercial use
(10,000/day, 5,000/hour, 600/minute). Compliant alternatives, none adopted:
an Open-Meteo paid API plan; Copernicus CDS, which `mr-climate` already uses
for a different 10 m product; or self-hosted Open-Meteo. `mr-climate` was
not modified.

## Seed repo, pin, timezone, and the box sync (Jerry approved)

Private repo https://github.com/MasterKano/nordic-climate-seed (stays private).

| Item | Value |
|---|---|
| Git commit | `224bc27102e954a83d67c370874507b344e39b2e` (`README.md`, `LICENCE.md` only) |
| Release | tag `nordic-climate-seed-20261125` |
| Asset | `nordic-climate-seed-20261125.tar.gz`, 22,538,558 bytes |
| Sibling | `nordic-climate-seed-20261125.sha256` |
| Pinned sha256 | `09f4947c8062373b9a01869cbd1690dd98a93a43131cba304d92d5cbbbaebbb7` |
| Contents | 1,042 files, `cache/openmeteo100m/om_YYYY_MM.csv`, 1940-01 through 2026-10 |

A second `gh release download` matched the pin. The tarball has no hourly
files and no calibration. `restore_seed.PINNED_SHA256` is that digest;
restore exits if the file, the sibling checksum, and the pin disagree.

GitHub documents IANA `timezone` on `schedule` (workflow syntax page, read
25 Nov 2026). DST spring-forward moves a schedule that lands in the skipped
hour to the next valid time. The commented crons are `5 5 * * 6` and
`5 5 11 * *` with `timezone: Europe/Zurich`. 05:05 is outside the skipped
hour, so CET and CEST do not need two UTC lines. Late-runner and same-day
dedup stay in `schedule_window.py`.

The seed release is public. Restore downloads it with no token. The history
rebuild is a pull-request workflow, read-only, and does not use an
environment or a secret. Its run URL is recorded after this commit’s CI.

IFS sync evidence is in `OPERATIONS.md`. Six files were written to
`/tmp/ifs-sync-test` and matched the workspace by sha256.
`/home/box/data/nordic-climate` was not created.

### Remaining actions for Jerry

No seed token. The release is public. Environments already exist and stay
as they are: `nordic-climate-cutover` (reviewer MasterKano, `main` only) and
`nordic-climate-publish` (no reviewer, `main` only). Both variables stay
unset. Crons stay commented.

Cutover, still later and not part of merging this PR: retire Mark's routine,
set `NORDIC_CLIMATE_PUBLISH` to `1`, approve the one manual run (environment
`nordic-climate-cutover`), set `NORDIC_CLIMATE_CUTOVER_DONE` to `1`, then
uncomment the two Zurich crons. A schedule event requires
`NORDIC_CLIMATE_PUBLISH=1`, `NORDIC_CLIMATE_CUTOVER_DONE=1`, and
`refs/heads/main`. With cutover unset the schedule job is skipped and does
not enter `nordic-climate-publish`. Confirm Actions email for the account
that edits the cron.

Merging this PR does not set either variable and does not register a
schedule, so it does not publish.

## Readiness for 11 Dec 2026

The Action can be reviewed now. It is not the publisher. The bot remains the
production path, which is what the 11 Dec deadline requires if this is not
yet accepted. No seed token is required. Follow OPERATIONS.md so only one
system is authorised to push, and only after `NORDIC_CLIMATE_CUTOVER_DONE`
is set.

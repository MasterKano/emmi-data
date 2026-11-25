# Nordic climate on GitHub Actions

This is the cutover and operations note for the monthly/weekly Nordic ERA5 archive
published at `nordic-climate/` on `main`. It does not change `mr-climate`, the
daily IFS top-up, or `nordic-weather.yml`.

Mark's Grok Bot routine is still the only system authorised to publish.
Nothing in this tree pushes to `main` until the steps below are done, in order.

## What runs

| Workflow | Permission | When it runs | Publishes |
|---|---|---|---|
| `nordic-climate-dry-run.yml` | `contents: read` | pull request, `workflow_dispatch` | no |
| `nordic-climate-live.yml` | `contents: read` | pull request, `workflow_dispatch` | no |
| `nordic-climate-seed.yml` | `contents: read` | pull request, `workflow_dispatch` | no |
| `nordic-climate-failure-probe.yml` | `contents: read` | only while `FAILURE_PROBE` exists | no |
| `nordic-climate-publish.yml` | `contents: write`, `actions: write` | `workflow_dispatch`, and the crons **after they are uncommented** | only if `NORDIC_CLIMATE_PUBLISH` is `1` and the Zurich window says yes |

`workflow_dispatch` is listed on the branch workflows. GitHub only shows that
button for workflow files on the default branch, so evidence runs use
`pull_request`.

Each workflow has its own concurrency group (`nordic-climate-dry-run`,
`nordic-climate-live`, `nordic-climate-seed`, `nordic-climate-publish`) with
`cancel-in-progress: false`. A second run of the same workflow waits. GitHub
cancels an older *pending* run in a shared group, so the workflows do not
share one group. The publish group is the one that matters after cutover.
`publish_outputs.py` also takes a file lock on one machine.

## Schedule

Target clock: **05:05 Europe/Zurich**, on **Saturday** and on **the 11th**
(the morning after `mr-climate`, which runs at 06:00 UTC on the 10th).

GitHub cron is UTC. Zurich is CET (UTC+1) from the last Sunday in October to
the last Sunday in March, and CEST (UTC+2) otherwise. 05:05 local is outside
the DST transition hour, so it exists once. The publish workflow therefore
has both UTC hours, commented out:

```yaml
# schedule:
#   - cron: "5 3 * * 6"   # 05:05 CEST Saturday
#   - cron: "5 4 * * 6"   # 05:05 CET Saturday
#   - cron: "5 3 11 * *"  # 05:05 CEST on the 11th
#   - cron: "5 4 11 * *"  # 05:05 CET on the 11th
```

`schedule_window.py` continues only when the UTC instant is 04:50–05:20 in
Zurich and the local date is a Saturday or the 11th. The other hour exits 0
and does not publish, so it does not send a failure notification. A manual
`workflow_dispatch` is not tied to the clock.

If the 11th is a Saturday, both crons fire. The second run waits, finds no
file changes, and does not create an empty commit.

## How to enable publishing

Do this only after the dry-run evidence in `WP02_REPORT.md` has been accepted,
and only so that **one** system is allowed to write `nordic-climate/` on `main`.

1. Add the repository secret named below. Do not create it from this repo's
   workflows; a person creates it in the GitHub UI.
2. Confirm Mark's routine will not run again (Jerry retires or pauses it).
   Until that is true, do not do steps 3–5.
3. Set the repository variable `NORDIC_CLIMATE_PUBLISH` to `1` on
   `MasterKano/emmi-data`. This arms `.github/workflows/nordic-climate-publish.yml`.
   The crons are still commented, so nothing runs on a clock.
4. From the Actions tab, run **Nordic climate publish** with "Commit the five
   compact files" unticked. That is a no-publish rebuild on the hosted runner.
   Then run it again with the box ticked for the first real commit, as
   `github-actions[bot]`. Compare the commit with the previous `nordic-climate/`
   files before leaving it.
5. In a follow-up commit, uncomment the four `cron` lines and the `schedule:`
   key in `nordic-climate-publish.yml`.

Both the variable and the uncommented crons are required. Either one alone
does not publish on a schedule. A dispatch with the box unticked never commits.

To turn publishing back off without deleting the workflow: delete
`NORDIC_CLIMATE_PUBLISH` (or set it to anything other than `1`) and comment
the crons again. The job is skipped when the variable is not `1`.

## Credential Jerry must add

Name: `NORDIC_CLIMATE_SEED_TOKEN`

- Fine-grained personal access token.
- Resource: only `MasterKano/newsletter-archive`.
- Permission: Contents, Read-only.
- No other permissions. Do not grant write, Actions, or administration.
- Stored as an Actions secret on `MasterKano/emmi-data`. It is not committed.

The public `GITHUB_TOKEN` cannot read that private release. No token was
created by this work. Until the secret exists, `nordic-climate-seed.yml`
fails on purpose and says so in the job summary. Dry runs that do not need
the historical cache do not use the secret.

`NORDIC_CLIMATE_PUBLISH` is a repository **variable**, not a secret. It is
absent now. Leave it unset until step 3 above.

No `CDSAPI_URL`, `CDSAPI_KEY`, Open-Meteo key, or mail secret is required.

## Cache

The five published files are rebuilt from the monthly point CSVs
`cache/openmeteo100m/om_YYYY_MM.csv`. Those files are what
`load_point_cache` and `month_complete` read. `hourly_raw/` is only a resume
cache for an interrupted hourly download of a month that is not already a
CSV. `cache/openmeteo` is the retired 10 m tree and is not read by this path.
The restore script extracts only `om_YYYY_MM.csv` and checks sha256.

The durable copy is the private release
`MasterKano/newsletter-archive` tag `nordic-climate-seed-20261125`:

- `nordic-climate-seed-20261125.tar.gz` (147,831,212 bytes)
- pinned sha256 `7df4f51d980d894a8fee8b0c3c1aa4ecd09cdac1d1ada24d060c744588a48026`
- sibling `.sha256` asset, checked against the same digest

The Actions cache is a separate, evictable copy (10 GB per repository, entries
not accessed for 7 days are removed). The publish workflow restores that
cache and, if `om_1940_01.csv` is missing, downloads the seed. Dry-run
workflows do not write the cache (`actions: write` is only on the publish
workflow). The raw cache is never committed and is not uploaded as an
artifact, because artifacts on this public repo are world-readable.

The original box copy under `/home/box/data/nordic-climate/` stays in place
for rollback.

## Freshness

A series can be internally complete and still be stale. After the existing
1940-to-latest gap check, the run refuses an end date older than 6 days.

- Nominal lag: 5 days. Open-Meteo Historical Weather API, data-sources table:
  ERA5 is "Daily with 5 days delay"
  (https://open-meteo.com/en/docs/historical-weather-api). The same figure is
  in the published `mtd.json` note.
- Ceiling: 6 days. The 21 Nov 2026 publish `540c0178` ended on 2026-11-15.
- Source string is `freshness.LAG_SOURCE` in `scripts/nordic-climate/freshness.py`.

## Failures

Success does not send a message. A validation error, a missing seed when a
restore was required, a freshness breach, or a push failure exits non-zero
and writes a job summary. GitHub then marks the run failed, which is the
notification Grok Bot watches. There is no mail secret on this workflow.

## Commit identity

Publishing commits are `github-actions[bot]`
`<41898333+github-actions[bot]@users.noreply.github.com>`, subject
`Update Nordic climate daily files`. The legacy box publisher
(`Kano Jigoro`) stays in `update.py` but refuses to run unless
`NORDIC_CLIMATE_ALLOW_LEGACY_PUBLISH=1`. Do not set that on Actions.

Unchanged files do not create a commit.

## Cutover

Before cutover the bot is the only publisher. This Action cannot publish.

At cutover, in this order:

1. Jerry confirms the bot routine will not push `nordic-climate/` again.
2. Set `NORDIC_CLIMATE_PUBLISH=1`.
3. One manual publish, reviewed.
4. Uncomment the four crons.

After that, the Action is the only publisher. Do not turn the bot back on
while the variable is `1` and the crons are active.

## Rollback

1. Delete `NORDIC_CLIMATE_PUBLISH` or set it to a value other than `1`.
2. Comment the four cron lines again (a one-line revert of the enable commit).
3. Ask Jerry to resume Mark's routine. The box scripts, cache, and `gh`
   login were not changed.
4. The last good `nordic-climate/` commit on `main` remains. Revert that
   commit only if a published file is wrong; do not delete history.

## Daily IFS top-up (not migrated)

Leave it on the box. It does not publish to emmi-data.

It lives in `production/brief/lib/nordic_topup.py` and reads, all under
`/home/box/data/nordic-climate/`:

- `mtd.json` and `monthly.csv` (also read by `lib/nordic_windspeed.py` for the
  Pulse wind and rain tiles)
- `daily_grid_mean.csv` (ERA5 wind on the overlap is replaced by this
  canonical series)
- `calibration.json` (rain only; wind stays raw 100 m)
- `topup/<YYYY-MM>_100m_run<YYYY-MM-DD>.json`, written by the top-up itself

The fetch is Open-Meteo `models=ecmwf_ifs` on the same 72-point grid, hourly
`wind_speed_100m` and daily `precipitation_sum`, bias-corrected against ERA5
on the overlap. It does not use the monthly point cache.

After this archive moves to Actions, those box files stop updating on the day
the bot stops, unless something copies the five compact files back. The
top-up's ERA5 overlap and the climatology in the tiles would then freeze.

Recommendation, not implemented: a small box job, after the Action has
published, does `git pull` in a clone of `emmi-data` and copies the five
`nordic-climate/` files plus the vendored `calibration.json` onto
`/home/box/data/nordic-climate/`. Keep the IFS fetch and `topup/` on the box.
Point `nordic_windspeed.py` at the refreshed copies. Do not fold IFS into the
ERA5 publish workflow: it is a Pulse figure, it uses a different model, and
it must not be committed to emmi-data.

## Costs and licensing

Public `emmi-data` uses standard GitHub-hosted runners, which are free on
public repositories. Artifacts uploaded here are the compact files only
(about 1 MB) with 14-day retention, inside the Free plan's artifact
allowance. The monthly CSV cache is on the order of 100 MB, inside the 10 GB
Actions cache allowance, and only the publish workflow writes it. No larger
runner is used. No new paid API is called.

Open-Meteo API data are CC BY 4.0. Attribution belongs next to displayed
data: weather data by Open-Meteo.com, link to https://open-meteo.com/.
Upstream ERA5 is Copernicus Climate Change Service / ECMWF. The free API
tier is published as non-commercial (10,000 calls/day, 5,000/hour, 600/minute;
https://open-meteo.com/en/terms, read 25 Nov 2026). An incremental run is a
handful of calls. A cold 1940 rebuild is hundreds of calls and is what the
seed avoids. Whether EMMi's existing use of that free tier is commercial is
Jerry's call; this migration does not add a key and does not change the
source the box already uses. A paid Open-Meteo plan would be a pre-existing
terms question, not a new architecture.

The raw cache is not republished. The five compact files stay on the public
repo as they are today.

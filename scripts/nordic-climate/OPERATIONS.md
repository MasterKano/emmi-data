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
| `nordic-climate-publish.yml` | `contents: write` only | `workflow_dispatch`, and the crons **after they are uncommented** | only on `refs/heads/main`, with `NORDIC_CLIMATE_PUBLISH=1`, the environment gate, and the Zurich rule |

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
#   - cron: "5 3 11 * 6"  # 05:05 CEST on Saturday OR the 11th
#   - cron: "5 4 11 * 6"  # 05:05 CET on Saturday OR the 11th
```

GitHub treats a cron with both a day-of-month and a day-of-week as OR, and
fires once when both match. A Saturday that is also the 11th is therefore
one trigger per UTC hour, not two triggers at the same minute.

`schedule_window.py` then:

- skips anything that is not a Saturday or the 11th in Zurich
- skips a time before 04:50 Zurich (the off-season hour arriving early)
- accepts a runner that starts later the same Zurich day, so a delayed
  GitHub VM does not drop that day's run
- skips when `nordic-climate/mtd.json` on main already has `generated` equal
  to that Zurich date, so the second UTC hour does not publish again

A skip exits 0. It is logged and written to the job summary. It does not
send a failure mail. A manual `workflow_dispatch` is not tied to the clock,
and it still cannot publish unless the workflow ref is `refs/heads/main`.

## How to enable publishing

Do this only after the dry-run evidence in `WP02_REPORT.md` has been accepted,
and only so that **one** system is allowed to write `nordic-climate/` on `main`.

1. Create the private seed repo and the scoped token named below. Do not
   create them from this repo's workflows.
2. On `MasterKano/emmi-data`, create the GitHub Actions environment
   `nordic-climate-publish` and add a required reviewer (Jerry) before the
   variable in step 4 is set. The publish job names that environment. If the
   environment does not exist when the job is first allowed to run, GitHub
   can create it with no reviewer.
3. Confirm Mark's routine will not run again (Jerry retires or pauses it).
   Until that is true, do not do steps 4–6.
4. Set the repository variable `NORDIC_CLIMATE_PUBLISH` to `1`. This arms
   the job, and only when the workflow ref is `refs/heads/main`. The crons
   are still commented, so nothing runs on a clock. A dispatch that selects
   another branch is skipped.
5. From the Actions tab on **main**, run **Nordic climate publish** with
   "Commit the five compact files" unticked. That checks out `main` and does
   not push. Then run it again with the box ticked for the first real
   commit, as `github-actions[bot]`. The environment reviewer must approve.
   Compare the commit with the previous `nordic-climate/` files.
6. In a follow-up commit on main, uncomment the two `cron` lines and the
   `schedule:` key in `nordic-climate-publish.yml`.

The variable, the main-branch guard, the environment, and the uncommented
crons are separate. A dispatch from a feature branch cannot push that branch
to main: the job is skipped unless `github.ref` is `refs/heads/main`, the
checkout is `ref: main`, and the push step refuses a HEAD that is not `main`.

To turn publishing back off without deleting the workflow: delete
`NORDIC_CLIMATE_PUBLISH` (or set it to anything other than `1`) and comment
the crons again. The job is skipped when the variable is not `1`.

## Credential Jerry must add

Do not put a token for `MasterKano/newsletter-archive` on this public repo.
That private repo holds more than this cache.

Name: `NORDIC_CLIMATE_SEED_TOKEN`

- Fine-grained personal access token.
- Resource: only the new private repo `MasterKano/nordic-climate-seed`.
- Permission: Contents, Read-only.
- No other permissions. Do not grant write, Actions, administration, or any
  access to `newsletter-archive`.
- Stored as an Actions secret on `MasterKano/emmi-data`. It is not committed.

What Jerry creates, and nothing in this PR creates:

1. A **private** repository `MasterKano/nordic-climate-seed`.
2. One release on it, tag `nordic-climate-seed-20261125`, containing only
   `nordic-climate-seed-20261125.tar.gz` and a sibling `.sha256`. The tarball
   may contain `cache/openmeteo100m/om_YYYY_MM.csv` and nothing else that the
   restore keeps. `hourly_raw/` and `cache/openmeteo` are ignored if present
   and must not be the reason for the repo.
3. The token above, scoped to that repo only.
4. After the first download, set repository variable or secret note
   `NORDIC_CLIMATE_SEED_SHA256` to the digest he published, so later runs pin
   it. Optional until that digest is known. The script already requires the
   tarball to match the sibling `.sha256` file.

No token was created by this work. Until the repo and the secret exist,
`nordic-climate-seed.yml` fails on purpose. Dry runs that do not need the
historical cache do not use the secret.

The existing release on `newsletter-archive` (tag `nordic-climate-seed-20261125`,
147,831,212 bytes, sha256
`7df4f51d980d894a8fee8b0c3c1aa4ecd09cdac1d1ada24d060c744588a48026`) can be the
source Jerry copies the monthly CSVs from. This workflow does not read it.

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

The durable copy Jerry is asked to create is the private repo in the
credential section. It is downloaded into the runner's temp disk for that
job and discarded with the runner.

These workflows do **not** write an Actions cache on public `emmi-data`.
Point-level files are not uploaded as artifacts. Artifacts on a public repo
are world-readable; the comparison JSON is the only history artifact, and it
contains counts and a short mismatch list, not the point cache.

### Can the compact monthly CSVs be public?

Assessed only. Nothing was made public.

- Open-Meteo licences API **data** as CC BY 4.0: share and adapt, including
  commercially, with attribution and a link to https://open-meteo.com/.
  That is a licence on the data, not permission to call the free API.
- ERA5 is a Copernicus product. The Copernicus licence allows reproduction
  and distribution, with attribution of the form "Generated using Copernicus
  Climate Change Service information [year]" and a statement when the product
  was adapted. https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels?tab=download
  points at that licence.
- The monthly CSVs are adapted ERA5 (72-point daily extracts via Open-Meteo,
  not the published grid means). A public copy would need both attributions.
  They are weather reanalysis, not a private customer dataset.
- The existing tarball's release text says "Not for public redistribution",
  and that tarball also holds `hourly_raw/` and the retired 10 m cache. That
  file must stay private. It is not what a public copy would be.

A later public release of **only** `om_YYYY_MM.csv`, with those attributions,
looks lawful under CC BY 4.0 and the Copernicus licence. It is not done here.
Jerry would have to approve it. Until then the simplest secure zero-cost
option is the private single-purpose repo and a token that can read only
that repo, with no Actions cache on emmi-data.

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

## Failures and who is emailed

Success does not send a message. A validation error, a missing seed when a
restore was required, a freshness breach, or a push failure exits non-zero
and writes a job summary. There is no mail secret on this workflow. GitHub's
own notification mail is the channel. A red mark on the Actions page is not
the same thing as mail leaving GitHub.

GitHub's rule for a **scheduled** workflow, from
https://docs.github.com/en/actions/monitoring-and-troubleshooting-workflows/notifications-for-workflow-runs
(read 25 Nov 2026):

- Mail goes to the user who created the workflow.
- If another user edits the `schedule` cron, later mail goes to that user.
- If the workflow is disabled and re-enabled, mail goes to the user who
  re-enabled it.
- A `workflow_dispatch` failure goes to the user who clicked Run.
- The account only receives that mail if Actions notifications are enabled
  at https://github.com/settings/notifications (email on; "failed workflows
  only" is enough) and the address is verified.

A red X on the Actions tab is not that mail. This repo has no setting that
names a recipient. Jerry confirms it like this:

1. The GitHub user who uncomments the cron (that should be MasterKano) checks
   the notification page above.
2. After cutover, trigger one failing run while signed in as that user
   (dispatch the seed workflow with the token absent, or wait for a real
   failure). The mail should arrive. If it does not, the notification page
   is the thing to fix.
3. This build could not read MasterKano's notification settings or inbox.
   The forced-failure run showed a failed check and an Actions error
   annotation. It did not prove an email was delivered.

Grok Bot can still watch the red check. The mail above is the human path.

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
4. Uncomment the two crons.

After that, the Action is the only publisher. Do not turn the bot back on
while the variable is `1` and the crons are active.

## Rollback

1. Delete `NORDIC_CLIMATE_PUBLISH` or set it to a value other than `1`.
2. Comment the two cron lines again (revert the enable commit).
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

The copy script is `scripts/nordic-climate/sync_box_copy.py`. It is not
deployed and it is not installed on the box. It downloads the five published
files plus `scripts/nordic-climate/calibration.json` from the public raw
URLs on `main` into `/home/box/data/nordic-climate/` (override with `--dest`).
`--dry-run` prints the plan and writes nothing. It does not take a token and
does not call Open-Meteo. Run it on the box only after this branch is on
main and Mark's routine has been retired, on a timer that is not the same
minute as the Action, so the top-up sees the new files. Keep the IFS fetch
and `topup/` on the box. Do not fold IFS into the ERA5 publish workflow.

## Costs and licensing

Public `emmi-data` uses standard GitHub-hosted runners, which are free on
public repositories. Artifacts uploaded here are the compact comparison
files with 14-day retention. The point cache is not stored in the Actions
cache and is not an artifact. No larger runner is used. No new paid API is
called. The private seed repo, once Jerry creates it, is an empty GitHub
private repo plus one release; that is inside the free plan.

### Two different Open-Meteo rules

They are not the same permission.

| | What it covers | Commercial use |
|---|---|---|
| CC BY 4.0 data licence (https://open-meteo.com/en/license) | The numbers, once obtained. Share and adapt with attribution. | Allowed by the data licence. |
| Free API terms (https://open-meteo.com/en/terms, read 25 Nov 2026) | Calling `archive-api.open-meteo.com` without a subscription. Limits: 10,000 calls/day, 5,000/hour, 600/minute. | Not allowed. Paid API plans are the commercial path. |

Attribution for displayed figures remains: weather data by Open-Meteo.com,
link to https://open-meteo.com/, and for the ERA5 portion "Generated using
Copernicus Climate Change Service information".

This migration still calls the same unauthenticated endpoint the box already
uses. It does not buy a plan and it does not switch provider.

Compliant alternatives for professional use, none of them implemented:

- An Open-Meteo API subscription (Standard or above), which the terms list
  as the commercial way to call the API. The data remains CC BY 4.0.
- Copernicus CDS directly. ERA5 there is free for any use, including
  commercial, under the Copernicus licence, with attribution. `mr-climate`
  already runs that path (`CDSAPI_URL` / `CDSAPI_KEY` on that repo, cron on
  the 10th, 10 m wind). It is a different product: the workbook is 10 m, not
  this 100 m 72-point series. Reusing CDS for this archive would be a new
  design, not a switch this PR makes. `mr-climate` is unchanged.
- Self-hosted Open-Meteo, which their terms list as the third access mode.
  That is an operational cost and is not set up here.

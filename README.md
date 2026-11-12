# emmi-data

Public price and market data for [emmi](https://github.com/MasterKano/emmi).

This repository stays **public** so the price refresh keeps using free GitHub Actions minutes. It holds the data workflows, the scripts they run, and the published data. It does not hold the site or company research. Those stay in the private emmi repo, behind Cloudflare Access.

## Workflows

| File | What it does |
|---|---|
| `intraday.yml` | Weekdays, every 15 minutes (`:07/:22/:37/:52`, 05–21 UTC). Yahoo quotes only. Updates `data/market.json` on `live-data`. |
| `ticker.yml` | One long-running job that dispatches `intraday.yml` during market hours and queues its own successor. On weekday mornings it dispatches one full `update-data.yml` catch-up if today's build is missing. The hourly schedule only restarts the chain. |
| `update-data.yml` | Weekday 05:30 full, 12:30 and 17:30 power, 21:00 market, weekend 12:30 power, catch-ups 09:45 and 13:45. Publishes `LIVE_PATHS` to `live-data`. |
| `ho-curve.yml` | Weekdays 20:07 UTC. `data/ho_curve.csv` and `data/ho_curve_history.csv` on `live-data`. |
| `ttf-weather.yml` | 08:40, 10:40, 20:40, 22:40 UTC. `data/ttf_weather.json` on `live-data`. |

`scripts/publish_live.sh` writes the orphan `live-data` branch as one force-pushed commit (no history growth). Intraday and the full build share `data/market.json`: a copy replaces the published file only when it is not older (`fundamentals_utc`, then `generated_utc`). Files a run does not own are kept. `ho-curve.yml` and `ttf-weather.yml` push on top of the current tip and retry if the tip moved.

`LIVE_PATHS` in `update-data.yml` is the set of files replaced on each full publish. The site's `live.js` requests the same paths. Keep the two lists in step.

`tracker-history` is a separate append-only branch (fast-forward only) for the project-progression log. The tracker step skips if that branch cannot be read, so the log is never restarted empty.

`data/market.json` on `main` is only the fallback if a job cannot read `live-data`. Jobs do not commit data back to `main`.

## Where the site reads

```
https://raw.githubusercontent.com/MasterKano/emmi-data/live-data/data/market.json
```

Same prefix for the other files under `data/`.

Nordic power v2 reads `nordic-v2/` on `main`. Oil & gas and Mining v2 read `og-mining-v2/` on `main` (see that folder's README).

## Actions secrets

Repository secrets, never committed. Intraday quotes do not use them. Copy the values from the original source (GitHub will not reveal the existing emmi secrets):

- `ENTSOE_API_TOKEN` — ENTSO-E day-ahead, SE4 detail, reservoirs, monthly history
- `FINGRID_API_KEY` — Finland wind (ENTSO-E fallback when unset)
- `FINGRID_API_KEY_SECONDARY` — second Fingrid key
- `GIE_API_KEY` — GIE AGSI+ gas storage (the step skips and keeps the previous file when unset)

Until those secrets exist here, prices still refresh. Power sections that need ENTSO-E or Fingrid fall back or keep prior values. Gas storage stays on the seeded file.


## Nordic climate

Versioned Nordic climate history and 1991–2020 daily normals live in [`nordic-climate/`](nordic-climate/), on `main`. They are not published to the `live-data` branch. The Monthly Nordic climate update routine refreshes that folder.

## Futures curves

Daily futures-curve snapshots (Nordic system-price, TTF, ICE Brent, NY Harbor ULSD) live in [`curves/`](curves/), on `main`, one CSV per curve per settlement date. They are not published to the `live-data` branch: that branch is force-pushed and would erase history. See [`curves/README.md`](curves/README.md) for the schema, sources, gaps and the daily snapshot command.

## Keep this repo public

A private repo would spend paid Actions minutes and would stop anonymous `raw.githubusercontent.com` reads, which is how the site loads prices. Do not add `research/` or any research notes.

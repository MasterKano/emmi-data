#!/usr/bin/env bash
# Publish build output to the orphan branch live-data as ONE commit (force-push, no history growth).
# Usage: publish_live.sh market <file>    -> replaces data/market.json (intraday prices)
#        publish_live.sh data   <dir>     -> replaces the listed data files under data/ (scheduled builds)
# data/market.json is written by both, so it is only replaced by a copy that is not older: newer fundamentals_utc
# (last full/market build) wins, then newer generated_utc. A slow build or a power-only run therefore never puts
# back older prices, and an intraday run started before a full build finished never undoes that build.
# Both workflows use this; the other side's files are kept. Concurrent pushes are handled with
# --force-with-lease on the tip we started from, retried on conflict.
set -euo pipefail
kind=$1 src=$2
url="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
w=$(mktemp -d)
# exit 0 if market file $1 should replace $2 (or $2 is missing/unreadable)
newer_market() { [ -f "$2" ] || return 0; python3 - "$1" "$2" <<'PY'
import json, sys
def k(p):
    try:
        d = json.load(open(p)); return (d.get("fundamentals_utc") or "", d.get("generated_utc") or "")
    except Exception:
        return ("", "")
sys.exit(0 if k(sys.argv[1]) >= k(sys.argv[2]) else 1)
PY
}
for i in 1 2 3 4 5 6; do
  rm -rf "$w"; mkdir -p "$w"; cd "$w"; git init -q -b live-data
  git config user.name "github-actions[bot]"; git config user.email "41898333+github-actions[bot]@users.noreply.github.com"
  base=""
  if git fetch -q --depth 1 "$url" live-data 2>/dev/null; then base=$(git rev-parse FETCH_HEAD); git checkout -q FETCH_HEAD -- . ; fi
  rm -f market.json   # old top-level intraday copy (before 19 Oct 2026); data/market.json is the only market file now
  if [ "$kind" = market ]; then
    if newer_market "$src" data/market.json; then mkdir -p data; cp "$src" data/market.json
    else echo "live-data data/market.json is newer (full build or later prices); not replaced"; exit 0; fi
  else
    for p in $LIVE_PATHS; do
      if [ "$p" = data/market.json ] && [ -e "$src/$p" ] && ! newer_market "$src/$p" "$p"; then echo "kept newer $p"; continue; fi
      rm -rf "$p"; if [ -e "$src/$p" ]; then mkdir -p "$(dirname "$p")"; cp -r "$src/$p" "$p"; fi
    done
  fi
  git add -A
  git commit -q -m "${kind} $(date -u +'%Y-%m-%d %H:%M') UTC" --allow-empty
  lease="--force-with-lease=live-data:${base}"; [ -z "$base" ] && lease="--force"
  if git push -q $lease "$url" HEAD:live-data 2>/dev/null; then echo "published ($kind) on try $i"; exit 0; fi
  echo "live-data moved underneath us, retrying ($i)"; sleep $((i * 3))
done
echo "publish failed"; exit 1

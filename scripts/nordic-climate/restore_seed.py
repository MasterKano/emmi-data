#!/usr/bin/env python3
"""Restore monthly Open-Meteo point CSVs from a dedicated private seed repo.

The token on public emmi-data must not be able to read MasterKano/newsletter-archive.
Jerry creates a separate private repo that contains only the monthly point
CSVs, and a fine-grained PAT with Contents read-only on that repo alone.
Default repo: ``MasterKano/nordic-climate-seed`` (override with
``NORDIC_CLIMATE_SEED_REPO``). This script does not create the repo or the
token, and it never prints the token.

The files are extracted into the runner's temp directory and are not saved
to the public repo's Actions cache. ``hourly_raw/`` and the retired 10 m
cache are not extracted.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import tarfile
from pathlib import Path

import requests

# Dedicated private repo. Not newsletter-archive. Jerry creates it; this
# script does not.
DEFAULT_SEED_REPO = "MasterKano/nordic-climate-seed"
SEED_TAG = "nordic-climate-seed-20261125"
SEED_TARBALL = "nordic-climate-seed-20261125.tar.gz"
SEED_SHA_NAME = "nordic-climate-seed-20261125.sha256"
SECRET_NAME = "NORDIC_CLIMATE_SEED_TOKEN"
# sha256 of nordic-climate-seed-20261125.tar.gz on MasterKano/nordic-climate-seed.
# 1,042 files, cache/openmeteo100m/om_YYYY_MM.csv only, 1940-01 through 2026-10.
# Verified by a second download of the release asset on 25 Nov 2026.
PINNED_SHA256 = "09f4947c8062373b9a01869cbd1690dd98a93a43131cba304d92d5cbbbaebbb7"
API = "https://api.github.com"
MONTH_CSV = re.compile(r"om_\d{4}_\d{2}\.csv\Z")


def seed_repo() -> str:
    return os.environ.get("NORDIC_CLIMATE_SEED_REPO", DEFAULT_SEED_REPO).strip()


def _token() -> str:
    token = os.environ.get(SECRET_NAME, "")
    if not token.strip():
        raise SystemExit(
            f"Missing credential {SECRET_NAME}. Store it as an environment secret, "
            "not a repository secret, on nordic-climate-publish and "
            "nordic-climate-cutover. Fine-grained PAT, Contents read-only, "
            f"repository {seed_repo()} only. Do not grant newsletter-archive. "
            "No token was created here."
        )
    return token.strip()


def _headers(token: str, accept: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "emmi-data-nordic-climate",
    }


def download_release_assets(dest: Path, session: requests.Session | None = None) -> tuple[Path, Path]:
    token = _token()
    sess = session or requests.Session()
    meta = sess.get(
        f"{API}/repos/{seed_repo()}/releases/tags/{SEED_TAG}",
        headers=_headers(token, "application/vnd.github+json"),
        timeout=60,
    )
    if meta.status_code in (401, 403, 404):
        raise SystemExit(
            f"Could not read the private seed release ({meta.status_code}). "
            f"{SECRET_NAME} needs Contents Read-only on {seed_repo()} only."
        )
    meta.raise_for_status()
    assets = {a["name"]: a for a in meta.json().get("assets", [])}
    for name in (SEED_TARBALL, SEED_SHA_NAME):
        if name not in assets:
            raise SystemExit(f"Seed release {SEED_TAG} is missing asset {name}")
    dest.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in (SEED_TARBALL, SEED_SHA_NAME):
        asset = assets[name]
        url = asset["url"]
        out = dest / name
        with sess.get(url, headers=_headers(token, "application/octet-stream"), timeout=300, stream=True) as resp:
            if resp.status_code in (401, 403, 404):
                raise SystemExit(f"Seed asset download failed ({resp.status_code}) for {name}")
            resp.raise_for_status()
            with out.open("wb") as fh:
                for chunk in resp.iter_content(1024 * 1024):
                    if chunk:
                        fh.write(chunk)
        paths[name] = out
    return paths[SEED_TARBALL], paths[SEED_SHA_NAME]


def parse_sha256_file(text: str) -> str:
    """Accept ``<hex>`` or ``<hex>  filename``."""
    line = text.strip().splitlines()[0].strip()
    hexpart = line.split()[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", hexpart):
        raise SystemExit("Seed .sha256 file does not contain a sha256 hex digest")
    return hexpart


def verify_sha256(tarball: Path, sha_file: Path) -> str:
    """The tarball, the sibling .sha256 asset, and PINNED_SHA256 must all match."""
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    published = parse_sha256_file(sha_file.read_text())
    if digest != published or digest != PINNED_SHA256 or published != PINNED_SHA256:
        raise SystemExit(
            "Seed integrity check failed: "
            f"file={digest} sha256_asset={published} pinned={PINNED_SHA256}"
        )
    return digest


def is_monthly_point_csv(name: str) -> bool:
    """True only for openmeteo100m monthly point CSVs. Never hourly_raw or 10 m."""
    norm = name.replace("\\", "/")
    if norm.endswith("/"):
        return False
    if "/hourly_raw/" in f"/{norm}" or norm.endswith(".json.gz"):
        return False
    parts = [p for p in norm.split("/") if p and p != "."]
    if "openmeteo100m" not in parts:
        return False
    # Reject the retired 10 m cache. Its directory is ``openmeteo``, not ``openmeteo100m``.
    if "openmeteo" in parts and "openmeteo100m" not in parts:
        return False
    # A path that contains cache/openmeteo/ (the 10 m tree) must not match via a suffix trick.
    if "/cache/openmeteo/" in f"/{norm}" and "/cache/openmeteo100m/" not in f"/{norm}":
        return False
    return bool(MONTH_CSV.fullmatch(parts[-1]))


def extract_monthly_csvs(tarball: Path, cache_dir: Path) -> list[Path]:
    """Extract only ``om_YYYY_MM.csv`` into ``cache_dir`` (the openmeteo100m directory)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with tarfile.open(tarball, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not is_monthly_point_csv(member.name):
                continue
            dest = cache_dir / Path(member.name).name
            src = tar.extractfile(member)
            if src is None:
                raise SystemExit(f"Could not read {member.name} from the seed")
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            with tmp.open("wb") as fh:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            tmp.replace(dest)
            written.append(dest)
    if not written:
        raise SystemExit("Seed tarball contained no openmeteo100m monthly CSVs")
    leaked = [p.name for p in cache_dir.iterdir() if p.is_dir() or not MONTH_CSV.fullmatch(p.name)]
    if leaked:
        raise SystemExit(f"Seed extract wrote unexpected paths: {leaked[:10]}")
    return sorted(written)


def restore(cache_dir: Path, download_dir: Path | None = None) -> dict:
    download_dir = download_dir or cache_dir.parent / "seed-download"
    tarball, sha_file = download_release_assets(download_dir)
    digest = verify_sha256(tarball, sha_file)
    files = extract_monthly_csvs(tarball, cache_dir)
    names = sorted(p.name for p in files)
    return {
        "sha256": digest,
        "n_monthly_csvs": len(files),
        "first": names[0],
        "last": names[-1],
        "hourly_raw_extracted": False,
        "openmeteo_10m_extracted": False,
    }


def main() -> int:
    cache = Path(os.environ.get("NORDIC_CLIMATE_CACHE", ""))
    if not str(cache):
        raise SystemExit("NORDIC_CLIMATE_CACHE is not set")
    info = restore(cache)
    print(
        f"seed restored: {info['n_monthly_csvs']} monthly CSVs "
        f"{info['first']}..{info['last']} sha256={info['sha256']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

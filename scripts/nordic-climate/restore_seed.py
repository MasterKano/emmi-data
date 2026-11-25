#!/usr/bin/env python3
"""Restore monthly Open-Meteo point CSVs from the public seed release.

The release is on public ``MasterKano/nordic-climate-seed``, tag
``nordic-climate-seed-20261125``. No token is used. ``newsletter-archive``
stays private and is not read.

The tarball must match its sibling ``.sha256`` and ``PINNED_SHA256``.
Only ``cache/openmeteo100m/om_YYYY_MM.csv`` is extracted, into the runner's
temp directory. Nothing is written to the Actions cache.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import tarfile
from pathlib import Path

import requests

DEFAULT_SEED_REPO = "MasterKano/nordic-climate-seed"
SEED_TAG = "nordic-climate-seed-20261125"
SEED_TARBALL = "nordic-climate-seed-20261125.tar.gz"
SEED_SHA_NAME = "nordic-climate-seed-20261125.sha256"
# sha256 of the public release tarball. 1,042 monthly CSVs, 1940-01 through 2026-10.
PINNED_SHA256 = "09f4947c8062373b9a01869cbd1690dd98a93a43131cba304d92d5cbbbaebbb7"
MONTH_CSV = re.compile(r"om_\d{4}_\d{2}\.csv\Z")


def seed_repo() -> str:
    return os.environ.get("NORDIC_CLIMATE_SEED_REPO", DEFAULT_SEED_REPO).strip()


def asset_url(name: str) -> str:
    """Public release URL. No Authorization header is added."""
    return f"https://github.com/{seed_repo()}/releases/download/{SEED_TAG}/{name}"


def download_release_assets(dest: Path, session: requests.Session | None = None) -> tuple[Path, Path]:
    sess = session or requests.Session()
    dest.mkdir(parents=True, exist_ok=True)
    paths = {}
    headers = {"User-Agent": "emmi-data-nordic-climate"}
    for name in (SEED_TARBALL, SEED_SHA_NAME):
        out = dest / name
        with sess.get(asset_url(name), headers=headers, timeout=300, stream=True, allow_redirects=True) as resp:
            if resp.status_code != 200:
                raise SystemExit(f"Public seed download failed ({resp.status_code}) for {name}")
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

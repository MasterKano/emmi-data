"""Publish no-op, overlap lock, Zurich window, and seed handling."""
import hashlib
import json
import os
import subprocess
import sys
import tarfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import freshness
import locking
import publish_outputs
import restore_seed
import schedule_window
import update

REPO = Path(__file__).resolve().parents[3]


def _normals(path: Path):
    rows = []
    for offset in range(366):
        day = date(2020, 1, 1) + timedelta(days=offset)
        n_years = 8 if (day.month, day.day) == (2, 29) else 30
        rec = {"month": day.month, "day": day.day, "doy": offset + 1, "n_years": n_years}
        for name in ("wind100", "temp", "rain"):
            rec[f"{name}_mean"] = 1
            rec[f"{name}_mean_smooth"] = 1
            rec[f"{name}_p10"] = 0
            rec[f"{name}_p90"] = 2
            rec[f"{name}_min"] = 0
            rec[f"{name}_max"] = 3
        rows.append(rec)
    pd.DataFrame(rows).to_csv(path, index=False)


def _bundle(built: Path, end: str, rain_blank=True, drop=None):
    built.mkdir(parents=True, exist_ok=True)
    days = pd.date_range("1940-01-01", end, freq="D")
    if drop:
        days = days[days != pd.Timestamp(drop)]
    frame = pd.DataFrame({
        "date": days.strftime("%Y-%m-%d"),
        "wind100": 5.0,
        "temp": 1.0,
        "rain": 1.0,
    })
    if rain_blank and len(frame) and frame["date"].iloc[0] == "1940-01-01":
        frame.loc[0, "rain"] = float("nan")
    frame.to_csv(built / "daily_grid_mean.csv", index=False)
    _normals(built / "daily_normals.csv")
    (built / "monthly.csv").write_text("month,wind100\n1940-01,5\n")
    (built / "mtd.json").write_text(json.dumps({
        "generated": end,
        "daily_coverage": {"start": "1940-01-01", "end": end, "n_days": int(len(frame))},
    }))
    (built / "README.md").write_text("method\n")


def _git_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)


def test_unchanged_files_make_no_commit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    built = tmp_path / "built"
    end = "2026-11-21"
    _bundle(built, end)
    (repo / "nordic-climate").mkdir(parents=True)
    for name in publish_outputs.PUBLISH_NAMES:
        (repo / "nordic-climate" / name).write_bytes((built / name).read_bytes())
    _git_repo(repo)
    subprocess.run(["git", "add", "nordic-climate"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("NORDIC_CLIMATE_PUBLISH", "1")
    assert publish_outputs.publish(repo, built, today=date.fromisoformat(end)) is None
    after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert before == after


def test_changed_files_commit_as_github_actions_bot(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    built = tmp_path / "built"
    end = "2026-11-21"
    _bundle(repo / "nordic-climate", end)
    _git_repo(repo)
    subprocess.run(["git", "add", "nordic-climate"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    _bundle(built, end)
    payload = json.loads((built / "mtd.json").read_text())
    payload["generated"] = "2026-11-22"
    (built / "mtd.json").write_text(json.dumps(payload))
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("NORDIC_CLIMATE_PUBLISH", "1")
    sha = publish_outputs.publish(repo, built, today=date.fromisoformat(end))
    assert sha
    author = subprocess.check_output(["git", "log", "-1", "--format=%an %ae"], cwd=repo, text=True).strip()
    assert author == "github-actions[bot] 41898333+github-actions[bot]@users.noreply.github.com"
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)
    assert "cache" not in status


def test_publish_refuses_without_variable_and_when_validation_fails(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    built = tmp_path / "built"
    _bundle(built, "2026-11-21")
    _git_repo(repo)
    (repo / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    monkeypatch.delenv("NORDIC_CLIMATE_PUBLISH", raising=False)
    with pytest.raises(SystemExit, match="NORDIC_CLIMATE_PUBLISH"):
        publish_outputs.publish(repo, built, today=date(2026, 11, 21))
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip() == ""
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("NORDIC_CLIMATE_PUBLISH", "1")
    with pytest.raises(RuntimeError, match="stale"):
        publish_outputs.publish(repo, built, today=date(2026, 12, 31))
    gapped = tmp_path / "gapped"
    _bundle(gapped, "2026-11-21", drop="1980-05-05")
    with pytest.raises(RuntimeError, match="Incomplete 1940-to-latest"):
        publish_outputs.publish(repo, gapped, today=date(2026, 11, 21))
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == head


def test_publish_refuses_a_non_main_ref(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    built = tmp_path / "built"
    _bundle(built, "2026-11-21")
    _git_repo(repo)
    monkeypatch.setenv("NORDIC_CLIMATE_PUBLISH", "1")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/wp02/nordic-climate-actions")
    with pytest.raises(SystemExit, match="refs/heads/main"):
        publish_outputs.publish(repo, built, today=date(2026, 11, 21))
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip() == ""


def test_box_sync_dry_run_lists_public_urls(capsys):
    import sync_box_copy
    assert sync_box_copy.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "raw.githubusercontent.com/MasterKano/emmi-data/main/nordic-climate/mtd.json" in out
    assert "calibration.json" in out
    assert "/home/box/data/nordic-climate" in out
    with pytest.raises(SystemExit, match="/home/box"):
        sync_box_copy.main(["--dest", "/home/box/data/nordic-climate"])


def test_legacy_publisher_is_disabled():
    with pytest.raises(RuntimeError, match="github-actions"):
        update.publish_emmi_data()


def test_overlapping_lock_fails(tmp_path):
    path = tmp_path / "nordic.lock"
    held = locking.RunLock(path)
    held.acquire()
    try:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys\nfrom pathlib import Path\n"
             "sys.path.insert(0, sys.argv[1])\n"
             "from locking import RunLock\n"
             "RunLock(Path(sys.argv[2])).acquire()\n",
             str(Path(__file__).resolve().parents[1]), str(path)],
            capture_output=True, text=True,
        )
        assert proc.returncode != 0
        assert "overlapping" in (proc.stderr + proc.stdout)
    finally:
        held.release()


def test_zurich_window_accepts_a_late_runner_and_skips_a_duplicate():
    def at(y, m, d, hh, mm):
        return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)
    # Saturday 17 Jan 2026 04:05 UTC is 05:05 CET.
    assert schedule_window.allows(at(2026, 1, 17, 4, 5), "schedule")
    # 03:05 UTC that morning is 04:05 CET, before 04:50.
    assert not schedule_window.allows(at(2026, 1, 17, 3, 5), "schedule")
    # The same Saturday, a runner that only starts at 09:05 CET, still publishes.
    assert schedule_window.allows(at(2026, 1, 17, 8, 5), "schedule")
    # Saturday 18 Jul 2026 03:05 UTC is 05:05 CEST.
    assert schedule_window.allows(at(2026, 7, 18, 3, 5), "schedule")
    # 04:05 UTC is 06:05 CEST. Late enough to count, unless today was already published.
    assert schedule_window.allows(at(2026, 7, 18, 4, 5), "schedule")
    assert not schedule_window.allows(
        at(2026, 7, 18, 4, 5), "schedule", last_published=date(2026, 7, 18)
    )
    # 11 July 2026 is a Saturday. One Zurich day: the second hour does not run again.
    assert schedule_window.allows(at(2026, 7, 11, 3, 5), "schedule")
    assert not schedule_window.allows(
        at(2026, 7, 11, 4, 5), "schedule", last_published=date(2026, 7, 11)
    )
    # Friday 11 Dec 2026 04:05 UTC is 05:05 CET. A delay to 15:00 CET still counts.
    assert schedule_window.allows(at(2026, 12, 11, 4, 5), "schedule")
    assert schedule_window.allows(at(2026, 12, 11, 14, 0), "schedule")
    assert not schedule_window.allows(
        at(2026, 12, 11, 14, 0), "schedule", last_published=date(2026, 12, 11)
    )
    # The 10th is mr-climate's day, not this routine.
    assert not schedule_window.allows(at(2026, 12, 10, 4, 5), "schedule")
    # Manual runs are not tied to the clock.
    assert schedule_window.allows(at(2026, 12, 10, 12, 0), "workflow_dispatch")


def test_seed_extracts_only_monthly_csvs(tmp_path):
    assert restore_seed.is_monthly_point_csv("cache/openmeteo100m/om_1940_01.csv")
    assert not restore_seed.is_monthly_point_csv("cache/openmeteo100m/hourly_raw/era5.json.gz")
    assert not restore_seed.is_monthly_point_csv("cache/openmeteo/om_1940_01.csv")
    assert not restore_seed.is_monthly_point_csv("monthly.csv")
    tarball = tmp_path / "seed.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        for name, body in {
            "cache/openmeteo100m/om_2020_01.csv": b"date,lat,lon\n",
            "cache/openmeteo100m/hourly_raw/era5.json.gz": b"raw",
            "cache/openmeteo/om_2020_01.csv": b"ten-metre",
            "monthly.csv": b"month\n",
            "calibration.json": b"{}",
        }.items():
            src = tmp_path / "src" / name
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_bytes(body)
            tar.add(src, arcname=name)
    dest = tmp_path / "cache"
    written = restore_seed.extract_monthly_csvs(tarball, dest)
    assert [p.name for p in written] == ["om_2020_01.csv"]
    assert list(dest.iterdir()) == [dest / "om_2020_01.csv"]


def test_seed_sha256_mismatch_and_missing_token(tmp_path, monkeypatch):
    blob = tmp_path / "blob.tar.gz"
    blob.write_bytes(b"not a real tarball, just bytes")
    digest = hashlib.sha256(blob.read_bytes()).hexdigest()
    good = tmp_path / "good.sha256"
    good.write_text(digest + "  blob.tar.gz\n")
    assert restore_seed.PINNED_SHA256 == "09f4947c8062373b9a01869cbd1690dd98a93a43131cba304d92d5cbbbaebbb7"
    assert digest != restore_seed.PINNED_SHA256
    with pytest.raises(SystemExit, match="integrity"):
        restore_seed.verify_sha256(blob, good)
    monkeypatch.setattr(restore_seed, "PINNED_SHA256", digest)
    assert restore_seed.verify_sha256(blob, good) == digest
    bad = tmp_path / "bad.sha256"
    bad.write_text(("0" * 64) + "\n")
    with pytest.raises(SystemExit, match="integrity"):
        restore_seed.verify_sha256(blob, bad)
    monkeypatch.delenv("NORDIC_CLIMATE_SEED_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="environment secret"):
        restore_seed._token()


def test_workflows_are_read_only_until_the_publish_gate():
    dry = (REPO / ".github/workflows/nordic-climate-dry-run.yml").read_text()
    live = (REPO / ".github/workflows/nordic-climate-live.yml").read_text()
    seed = (REPO / ".github/workflows/nordic-climate-seed.yml").read_text()
    publish = (REPO / ".github/workflows/nordic-climate-publish.yml").read_text()
    groups = {
        dry: "group: nordic-climate-dry-run",
        live: "group: nordic-climate-live",
        seed: "group: nordic-climate-seed",
        publish: "group: nordic-climate-publish",
    }
    for text, group in groups.items():
        assert group in text
        assert "cancel-in-progress: false" in text
    for text in (dry, live, seed):
        assert "contents: read" in text
        assert "contents: write" not in text
    assert "contents: write" in publish
    assert "actions: write" not in publish
    assert "actions/cache" not in publish
    assert "actions/cache" not in seed
    assert "github.ref == 'refs/heads/main'" in publish
    assert "ref: main" in publish
    assert "nordic-climate-publish" in publish
    assert "nordic-climate-cutover" in publish
    assert "timezone: Europe/Zurich" in publish
    assert "5 5 * * 6" in publish
    assert "5 5 11 * *" in publish
    seed_on = seed.split("on:", 1)[1].split("permissions:", 1)[0]
    assert "pull_request" not in seed_on
    for text in (dry, live, seed, publish):
        assert "pull_request_target" not in text.split("jobs:", 1)[0]
    assert "nordic-climate-seed" in seed
    assert "NORDIC_CLIMATE_PUBLISH" in publish
    cron_lines = [line for line in publish.splitlines() if "cron:" in line]
    assert cron_lines and all(line.lstrip().startswith("#") for line in cron_lines)
    assert "github-actions[bot]" in (REPO / "scripts/nordic-climate/publish_outputs.py").read_text()


def test_published_readme_is_the_method_file_and_wind_stays_raw():
    assert (REPO / "nordic-climate/README.md").read_bytes() == (REPO / "scripts/nordic-climate/README.md").read_bytes()
    cal = json.loads((REPO / "scripts/nordic-climate/calibration.json").read_text())
    assert cal["vars"]["wind100"]["slope"] == 1.0
    assert cal["vars"]["wind100"]["intercept"] == 0.0
    assert cal["vars"]["temp"]["calibrated_rmse"] == pytest.approx(0.1696, abs=0.001)
    assert cal["vars"]["rain"]["calibrated_rmse"] == pytest.approx(0.0757, abs=0.001)
    assert publish_outputs.PUBLISH_NAMES == [
        "monthly.csv", "daily_grid_mean.csv", "daily_normals.csv", "mtd.json", "README.md",
    ]


def test_schedule_script_prints_proceed(monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("NORDIC_CLIMATE_CUTOVER_DONE", "1")
    monkeypatch.setenv("NORDIC_CLIMATE_NOW", "2026-12-11T04:05:00+00:00")
    assert schedule_window.main(["schedule_window.py"]) == 0
    monkeypatch.setenv("NORDIC_CLIMATE_NOW", "2026-12-10T04:05:00+00:00")
    assert schedule_window.main(["schedule_window.py"]) == 0


def test_publish_route_keeps_schedule_off_until_cutover_is_done():
    main = "refs/heads/main"
    assert schedule_window.publish_route("schedule", "1", None, main) is None
    assert schedule_window.publish_route("schedule", "1", "", main) is None
    assert schedule_window.publish_route("schedule", "1", "1", main) == "nordic-climate-publish"
    assert schedule_window.publish_route("workflow_dispatch", "1", None, main) == "nordic-climate-cutover"
    assert schedule_window.publish_route("workflow_dispatch", "1", "1", main) == "nordic-climate-publish"
    assert schedule_window.publish_route("schedule", None, "1", main) is None
    assert schedule_window.publish_route("workflow_dispatch", None, None, main) is None
    assert schedule_window.publish_route("schedule", "1", "1", "refs/heads/wp02/nordic-climate-actions") is None
    text = (REPO / ".github/workflows/nordic-climate-publish.yml").read_text()
    assert "github.event_name == 'schedule' && vars.NORDIC_CLIMATE_CUTOVER_DONE == '1'" in text
    assert "github.event_name == 'schedule' || vars.NORDIC_CLIMATE_CUTOVER_DONE" not in text


def test_scheduled_publish_refuses_without_cutover_done(monkeypatch):
    monkeypatch.setenv("NORDIC_CLIMATE_PUBLISH", "1")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.delenv("NORDIC_CLIMATE_CUTOVER_DONE", raising=False)
    with pytest.raises(SystemExit, match="NORDIC_CLIMATE_CUTOVER_DONE"):
        publish_outputs.publish(Path("/tmp/unused-nordic-publish"), Path("/tmp/unused-nordic-built"))

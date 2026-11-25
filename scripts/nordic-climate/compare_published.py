#!/usr/bin/env python3
"""Compare rebuilt compact files with the published nordic-climate/ tree.

Daily overlap is compared after rounding to the published precision (2 decimals).
New dates may appear past the published end. A mismatch on a shared date fails.
``daily_normals.csv`` and ``README.md`` are compared as bytes when both exist.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

DAILY_COLS = ["wind100", "temp", "rain"]


def _round_match(built: pd.DataFrame, published: pd.DataFrame, decimals: int) -> dict:
    built = built.copy()
    published = published.copy()
    built["date"] = pd.to_datetime(built["date"]).dt.strftime("%Y-%m-%d")
    published["date"] = pd.to_datetime(published["date"]).dt.strftime("%Y-%m-%d")
    merged = published.merge(built, on="date", how="left", suffixes=("_pub", "_new"))
    missing = merged[merged[f"{DAILY_COLS[0]}_new"].isna()]["date"].tolist()
    mismatches = []
    max_abs = {k: 0.0 for k in DAILY_COLS}
    compared = 0
    for _, row in merged.iterrows():
        if pd.isna(row[f"{DAILY_COLS[0]}_new"]):
            continue
        compared += 1
        for col in DAILY_COLS:
            pub = row[f"{col}_pub"]
            new = row[f"{col}_new"]
            if pd.isna(pub) and pd.isna(new):
                continue
            if pd.isna(pub) or pd.isna(new):
                mismatches.append({"date": row["date"], "column": col, "published": None if pd.isna(pub) else float(pub), "rebuilt": None if pd.isna(new) else float(new)})
                continue
            diff = abs(round(float(new), decimals) - round(float(pub), decimals))
            max_abs[col] = max(max_abs[col], diff)
            if diff > 0:
                mismatches.append(
                    {"date": row["date"], "column": col, "published": round(float(pub), decimals), "rebuilt": round(float(new), decimals)}
                )
    new_dates = sorted(set(built["date"]) - set(published["date"]))
    return {
        "compared_days": compared,
        "published_days_missing_from_rebuild": missing[:20],
        "n_published_days_missing": len(missing),
        "n_mismatches": len(mismatches),
        "mismatches_head": mismatches[:20],
        "max_abs_diff": max_abs,
        "new_dates_head": new_dates[:20],
        "n_new_dates": len(new_dates),
        "rebuild_end": built["date"].max() if len(built) else None,
        "published_end": published["date"].max() if len(published) else None,
    }


def compare(built_dir: Path, published_dir: Path) -> dict:
    report = {"built": str(built_dir), "published": str(published_dir)}
    daily_b = pd.read_csv(built_dir / "daily_grid_mean.csv")
    daily_p = pd.read_csv(published_dir / "daily_grid_mean.csv")
    report["daily"] = _round_match(daily_b, daily_p, 2)
    # 1940-01-01 rain stays blank on both sides.
    b0 = daily_b.loc[daily_b["date"].astype(str).str[:10].eq("1940-01-01")]
    p0 = daily_p.loc[daily_p["date"].astype(str).str[:10].eq("1940-01-01")]
    report["rain_1940_01_01_blank"] = bool(len(b0) and len(p0) and pd.isna(b0["rain"].iloc[0]) and pd.isna(p0["rain"].iloc[0]))
    for name in ("daily_normals.csv", "README.md"):
        b = (built_dir / name).read_bytes()
        p = (published_dir / name).read_bytes()
        report[name] = {"byte_equal": b == p, "built_bytes": len(b), "published_bytes": len(p)}
    if (built_dir / "monthly.csv").exists():
        mb = pd.read_csv(built_dir / "monthly.csv")
        mp = pd.read_csv(published_dir / "monthly.csv")
        merged = mp.merge(mb, on="month", how="inner", suffixes=("_pub", "_new"))
        monthly_max = {}
        n_mis = 0
        for col in ("wind100", "temp", "rain"):
            if f"{col}_pub" not in merged or f"{col}_new" not in merged:
                continue
            diff = (merged[f"{col}_new"] - merged[f"{col}_pub"]).abs()
            monthly_max[col] = None if diff.dropna().empty else float(diff.max())
            n_mis += int((diff.fillna(0) > 1e-4).sum())
        report["monthly"] = {
            "overlapping_months": int(len(merged)),
            "max_abs_diff": monthly_max,
            "n_mismatches_1e-4": n_mis,
        }
    report["ok"] = (
        report["daily"]["n_mismatches"] == 0
        and report["daily"]["n_published_days_missing"] == 0
        and report["rain_1940_01_01_blank"]
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--built", type=Path, required=True)
    parser.add_argument("--published", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.built, args.published)
    args.out.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("ok", "daily", "rain_1940_01_01_blank")}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

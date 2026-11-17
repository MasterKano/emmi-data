#!/usr/bin/env python3
"""1-day gaps are interpolated; longer gaps stay blank; 30-day average is not blanked."""
import datetime as dt
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
# build.py imports box-only helpers; load only the two functions via exec of a stub if needed.
# Import after putting scripts on the path so the module can load.
import os
SCRIPTS = Path("/workspace/emmi-data/scripts")
if not SCRIPTS.exists():
    SCRIPTS = Path("/workspace/emmi-mtd/emmi-data/scripts")
sys.path.insert(0, str(SCRIPTS))

import build as b

def test_one_day_gap_is_interpolated():
    idx = pd.date_range("2026-11-01", "2026-11-10")
    s = pd.Series([10, 20, np.nan, 40, 50, 60, 70, 80, 90, 100], index=idx)
    out = b.interpolate_short_gaps(s)
    assert abs(out.loc["2026-11-03"] - 30) < 1e-9
    assert out.isna().sum() == 0

def test_two_day_gap_stays_blank():
    idx = pd.date_range("2026-11-01", "2026-11-06")
    s = pd.Series([10, np.nan, np.nan, 40, 50, 60], index=idx)
    out = b.interpolate_short_gaps(s)
    assert pd.isna(out.loc["2026-11-02"]) and pd.isna(out.loc["2026-11-03"])
    assert out.loc["2026-11-01"] == 10
    assert out.loc["2026-11-04"] == 40

def test_end_gap_not_filled():
    idx = pd.date_range("2026-11-01", "2026-11-04")
    s = pd.Series([10, 20, 30, np.nan], index=idx)
    out = b.interpolate_short_gaps(s)
    assert pd.isna(out.loc["2026-11-04"])

def test_trailing_survives_one_day_gap():
    idx = pd.date_range("2026-10-01", "2026-11-16")
    s = pd.Series(20.0, index=idx)
    s.loc["2026-11-08"] = np.nan
    ma = b.trailing(s)
    assert pd.notna(ma.loc["2026-11-16"])
    assert abs(ma.loc["2026-11-16"] - 20) < 1e-9
    # The filled day itself also has a 30-day average.
    assert pd.notna(ma.loc["2026-11-08"])

if __name__ == "__main__":
    # build.py expects box climate/scripts; may fail to import if those fail.
    test_one_day_gap_is_interpolated()
    test_two_day_gap_stays_blank()
    test_end_gap_not_filled()
    test_trailing_survives_one_day_gap()
    print("ok")

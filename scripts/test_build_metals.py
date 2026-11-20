"""Unit tests for CoT source URLs, CFTC release timing, and the RBOB / feeder mappings."""
import datetime as dt
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_metals as M  # noqa: E402
from universe import GROUPS  # noqa: E402


def _syms():
    out = []
    for g in GROUPS:
        for s in g.get("sections") or []:
            for r in s.get("rows") or []:
                out.append(r[0] if isinstance(r, (list, tuple)) else r.get("sym") or r.get("yahoo") or r)
    return out


def _row(sym):
    for g in GROUPS:
        for s in g.get("sections") or []:
            for r in s.get("rows") or []:
                # R() returns a dict-like or a tuple; inspect
                if isinstance(r, dict) and r.get("sym") == sym:
                    return r
                if hasattr(r, "get") and r.get("sym") == sym:
                    return r
                if isinstance(r, (list, tuple)) and r and r[0] == sym:
                    return r
    return None


def test_ice_current_year_prefers_automated_file():
    now = dt.datetime(2026, 11, 20, 21, 0, tzinfo=dt.timezone.utc)
    urls = M.ice_csv_urls(2026, now=now)
    assert urls[0] == "https://www.ice.com/publicdocs/cot_report/automated/COTHist2026.csv"
    assert "publicdocs/futures/COTHist2026.csv" in urls[1]
    # Prior years stay on the annual archive only.
    assert M.ice_csv_urls(2025, now=now) == ("https://www.ice.com/publicdocs/futures/COTHist2025.csv",)


def test_expected_cftc_tuesday_before_and_after_friday_release():
    # Friday 20 Nov 2026 19:00 UTC: 17 Nov report is not yet out (release 20:30 UTC).
    before = dt.datetime(2026, 11, 20, 19, 0, tzinfo=dt.timezone.utc)
    assert M.expected_cftc_tuesday(before) == "2026-11-10"
    after = dt.datetime(2026, 11, 20, 21, 0, tzinfo=dt.timezone.utc)
    assert M.expected_cftc_tuesday(after) == "2026-11-17"
    # The following Monday still expects the 17 Nov report.
    mon = dt.datetime(2026, 11, 23, 8, 0, tzinfo=dt.timezone.utc)
    assert M.expected_cftc_tuesday(mon) == "2026-11-17"


def test_us_list_has_rbob_and_feeders_with_yahoo_symbols():
    by = {m[0]: m for m in M.US}
    assert by["rbob"][3] == "RB=F" and by["rbob"][4] == "111659"
    assert by["feeders"][3] == "GF=F" and by["feeders"][4] == "061641"
    assert by["feeders"][2] == "livestock" and by["rbob"][2] == "energy"


def _all_yahoo():
    syms = []
    for g in GROUPS:
        for sec in g.get("sections") or []:
            for r in sec.get("rows") or []:
                if isinstance(r, dict):
                    syms.append(r.get("sym") or r.get("yahoo") or r.get("id"))
                else:
                    # namedtuple / SimpleNamespace / tuple
                    syms.append(getattr(r, "sym", None) or (r[0] if r else None))
    return [s for s in syms if s]


def test_universe_lists_rbob_and_feeder_cattle():
    # Inspect structure once if this fails.
    syms = _all_yahoo()
    assert "RB=F" in syms, syms[:20]
    assert "GF=F" in syms


if __name__ == "__main__":
    test_ice_current_year_prefers_automated_file()
    test_expected_cftc_tuesday_before_and_after_friday_release()
    test_us_list_has_rbob_and_feeders_with_yahoo_symbols()
    test_universe_lists_rbob_and_feeder_cattle()
    print("ok")

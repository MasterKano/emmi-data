"""Pure checks for the NEI monthly annex. No network and no stored files required."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nei_monthly_annex as A


def test_october_2026_has_the_25_hour_delivery_day():
    assert A.day_hours(dt.date(2026, 10, 25)) == 25
    assert A.day_hours(dt.date(2026, 10, 24)) == 24
    assert A.month_hours(2026, 10) == 745
    assert A.month_hours(2026, 3) == 743
    assert A.month_hours(2026, 11) == 720


def test_hour_weighted_mean_refuses_a_missing_leg():
    assert A.hour_weighted([(10, 10), (20, 30)]) == 17.5
    assert A.hour_weighted([(10, 10), (None, 30)]) is None
    assert A.hour_weighted([]) is None


def test_outright_is_not_added_to_sys():
    price, how = A.quote_price("outright", 131.99, 88.06)
    assert price == 131.99
    assert how == "direct_settlement"
    assert A.implied_spread(131.99, 88.06) == 131.99 - 88.06


def test_epad_is_added_only_when_sys_exists():
    price, how = A.quote_price("epad", 20.0, 88.0)
    assert (price, how) == (108.0, "sys_plus_epad")
    assert A.quote_price("epad", 20.0, None) == (None, "epad_without_sys")
    assert A.quote_price("outright", None, 88.0) == (None, "missing")


def test_blank_quote_is_missing_and_a_real_zero_stays_zero():
    assert A.parse_number("") is None
    assert A.parse_number("  ") is None
    assert A.parse_number("0") == 0.0
    assert A.parse_number("88.06") == 88.06


def test_labels():
    outright = {"marketGroup": "POWER", "product": "Base", "commodity": "POWER", "shortCode": "1NBM", "delivery": "Nov-26", "area": "NO1"}
    epad = {"marketGroup": "POWER", "product": "Base", "commodity": "POWER", "shortCode": "SYOSLOEPAD", "delivery": "Nov-26", "area": "NO1"}
    assert A.instrument_kind(outright) == "outright"
    assert A.instrument_kind(epad) == "epad"
    assert A.delivered_month_check(88.06, 88.056, 88.056) == "matches_spot_outright"
    assert A.delivered_month_check(52.0, 140.0, 88.0) == "looks_like_epad"


def test_remainder_baskets_and_sessions():
    assert A.remainder_months(dt.date(2026, 10, 30)) == [("Nov-26", 720), ("Dec-26", 744)]
    sep = A.remainder_months(dt.date(2026, 9, 30))
    assert [name for name, _ in sep] == ["Oct-26", "Nov-26", "Dec-26"]
    assert sep[0][1] == 745
    dates = [dt.date(2026, 9, 29), dt.date(2026, 9, 30), dt.date(2026, 10, 30), dt.date(2026, 11, 24)]
    assert A.last_session(dates, 2026, 9) == dt.date(2026, 9, 30)
    assert A.last_session(dates, 2026, 10) == dt.date(2026, 10, 30)


def test_partial_month_in_the_stored_price_file_is_not_final():
    doc = {
        "history_end": "2026-11-26",
        "months": ["2026-10", "2026-11"],
        "monthly": {"Nordic": [88.056, 143.816], "NO1": [131.991, 150.0]},
    }
    found = A.found_from_prices(doc)
    assert A.covers_month(found["2026-10"]["start"], found["2026-10"]["end"], 2026, 10)
    assert not A.covers_month(found["2026-11"]["start"], found["2026-11"]["end"], 2026, 11)


def test_zero_open_interest_is_flagged_not_treated_as_blank():
    assert "zero_open_interest" in A.liquidity_flag(None, 0)
    assert "zero_open_interest" not in A.liquidity_flag(None, None)
    assert "volume_blank" in A.liquidity_flag(None, 10)

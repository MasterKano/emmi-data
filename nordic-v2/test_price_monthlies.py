#!/usr/bin/env python3
"""Monthly day-ahead prices are period-weighted, matching Nord Pool's Monthly Aggregate."""
import json
import unittest
from pathlib import Path
import price_monthlies

ROOT = Path(__file__).resolve().parent


def at(doc, code, ym):
    return doc['monthly'][code][doc['months'].index(ym)]


class MonthlyPrices(unittest.TestCase):
    def test_long_dst_day_is_period_weighted_not_day_weighted(self):
        # 30 ordinary 96-period days at 100, plus one cheap 100-period clock-change day.
        days = [[100.0] * 96 for _ in range(30)] + [[0.0] * 100]
        got = price_monthlies.monthly_mean(days)
        period = (30 * 96 * 100) / (30 * 96 + 100)
        day_weighted = (30 * 100) / 31
        self.assertAlmostEqual(got, period, places=9)
        self.assertGreater(abs(got - day_weighted), 0.1)

    def test_short_dst_day_is_period_weighted_not_day_weighted(self):
        # Spring-forward: 92 periods, not 96. Collapsing the day first over-weights it.
        days = [[10.0] * 96 for _ in range(30)] + [[100.0] * 92]
        got = price_monthlies.monthly_mean(days)
        period = (30 * 96 * 10 + 92 * 100) / (30 * 96 + 92)
        day_weighted = (30 * 10 + 100) / 31
        self.assertAlmostEqual(got, period, places=9)
        self.assertGreater(abs(got - day_weighted), 0.1)

    def test_missing_period_is_left_missing(self):
        self.assertAlmostEqual(price_monthlies.monthly_mean([[10.0, None, 30.0]]), 20.0)
        self.assertIsNone(price_monthlies.monthly_mean([[None, None]]))

    def test_country_uses_only_periods_present_in_every_zone(self):
        zones = {
            'DK1': [('a', 10.0), ('b', None)],
            'DK2': [('a', 30.0), ('b', 50.0)],
        }
        self.assertEqual(price_monthlies.equal_weight_periods(zones), [20.0])

    def test_october_2026_matches_official_nord_pool_monthly_aggregate(self):
        doc = json.loads((ROOT / 'prices_normals.json').read_text())
        sys = at(doc, 'Nordic', '2026-10')
        no1 = at(doc, 'NO1', '2026-10')
        # Official Monthly Aggregate is 88.06 / 131.99. The stored figure is the
        # unrounded period mean (about 88.056319 / 131.991289), not the old
        # mean of the 31 daily averages (88.169 / 132.091).
        self.assertEqual(price_monthlies.round_half_up(sys, 2), 88.06)
        self.assertEqual(price_monthlies.round_half_up(no1, 2), 131.99)
        self.assertAlmostEqual(sys, 88.056319, delta=0.001)
        self.assertAlmostEqual(no1, 131.991289, delta=0.001)
        self.assertGreater(abs(sys - 88.169), 0.05)
        self.assertGreater(abs(no1 - 132.091), 0.05)
        self.assertIn('not the mean of daily averages', doc['method'])
        self.assertIn('Nord Pool', doc['source'])

    def test_neighbouring_months_and_pre_2024_gap(self):
        doc = json.loads((ROOT / 'prices_normals.json').read_text())
        self.assertEqual(price_monthlies.round_half_up(at(doc, 'Nordic', '2026-09'), 2), 95.57)
        self.assertEqual(price_monthlies.round_half_up(at(doc, 'NO1', '2026-09'), 2), 122.91)
        # March 2026 is the spring-forward month; the daily-mean method was 63.734 / 105.437.
        self.assertEqual(price_monthlies.round_half_up(at(doc, 'Nordic', '2026-03'), 2), 63.77)
        self.assertEqual(price_monthlies.round_half_up(at(doc, 'NO1', '2026-03'), 2), 105.45)
        self.assertEqual(doc['history_end'], '2026-11-12')
        self.assertEqual(price_monthlies.round_half_up(at(doc, 'Nordic', '2026-11'), 2), 140.63)
        self.assertEqual(price_monthlies.round_half_up(at(doc, 'NO1', '2026-11'), 2), 197.86)
        # Nord Pool's public API returns HTTP 401 before 2024, so this day-weighted October stays.
        self.assertEqual(at(doc, 'NO1', '2023-10'), 36.792)
        self.assertIsNone(at(doc, 'Nordic', '2023-10'))


if __name__ == '__main__':
    unittest.main()

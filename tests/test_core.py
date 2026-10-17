"""Offline tests for the pricing and settlement logic. Run: python3 -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import polymarket_arb_scanner as s  # noqa: E402


def market(mid, yes_price, closed=True):
    return {"id": mid, "closed": closed, "outcomes": '["Yes","No"]',
            "outcomePrices": f'["{yes_price}","{1 - yes_price}"]'}


class WalkSets(unittest.TestCase):
    def test_no_gap_buys_nothing(self):
        self.assertEqual(s.walk_sets([[(0.5, 10)], [(0.5, 10)]], 1.0, 0.0, 0.01), (0.0, 0.0))

    def test_stops_at_thinnest_level_then_margin(self):
        # First level: 0.45 + 0.50 = 0.95 for 10 sets. Next: 0.50 + 0.50 = 1.00, no margin.
        qty, cost = s.walk_sets([[(0.45, 10), (0.50, 50)], [(0.50, 40)]], 1.0, 0.0, 0.01)
        self.assertAlmostEqual(qty, 10)
        self.assertAlmostEqual(cost, 9.5)

    def test_max_cost_caps_size(self):
        qty, cost = s.walk_sets([[(0.40, 100)], [(0.40, 100)]], 1.0, 0.0, 0.01, max_cost=8)
        self.assertAlmostEqual(cost, 8)
        self.assertAlmostEqual(qty, 10)

    def test_fee_can_remove_the_gap(self):
        self.assertEqual(s.walk_sets([[(0.49, 10)], [(0.49, 10)]], 1.0, 0.05, 0.01), (0.0, 0.0))

    def test_empty_book(self):
        self.assertEqual(s.walk_sets([[(0.4, 10)], []], 1.0, 0.0, 0.0), (0.0, 0.0))


class Books(unittest.TestCase):
    def test_no_asks_mirror_yes_bids(self):
        book = {"bids": [{"price": "0.30", "size": "5"}, {"price": "0.40", "size": "2"}]}
        self.assertEqual(s.no_asks_of(book), [(0.6, 2.0), (0.7, 5.0)])

    def test_asks_drop_junk_levels(self):
        book = {"asks": [{"price": "0.5", "size": "0"}, {"price": "1", "size": "3"}, {"price": "0.2", "size": "4"}]}
        self.assertEqual(s.asks_of(book), [(0.2, 4.0)])


class Candidates(unittest.TestCase):
    def ev(self, markets, **kw):
        return {"negRisk": True, "markets": markets, **kw}

    def open_market(self, mid):
        return {"id": mid, "outcomes": '["Yes","No"]', "clobTokenIds": f'["{mid}y","{mid}n"]'}

    def test_resolved_no_leg_drops_out(self):
        legs, why = s.build_candidate(self.ev([self.open_market("a"), self.open_market("b"), market("c", 0)]))
        self.assertEqual(why, "ok")
        self.assertEqual([leg["yes"] for leg in legs], ["ay", "by"])

    def test_already_won_event_is_skipped(self):
        legs, _ = s.build_candidate(self.ev([self.open_market("a"), self.open_market("b"), market("c", 1)]))
        self.assertIsNone(legs)

    def test_augmented_skipped_by_default(self):
        legs, _ = s.build_candidate(self.ev([self.open_market("a"), self.open_market("b")], negRiskAugmented=True))
        self.assertIsNone(legs)


class Settlement(unittest.TestCase):
    def test_yes_set_pays_one_per_set(self):
        ev = {"markets": [market("a", 0), market("b", 1), market("c", 0)]}
        pos = {"side": "YES", "sets": 10, "payout": 10, "market_ids": ["a", "b", "c"]}
        self.assertEqual(s.actual_payout(pos, ev), 10)

    def test_waits_until_every_leg_resolves(self):
        ev = {"markets": [market("a", 0), market("b", 0.5, closed=False)]}
        pos = {"side": "YES", "sets": 10, "payout": 10, "market_ids": ["a", "b"]}
        self.assertIsNone(s.actual_payout(pos, ev))

    def test_legacy_no_set_ignores_markets_closed_before_buy(self):
        # Bought NO on b, c, d (3 legs -> pays 2/set); a had already resolved NO.
        ev = {"closed": True, "markets": [market("a", 0), market("b", 1), market("c", 0), market("d", 0)]}
        pos = {"side": "NO", "sets": 10, "payout": 20}
        self.assertEqual(s.actual_payout(pos, ev), 20)


if __name__ == "__main__":
    unittest.main()

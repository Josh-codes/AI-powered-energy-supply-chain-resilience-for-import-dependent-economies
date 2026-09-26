"""Tests for response/reroute.py — the MCDM alternative-supplier ranking.

Mocking convention: none. Scores are a pure function of committed seed data
(alternatives.json + refineries.json) and nothing here reads live risk.

What is and is not pinned to a number: everything that derives from committed
seed data IS pinned — grade-compatibility fractions, MCDM scores and ranking
order. The anchors below were computed straight from the JSON files by an
independent script, not by calling this module, so a pinned test checks the
module against the data rather than against itself. If alternatives.json or
refineries.json change, these anchors must be recomputed deliberately.
"""
from types import SimpleNamespace

from django.core.management import call_command
from django.test import TestCase

from core.models import AlternativeSupplier
from response.reroute import (
    CRISIS_WEIGHTS,
    compute_grade_compatibility,
    normalize,
    rank_alternatives,
    replacement_timeline,
    score_alternative,
)

# Fraction of the 5.364 mb/d refinery fleet admitting each crude.
COMPAT = {
    "Saudi Arabia via Yanbu": 0.997390,
    "UAE via Fujairah": 1.000000,
    "Kuwait via Shuaiba": 0.698546,
    "Iraq via Ceyhan": 0.997390,
    "USA WTI": 0.942021,
    "Nigeria Bonny Light": 1.000000,
    "Angola Girassol": 1.000000,
    "Russia Urals": 1.000000,
    "Kazakhstan CPC Blend": 0.386465,
    "Libya Es Sider": 1.000000,
}

HORMUZ_NORMAL = [
    ("UAE via Fujairah", 1.0),
    ("Saudi Arabia via Yanbu", 0.914348),
    ("Libya Es Sider", 0.78),
    ("Kuwait via Shuaiba", 0.754636),
    ("Iraq via Ceyhan", 0.744348),
    ("Nigeria Bonny Light", 0.625),
    ("Kazakhstan CPC Blend", 0.606616),
    ("Angola Girassol", 0.54),
    ("USA WTI", 0.235505),
]
CAPE_NORMAL = [
    "UAE via Fujairah", "Saudi Arabia via Yanbu", "Kuwait via Shuaiba",
    "Libya Es Sider", "Iraq via Ceyhan", "Kazakhstan CPC Blend",
]
CAPE_SEVERE = [
    "UAE via Fujairah", "Saudi Arabia via Yanbu", "Libya Es Sider",
    "Kuwait via Shuaiba", "Iraq via Ceyhan", "Kazakhstan CPC Blend",
]
RED_SEA_NORMAL = ["UAE via Fujairah", "Nigeria Bonny Light", "Angola Girassol", "USA WTI"]


def _alt(name="x", premium=0.0, transit=10, api=32.0, sulfur=1.0, transits=(), sanctioned=False, mbd=0.1):
    return SimpleNamespace(
        name=name, country=name, route_description="", price_premium_usd=premium,
        transit_days=transit, api_gravity=api, sulfur_pct=sulfur,
        transits_corridors=list(transits), sanctioned=sanctioned, max_incremental_mbd=mbd,
    )


def _ref(cap, api_min, api_max, sulfur):
    return SimpleNamespace(
        capacity_mbd=cap, api_gravity_min=api_min, api_gravity_max=api_max, sulfur_tolerance=sulfur,
    )


class NormalizeTests(TestCase):
    def test_degenerate_range_is_one(self):
        self.assertEqual(normalize(5, [5, 5, 5]), 1.0)
        self.assertEqual(normalize(5, [5, 5, 5], invert=True), 1.0)

    def test_min_max(self):
        self.assertEqual(normalize(0, [0, 10]), 0.0)
        self.assertEqual(normalize(10, [0, 10]), 1.0)
        self.assertEqual(normalize(10, [0, 10], invert=True), 0.0)

    def test_negative_premium_is_handled(self):
        """Russia's -$3 discount: the cheapest option gets cost_score 1.0."""
        self.assertEqual(normalize(-3.0, [-3.0, 1.0, 9.0], invert=True), 1.0)


class GradeCompatibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_db", verbosity=0)

    def test_fractions_match_the_seed_data(self):
        for alt in AlternativeSupplier.objects.all():
            self.assertAlmostEqual(compute_grade_compatibility(alt), COMPAT[alt.name], places=6, msg=alt.name)

    def test_is_capacity_weighted_not_a_refinery_count(self):
        refineries = [_ref(0.9, 30, 40, 2.0), _ref(0.1, 20, 25, 3.0)]
        self.assertAlmostEqual(compute_grade_compatibility(_alt(api=35, sulfur=1.0), refineries), 0.9)
        self.assertAlmostEqual(compute_grade_compatibility(_alt(api=22, sulfur=1.0), refineries), 0.1)

    def test_sulfur_tolerance_excludes(self):
        refineries = [_ref(1.0, 20, 45, 1.5)]
        self.assertEqual(compute_grade_compatibility(_alt(api=32, sulfur=2.0), refineries), 0.0)

    def test_window_bounds_are_inclusive(self):
        refineries = [_ref(1.0, 30, 40, 2.0)]
        self.assertEqual(compute_grade_compatibility(_alt(api=30, sulfur=2.0), refineries), 1.0)
        self.assertEqual(compute_grade_compatibility(_alt(api=40, sulfur=2.0), refineries), 1.0)

    def test_empty_fleet_is_zero_not_a_division_error(self):
        self.assertEqual(compute_grade_compatibility(_alt(), []), 0.0)


class ScoreAlternativeTests(TestCase):
    def test_dominant_option_scores_one(self):
        best = _alt("best", premium=0, transit=5)
        worst = _alt("worst", premium=10, transit=30)
        refs = [_ref(1.0, 20, 45, 4.0)]
        self.assertAlmostEqual(score_alternative(best, [best, worst], refineries=refs)["score"], 1.0)
        self.assertAlmostEqual(score_alternative(worst, [best, worst], refineries=refs)["score"], 0.25)

    def test_weights_sum_to_one(self):
        for weights in CRISIS_WEIGHTS.values():
            self.assertAlmostEqual(sum(weights), 1.0)

    def test_unknown_crisis_raises(self):
        with self.assertRaises(ValueError):
            score_alternative(_alt(), [_alt()], crisis="panic", refineries=[])


class RankAlternativesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_db", verbosity=0)

    def _names(self, rows):
        return [r["source"] for r in rows]

    def test_hormuz_ranking_matches_the_seed_data(self):
        rows = rank_alternatives("Hormuz")
        self.assertEqual(self._names(rows), [n for n, _ in HORMUZ_NORMAL])
        for row, (_name, score) in zip(rows, HORMUZ_NORMAL):
            self.assertAlmostEqual(row["score"], score, places=6, msg=row["source"])

    def test_every_corridor_has_candidates(self):
        """The Phase 5 data defect: with eligibility keyed on avoids_corridor
        (always "Hormuz"), Cape and Red Sea returned nothing."""
        self.assertEqual(self._names(rank_alternatives("Cape")), CAPE_NORMAL)
        self.assertEqual(self._names(rank_alternatives("Red Sea")), RED_SEA_NORMAL)

    def test_alternatives_transiting_the_disrupted_corridor_are_excluded(self):
        """Saudi via Yanbu runs through Suez, so it is a Hormuz and Cape
        replacement but must never be offered for a Red Sea disruption."""
        self.assertIn("Saudi Arabia via Yanbu", self._names(rank_alternatives("Hormuz")))
        self.assertIn("Saudi Arabia via Yanbu", self._names(rank_alternatives("Cape")))
        self.assertNotIn("Saudi Arabia via Yanbu", self._names(rank_alternatives("Red Sea")))
        for row in rank_alternatives("Red Sea"):
            self.assertNotIn("Red Sea", row["transits_corridors"])

    def test_severe_crisis_weights_reorder_cape(self):
        """Both sail 12 days, so transit cannot separate them. When the cost
        weight falls 0.40 -> 0.20, Kuwait's $1 price edge no longer outweighs
        its poor grade fit (0.70 of the fleet vs Libya's 1.00)."""
        self.assertEqual(self._names(rank_alternatives("Cape", crisis="severe")), CAPE_SEVERE)

    def test_sanctioned_alternatives_are_excluded_by_default(self):
        self.assertNotIn("Russia Urals", self._names(rank_alternatives("Hormuz")))
        rows = rank_alternatives("Hormuz", include_sanctioned=True)
        self.assertIn("Russia Urals", self._names(rows))
        self.assertTrue(next(r for r in rows if r["source"] == "Russia Urals")["sanctioned"])

    def test_scores_are_relative_to_the_candidate_set(self):
        """Adding Russia's -$3 premium widens the cost range, so UAE's score
        drops although UAE itself did not change."""
        without = rank_alternatives("Hormuz")[0]
        with_russia = next(
            r for r in rank_alternatives("Hormuz", include_sanctioned=True) if r["source"] == "UAE via Fujairah"
        )
        self.assertAlmostEqual(without["score"], 1.0, places=6)
        self.assertAlmostEqual(with_russia["score"], 0.866667, places=6)

    def test_documented_api_keys_are_present(self):
        row = rank_alternatives("Hormuz")[0]
        for key in ("source", "score", "cost_score", "transit_score", "compat_score",
                    "transit_days", "price_premium"):
            self.assertIn(key, row)

    def test_supply_shock_excludes_no_route(self):
        rows = rank_alternatives(None)
        self.assertEqual(len(rows), AlternativeSupplier.objects.filter(sanctioned=False).count())

    def test_cumulative_coverage(self):
        rows = rank_alternatives("Hormuz", gap_mbd=1.0)
        running = 0.0
        for r in rows:
            running += r["max_incremental_mbd"]
            self.assertAlmostEqual(r["cumulative_coverage_mbd"], running, places=9)
            self.assertLessEqual(r["cumulative_coverage_pct"], 100.0)
            self.assertEqual(r["covers_gap"], running >= 1.0 - 1e-9)
        self.assertNotIn("cumulative_coverage_mbd", rank_alternatives("Hormuz")[0])

    def test_empty_candidate_set_returns_empty_list(self):
        only_hormuz = [_alt(transits=["Hormuz"])]
        self.assertEqual(rank_alternatives("Hormuz", alternatives=only_hormuz), [])

    def test_ties_break_on_name(self):
        twins = [_alt("b"), _alt("a")]
        rows = rank_alternatives("Hormuz", alternatives=twins, refineries=[_ref(1, 20, 45, 4)])
        self.assertEqual(self._names(rows), ["a", "b"])

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            rank_alternatives("Suez")
        with self.assertRaises(ValueError):
            rank_alternatives("Hormuz", crisis="panic")
        with self.assertRaises(ValueError):
            rank_alternatives("Hormuz", gap_mbd=-1)


class ReplacementTimelineTests(TestCase):
    def _rows(self, *spec):
        return [{"source": s, "transit_days": d, "max_incremental_mbd": m} for s, d, m in spec]

    def test_stops_at_the_first_covering_alternative(self):
        rows = self._rows(("a", 8, 0.3), ("b", 20, 0.5), ("c", 10, 1.0))
        t = replacement_timeline(rows, 0.7)
        self.assertEqual(t["alternatives_used"], ["a", "b"])
        self.assertEqual(t["transit_days"], 20)  # the SLOWEST cargo needed
        self.assertTrue(t["covers_gap"])
        self.assertEqual(t["residual_gap_mbd"], 0.0)

    def test_reports_residual_when_nothing_covers(self):
        t = replacement_timeline(self._rows(("a", 8, 0.3)), 1.0)
        self.assertFalse(t["covers_gap"])
        self.assertAlmostEqual(t["residual_gap_mbd"], 0.7)

    def test_empty_ranking_has_no_transit(self):
        t = replacement_timeline([], 1.0)
        self.assertIsNone(t["transit_days"])
        self.assertFalse(t["covers_gap"])

    def test_daily_gap_steps_down_as_each_cargo_lands(self):
        rows = self._rows(("a", 8, 0.3), ("b", 20, 0.5), ("c", 10, 1.0))
        profile = replacement_timeline(rows, 0.7, 25)["daily_gap_mbd"]
        self.assertEqual(len(profile), 25)
        for t, g in enumerate(profile):
            expected = 0.7 if t < 8 else (0.4 if t < 20 else 0.0)
            self.assertAlmostEqual(g, expected, places=9, msg=t)

    def test_daily_gap_keeps_the_residual_open(self):
        profile = replacement_timeline(self._rows(("a", 8, 0.3)), 1.0, 12)["daily_gap_mbd"]
        self.assertAlmostEqual(profile[7], 1.0)
        self.assertAlmostEqual(profile[8], 0.7)
        self.assertAlmostEqual(profile[-1], 0.7)

    def test_daily_gap_without_alternatives_is_constant(self):
        self.assertEqual(replacement_timeline([], 0.5, 4)["daily_gap_mbd"], [0.5] * 4)

    def test_daily_gap_only_when_duration_given(self):
        rows = self._rows(("a", 8, 0.3))
        self.assertNotIn("daily_gap_mbd", replacement_timeline(rows, 1.0))
        with self.assertRaises(ValueError):
            replacement_timeline(rows, 1.0, 0)

    def test_zero_gap_needs_nothing(self):
        t = replacement_timeline(self._rows(("a", 8, 0.3)), 0.0)
        self.assertEqual(t["alternatives_used"], [])
        self.assertTrue(t["covers_gap"])

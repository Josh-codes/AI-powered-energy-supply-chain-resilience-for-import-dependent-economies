"""Tests for response/plan.py::build_response.

Mocking convention: none. The reference is the Phase 5 chain composed BY HAND
from the four functions it wraps, so the test checks the composition rather
than the function against itself. Synthetic risk, seeded graph.
"""
from django.core.management import call_command
from django.test import TestCase

from graph.builder import build_graph
from graph.updater import update_edge_weights
from response.gap import estimate_supply_gap, gap_from_scenario
from response.plan import build_response
from response.reroute import rank_alternatives, replacement_timeline
from response.spr import compute_spr_schedule

SYNTHETIC_RISK = {"Hormuz": 0.615, "Red Sea": 0.537, "Cape": 0.05}


class BuildResponseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_db", verbosity=0)

    def setUp(self):
        self.G = build_graph(persist=False)
        update_edge_weights(self.G, SYNTHETIC_RISK)

    def _by_hand(self, gap, crisis="normal", duration=14):
        ranked = rank_alternatives(gap["corridor"], crisis=crisis, gap_mbd=gap["gap_mbd"])
        timeline = replacement_timeline(ranked, gap["gap_mbd"], duration)
        spr = compute_spr_schedule(
            gap["gap_mbd"], duration, timeline["transit_days"] or duration,
            gap_profile=timeline["daily_gap_mbd"],
        )
        return ranked, timeline, spr

    def test_corridor_chain_matches_the_hand_composition(self):
        got = build_response(self.G, corridor="Cape", crisis="severe", duration_days=30)
        gap = estimate_supply_gap("Cape", 100, G=self.G, duration_days=30)
        ranked, timeline, spr = self._by_hand(gap, "severe", 30)

        self.assertEqual(got["gap"], gap)
        self.assertEqual(got["reroute"], ranked)
        self.assertEqual(got["timeline"], timeline)
        self.assertEqual(got["spr"], spr)

    def test_scenario_chain_matches_the_hand_composition(self):
        got = build_response(self.G, scenario="opec_cut")
        gap = gap_from_scenario("opec_cut", G=self.G, duration_days=14)
        ranked, timeline, spr = self._by_hand(gap)

        self.assertEqual(got["gap"], gap)
        self.assertEqual(got["spr"], spr)
        self.assertIsNone(got["gap"]["corridor"])  # supply shock excludes no route

    def test_partial_degradation(self):
        got = build_response(self.G, corridor="Hormuz", degradation_pct=40)
        self.assertEqual(got["gap"]["degradation_pct"], 40)
        self.assertEqual(got["gap"], estimate_supply_gap("Hormuz", 40, G=self.G, duration_days=14))

    def test_does_not_mutate_the_graph(self):
        before = {(u, v): d["effective_capacity"] for u, v, d in self.G.edges(data=True)}
        build_response(self.G, corridor="Hormuz")
        after = {(u, v): d["effective_capacity"] for u, v, d in self.G.edges(data=True)}
        self.assertEqual(before, after)

    def test_input_errors(self):
        with self.assertRaises(ValueError):
            build_response(self.G)                                         # neither
        with self.assertRaises(ValueError):
            build_response(self.G, corridor="Hormuz", scenario="hormuz_30")  # both
        with self.assertRaises(ValueError):
            build_response(self.G, corridor="Suez")
        with self.assertRaises(ValueError):
            build_response(self.G, corridor="Hormuz", duration_days=0)
        with self.assertRaises(ValueError):
            build_response(self.G, corridor="Hormuz", crisis="mild")

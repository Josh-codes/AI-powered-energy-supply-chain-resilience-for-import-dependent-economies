"""Tests for response/gap.py — supply-gap estimation.

Mocking convention: none — deterministic graph math over the real seed data,
same as test_criticality.py. No network, no API spend.

What is and is not pinned to a number:
  * On a risk-FREE graph (effective_capacity == volume) the gap at 100%
    degradation IS the static corridor cut, which derives from committed seed
    data — pinned to CLAUDE.md's Phase 1 figures.
  * On a risk-weighted graph ``gap_mbd`` moves with the live corpus, so only
    properties and cross-path agreement are asserted.
"""
from django.core.management import call_command
from django.test import TestCase

from criticality.cascade import cascading_failure_simulation
from criticality.scenarios import run_scenario
from graph.builder import build_graph
from graph.updater import update_edge_weights
from response.gap import BASELINE, estimate_supply_gap, gap_from_scenario

# Documented in CLAUDE.md Phase 1; also STATIC_CUT_MBD in test_criticality.py.
STATIC_CUT_MBD = {"Hormuz": 1.784, "Cape": 1.609, "Red Sea": 0.202}
SYNTHETIC_RISK = {"Hormuz": 0.615, "Red Sea": 0.537, "Cape": 0.05}


class GapTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_db", verbosity=0)
        cls.G = build_graph(persist=False)
        cls.G_risk = build_graph(persist=False)
        update_edge_weights(cls.G_risk, SYNTHETIC_RISK)


class EstimateSupplyGapTests(GapTestBase):
    def test_full_cut_on_a_risk_free_graph_is_the_static_cut(self):
        for corridor, expected in STATIC_CUT_MBD.items():
            gap = estimate_supply_gap(corridor, 100, G=self.G)
            self.assertAlmostEqual(gap["gap_mbd"], expected, places=2, msg=corridor)

    def test_gap_agrees_with_the_cascade_final_step(self):
        """Two independently written paths — gap.py's single-point solve and
        cascade.py's 10-step loop — must land on the same number."""
        for corridor in STATIC_CUT_MBD:
            gap = estimate_supply_gap(corridor, 100, G=self.G_risk)["gap_mbd"]
            cascade = cascading_failure_simulation(corridor, G=self.G_risk)[-1]["capacity_loss_mbd"]
            self.assertAlmostEqual(gap, cascade, places=9, msg=corridor)

    def test_gap_agrees_with_every_cascade_step(self):
        steps = cascading_failure_simulation("Hormuz", G=self.G_risk)
        for step in steps:
            gap = estimate_supply_gap("Hormuz", step["degradation_pct"], G=self.G_risk)
            self.assertAlmostEqual(gap["gap_mbd"], step["capacity_loss_mbd"], places=9)

    def test_risk_weighted_gap_is_smaller_than_the_static_one(self):
        """Measured on top of today's risk: an already-degraded corridor has
        less left to lose."""
        for corridor in ("Hormuz", "Red Sea"):
            risk = estimate_supply_gap(corridor, 100, G=self.G_risk)["gap_mbd"]
            self.assertLess(risk, STATIC_CUT_MBD[corridor], msg=corridor)

    def test_gap_is_monotonic_in_degradation(self):
        gaps = [estimate_supply_gap("Cape", p, G=self.G_risk)["gap_mbd"] for p in range(0, 101, 20)]
        self.assertAlmostEqual(gaps[0], 0.0, places=9)
        for a, b in zip(gaps, gaps[1:]):
            self.assertLessEqual(a, b + 1e-9)

    def test_refinery_shortfalls_do_not_exceed_the_gap(self):
        """The 5% deadband: listed shortfalls sum to at most the gap, and the
        residual is reported rather than hidden."""
        for corridor in STATIC_CUT_MBD:
            for pct in (30, 70, 100):
                g = estimate_supply_gap(corridor, pct, G=self.G_risk)
                self.assertLessEqual(g["refinery_shortfall_mbd"], g["gap_mbd"] + 1e-9)
                self.assertAlmostEqual(
                    g["refinery_shortfall_mbd"] + g["deadband_unattributed_mbd"], g["gap_mbd"], places=9,
                )

    def test_baseline_is_labelled(self):
        g = estimate_supply_gap("Hormuz", 50, G=self.G_risk)
        self.assertEqual(g["baseline"], BASELINE)
        self.assertEqual(g["mechanism"], "corridor")
        self.assertIsNone(g["scenario"])

    def test_duration_scales_to_total_barrels(self):
        g = estimate_supply_gap("Hormuz", 100, G=self.G, duration_days=10)
        self.assertAlmostEqual(g["total_shortfall_mb"], g["gap_mbd"] * 10, places=9)
        self.assertIsNone(estimate_supply_gap("Hormuz", 100, G=self.G)["total_shortfall_mb"])

    def test_load_bearing_ports_are_reported(self):
        g = estimate_supply_gap("Hormuz", 100, G=self.G)
        self.assertTrue(g["load_bearing_ports"])
        self.assertTrue(all(v >= 0 for v in g["load_bearing_ports"].values()))

    def test_does_not_mutate_input(self):
        before = {(u, v): dict(d) for u, v, d in self.G_risk.edges(data=True)}
        estimate_supply_gap("Hormuz", 100, G=self.G_risk)
        after = {(u, v): dict(d) for u, v, d in self.G_risk.edges(data=True)}
        self.assertEqual(before, after)

    def test_unknown_corridor_raises_instead_of_reporting_zero(self):
        """degrade_corridor silently no-ops on a bad name; "Suez" is the
        likely one (folded into Red Sea in Phase 1)."""
        for bad in ("Suez", "Vadinar", "hormuz"):
            with self.assertRaises(ValueError, msg=bad):
                estimate_supply_gap(bad, 100, G=self.G)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            estimate_supply_gap("Hormuz", 120, G=self.G)
        with self.assertRaises(ValueError):
            estimate_supply_gap("Hormuz", 100, G=self.G, duration_days=0)


class GapFromScenarioTests(GapTestBase):
    def test_corridor_scenario_matches_run_scenario(self):
        g = gap_from_scenario("hormuz_full", G=self.G_risk)
        r = run_scenario("hormuz_full", G=self.G_risk)
        self.assertAlmostEqual(g["gap_mbd"], r["capacity_loss_mbd"], places=9)
        self.assertEqual(g["corridor"], "Hormuz")
        self.assertEqual(g["degradation_pct"], 100)

    def test_corridor_scenario_matches_estimate_supply_gap(self):
        g = gap_from_scenario("hormuz_30", G=self.G_risk)
        direct = estimate_supply_gap("Hormuz", 30, G=self.G_risk)
        self.assertAlmostEqual(g["gap_mbd"], direct["gap_mbd"], places=9)

    def test_supply_scenario_has_no_corridor(self):
        g = gap_from_scenario("opec_cut", G=self.G_risk)
        self.assertEqual(g["mechanism"], "supply")
        self.assertIsNone(g["corridor"])
        self.assertEqual(g["load_bearing_ports"], {})
        self.assertGreater(g["gap_mbd"], 0.0)

    def test_unknown_scenario_raises(self):
        with self.assertRaises(ValueError):
            gap_from_scenario("suez_closure", G=self.G)

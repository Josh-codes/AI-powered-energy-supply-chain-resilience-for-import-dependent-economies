"""Tests for response/trigger.py (the re-specified threshold) and the
``run_response`` management command.

Mocking convention: one patch, at the command's import site of
``evaluate_graph``, to reach the "nothing crossed" branch that the seeded graph
cannot produce. No network, no API spend.

What is and is not pinned to a number:
  * ``check_threshold`` is a pure function over rows, so its arithmetic is
    pinned exactly against synthetic rows.
  * Graph-backed tests apply SYNTHETIC risk, never the live corpus, so which
    corridors cross is asserted; the live loss fractions are not pinned.
"""
from io import StringIO
import unittest
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from core.models import Corridor
from graph.algorithms import capacity_weighted_betweenness
from graph.builder import build_graph, nodes_by_kind
from graph.state import GraphState
from graph.updater import update_edge_weights
from response.trigger import (
    LOSS_FRACTION_THRESHOLD,
    RISK_ALONE_THRESHOLD,
    check_threshold,
    evaluate_graph,
)

SYNTHETIC_RISK = {"Hormuz": 0.615, "Red Sea": 0.537, "Cape": 0.05}


def _row(corridor, loss, risk, centrality=0.05):
    return {"corridor": corridor, "capacity_loss_mbd": loss, "live_risk_score": risk,
            "centrality": centrality}


class CheckThresholdTests(unittest.TestCase):
    def test_low_risk_corridor_can_still_trigger(self):
        """The veto bug: Cape loses the most flow precisely BECAUSE it is
        intact (risk 0.050). Any `AND risk > X` gate would suppress it."""
        r = check_threshold([_row("Cape", 2.053, 0.05)], 3.039)
        self.assertTrue(r["threshold_crossed"])
        self.assertEqual(r["triggered_corridor"], "Cape")
        self.assertTrue(r["evaluated"][0]["loss_crossed"])
        self.assertFalse(r["evaluated"][0]["risk_crossed"])

    def test_original_spec_condition_rejects_the_same_row(self):
        """The same Cape row under CLAUDE.md's original condition."""
        row = _row("Cape", 2.053, 0.05, centrality=0.0655)
        self.assertFalse(row["centrality"] > 0.65 and row["live_risk_score"] > 0.50)

    def test_loss_fraction_is_relative_to_baseline(self):
        """The same 0.5 mb/d loss crosses on a 3 mb/d network (16.7%) and not
        on a 10 mb/d one (5%) — the threshold does not hardcode graph size."""
        self.assertTrue(check_threshold([_row("X", 0.5, 0.0)], 3.0)["threshold_crossed"])
        self.assertFalse(check_threshold([_row("X", 0.5, 0.0)], 10.0)["threshold_crossed"])

    def test_threshold_is_strict(self):
        at = LOSS_FRACTION_THRESHOLD * 4.0
        self.assertFalse(check_threshold([_row("X", at, 0.0)], 4.0)["threshold_crossed"])

    def test_risk_alone_or_clause(self):
        """An emerging crisis on a structurally minor corridor still fires."""
        r = check_threshold([_row("Red Sea", 0.037, 0.80)], 3.039)
        self.assertTrue(r["threshold_crossed"])
        e = r["evaluated"][0]
        self.assertFalse(e["loss_crossed"])
        self.assertTrue(e["risk_crossed"])
        self.assertIn("risk score", r["reason"])

    def test_nothing_crosses(self):
        r = check_threshold([_row("Red Sea", 0.037, 0.537)], 3.039)
        self.assertFalse(r["threshold_crossed"])
        self.assertIsNone(r["triggered_corridor"])
        self.assertEqual(r["triggered"], [])

    def test_largest_loss_fraction_wins_regardless_of_input_order(self):
        rows = [_row("Hormuz", 0.833, 0.615), _row("Cape", 2.053, 0.05), _row("Red Sea", 0.037, 0.537)]
        for ordering in (rows, list(reversed(rows))):
            r = check_threshold(ordering, 3.039)
            self.assertEqual(r["triggered_corridor"], "Cape")
            self.assertEqual(r["triggered"], ["Cape", "Hormuz"])

    def test_loss_fraction_arithmetic(self):
        r = check_threshold([_row("Cape", 2.053, 0.05)], 3.039)
        self.assertAlmostEqual(r["evaluated"][0]["loss_fraction"], 2.053 / 3.039, places=12)

    def test_zero_baseline_leaves_only_the_risk_clause(self):
        r = check_threshold([_row("X", 1.0, 0.1), _row("Y", 1.0, 0.9)], 0.0)
        self.assertEqual(r["triggered"], ["Y"])

    def test_defaults(self):
        r = check_threshold([], 3.0)
        self.assertEqual(r["loss_fraction_threshold"], LOSS_FRACTION_THRESHOLD)
        self.assertEqual(r["risk_alone_threshold"], RISK_ALONE_THRESHOLD)

    def test_invalid_thresholds_raise(self):
        for kwargs in ({"loss_fraction": 0}, {"loss_fraction": 1.5}, {"risk_alone": 0}, {"risk_alone": 2}):
            with self.assertRaises(ValueError, msg=kwargs):
                check_threshold([], 3.0, **kwargs)


class EvaluateGraphTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_db", verbosity=0)
        cls.G = build_graph(persist=False)
        update_edge_weights(cls.G, SYNTHETIC_RISK)

    def test_original_spec_threshold_could_never_fire(self):
        """The Phase 5 blocker, pinned. Across a full risk sweep, no corridor's
        capacity-weighted betweenness gets anywhere near 0.65, so
        `centrality > 0.65 AND risk > 0.50` is unreachable on this graph."""
        for risk in (0.0, 0.3, 0.6, 0.9, 0.99):
            G = build_graph(persist=False)
            update_edge_weights(G, {"Hormuz": risk, "Red Sea": risk, "Cape": 0.05})
            bc = capacity_weighted_betweenness(G)
            for corridor in nodes_by_kind(G, "corridor"):
                self.assertLess(bc[corridor], 0.65, msg=(corridor, risk))

    def test_the_intact_corridor_triggers_on_the_real_graph(self):
        r = evaluate_graph(self.G)
        self.assertTrue(r["threshold_crossed"])
        self.assertEqual(r["triggered_corridor"], "Cape")
        self.assertIn("Hormuz", r["triggered"])
        self.assertNotIn("Red Sea", r["triggered"])

    def test_carries_the_criticality_rows(self):
        r = evaluate_graph(self.G)
        self.assertEqual({row["corridor"] for row in r["criticality"]}, {"Hormuz", "Red Sea", "Cape"})

    def test_does_not_mutate_input(self):
        before = {(u, v): dict(d) for u, v, d in self.G.edges(data=True)}
        evaluate_graph(self.G)
        self.assertEqual(before, {(u, v): dict(d) for u, v, d in self.G.edges(data=True)})

    def test_missing_graph_raises(self):
        GraphState.get_instance().clear()
        with self.assertRaises(ValueError):
            evaluate_graph()


class RunResponseCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_db", verbosity=0)
        for name, risk in SYNTHETIC_RISK.items():
            Corridor.objects.filter(name=name).update(live_risk_score=risk)

    def tearDown(self):
        GraphState.get_instance().clear()  # the command persists into the singleton

    def _run(self, **kwargs):
        out = StringIO()
        call_command("run_response", stdout=out, **kwargs)
        return out.getvalue()

    def test_auto_responds_to_the_triggered_corridor(self):
        out = self._run(auto=True)
        self.assertIn("TRIGGERED: Cape", out)
        self.assertIn("Supply gap: Cape at 100% degradation", out)
        self.assertIn("Reroute ranking", out)
        self.assertIn("SPR drawdown", out)
        self.assertIn("status           : Optimal", out)

    def test_auto_does_nothing_when_nothing_crosses(self):
        """On the seeded graph a full Hormuz or Cape cut always exceeds 15% of
        flow, so the no-trigger branch is unreachable with real data — patch
        the trigger result instead (patched at the command's import site)."""
        quiet = check_threshold([_row("Red Sea", 0.01, 0.1)], 3.0)
        quiet["criticality"] = []
        with patch("core.management.commands.run_response.evaluate_graph", return_value=quiet):
            out = self._run(auto=True)
        self.assertIn("Not triggered", out)
        self.assertIn("no recommendations generated", out)
        self.assertNotIn("SPR drawdown", out)

    def test_spr_covers_the_residual_and_can_run_dry(self):
        """Cape's alternatives never close its gap, so over a long horizon the
        SPR keeps covering the residual after the last cargo and eventually
        runs out, rather than stopping at the last arrival."""
        out = self._run(corridor="Cape", duration_days=60)
        self.assertIn("gap by day", out)
        self.assertIn("reserve runs dry on day", out)
        self.assertIn("the crisis outlasts this horizon", out)

    def test_no_target_prints_a_hint(self):
        out = self._run()
        self.assertIn("Pass --corridor NAME, --scenario KEY or --auto", out)
        self.assertNotIn("SPR drawdown", out)

    def test_red_sea_never_offers_a_suez_route(self):
        out = self._run(corridor="Red Sea")
        self.assertNotIn("Saudi Arabia via Yanbu", out)
        self.assertIn("UAE via Fujairah", out)

    def test_sanctioned_only_with_flag(self):
        self.assertNotIn("Russia Urals", self._run(corridor="Hormuz"))
        self.assertIn("Russia Urals", self._run(corridor="Hormuz", include_sanctioned=True))

    def test_supply_scenario(self):
        out = self._run(scenario="opec_cut")
        self.assertIn("scenario opec_cut", out)
        self.assertIn("supply shock", out)

    def test_unknown_corridor_is_an_error_not_a_crash(self):
        out = self._run(corridor="Suez")
        self.assertIn("unknown corridor 'Suez'", out)
        self.assertNotIn("SPR drawdown", out)

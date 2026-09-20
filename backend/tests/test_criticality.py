"""Tests for graph/algorithms.py and the criticality/ app.

Mocking convention: none needed — everything here is deterministic graph math
over the real seed data. No network, no API spend.

What is and is not pinned to a number:
  * STATIC quantities (cuts on ``volume``, stranded port volumes) ARE pinned.
    They derive from committed seed data that live risk never mutates, so they
    are safe regression anchors — same rationale as test_graph.py's 4.228
    baseline assertion.
  * RISK-WEIGHTED quantities (capacity_loss_mbd, centrality) are NOT pinned.
    They move with the live corpus and the still-uncalibrated SATURATION_K, so
    hardcoding them would produce false regressions. Properties and bounds are
    asserted instead.
"""
import networkx as nx
from django.core.management import call_command
from django.test import TestCase

from criticality.cascade import cascading_failure_simulation, check_refineries
from criticality.engine import compute_criticality, compute_port_criticality
from criticality.scenarios import SCENARIOS, run_scenario
from graph.algorithms import (
    baseline_max_flow,
    capacity_weighted_betweenness,
    corridor_load_bearing_ports,
    degrade_corridor,
    degrade_supply,
    residual_port_criticality,
    risk_weighted_max_flow,
    structural_betweenness,
)
from graph.builder import SINK, SOURCE, build_graph, nodes_by_kind
from graph.updater import update_edge_weights

# Documented in CLAUDE.md Phase 1 and reproduced by build_graph's own cut table.
STATIC_CUT_MBD = {"Hormuz": 1.784, "Cape": 1.609, "Red Sea": 0.202}

# edges.json validation block: crude routed to a port with no matching
# refinery offtake to place it.
STRANDED_MBD = {
    "Chennai": 0.272, "Mumbai JNPT": 0.168, "Paradip": 0.157,
    "Sikka": 0.053, "Vizag": 0.049,
}


class CriticalityTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_db", verbosity=0)
        cls.G = build_graph(persist=False)


class AlgorithmsTests(CriticalityTestBase):
    def test_baseline_max_flow_matches_refinery_deliverable(self):
        flow, _ = baseline_max_flow(self.G)
        deliverable = sum(
            d["volume"] for u, v, d in self.G.edges(data=True)
            if self.G.nodes[u].get("kind") == "port"
            and self.G.nodes[v].get("kind") == "refinery"
        )
        self.assertAlmostEqual(flow, deliverable, places=2)

    def test_risk_and_baseline_flow_agree_on_a_fresh_graph(self):
        self.assertAlmostEqual(baseline_max_flow(self.G)[0], risk_weighted_max_flow(self.G)[0], places=6)

    def test_degrade_corridor_does_not_mutate_input(self):
        before = {
            (u, v): d["effective_capacity"] for u, v, d in self.G.edges(data=True)
        }
        degrade_corridor(self.G, "Hormuz", 50)
        after = {
            (u, v): d["effective_capacity"] for u, v, d in self.G.edges(data=True)
        }
        self.assertEqual(before, after)

    def test_degrade_corridor_scales_only_tagged_edges(self):
        H = degrade_corridor(self.G, "Hormuz", 50)
        for u, v, d in H.edges(data=True):
            original = self.G[u][v]["effective_capacity"]
            expected = original * 0.5 if d.get("corridor") == "Hormuz" else original
            self.assertAlmostEqual(d["effective_capacity"], expected, places=9, msg=f"{u}->{v}")

    def test_degrade_corridor_rejects_out_of_range_pct(self):
        for bad in (-1, 101):
            with self.assertRaises(ValueError):
                degrade_corridor(self.G, "Hormuz", bad)

    def test_static_corridor_cuts_match_documented_deltas(self):
        """Regression anchor: these come from static `volume` seed data and are
        independent of live risk / SATURATION_K, so they are safe to pin."""
        base, _ = baseline_max_flow(self.G)
        for corridor, expected_loss in STATIC_CUT_MBD.items():
            H = degrade_corridor(self.G, corridor, 100, capacity_attr="volume")
            cut, _ = nx.maximum_flow(H, SOURCE, SINK, capacity="volume")
            self.assertAlmostEqual(base - cut, expected_loss, places=2, msg=corridor)

    def test_degrade_supply_reduces_source_outflow_by_the_requested_amount(self):
        total_before = sum(
            d["effective_capacity"] for _u, _v, d in self.G.out_edges(SOURCE, data=True)
        )
        H = degrade_supply(self.G, 2.0)
        total_after = sum(
            d["effective_capacity"] for _u, _v, d in H.out_edges(SOURCE, data=True)
        )
        self.assertAlmostEqual(total_before - total_after, 2.0, places=6)

    def test_degrade_supply_touches_only_source_edges(self):
        H = degrade_supply(self.G, 1.0)
        for u, v, d in H.edges(data=True):
            if u != SOURCE:
                self.assertAlmostEqual(
                    d["effective_capacity"], self.G[u][v]["effective_capacity"], places=9
                )

    def test_degrade_supply_cannot_drive_capacity_negative(self):
        H = degrade_supply(self.G, 10_000.0)
        for _u, _v, d in H.out_edges(SOURCE, data=True):
            self.assertGreaterEqual(d["effective_capacity"], 0.0)

    def test_structural_betweenness_ranks_hormuz_highest(self):
        bc = structural_betweenness(self.G)
        top = max(nodes_by_kind(self.G, "corridor"), key=lambda n: bc[n])
        self.assertEqual(top, "Hormuz")

    def test_capacity_weighted_betweenness_direction_is_correct(self):
        """The bug-fix proof. CLAUDE.md's literal `weight='effective_capacity'`
        treats capacity as DISTANCE, so throttling a corridor would make it
        look MORE central. With the 1/capacity distance transform, throttling
        a corridor must make it LESS central."""
        before = capacity_weighted_betweenness(self.G)["Hormuz"]
        starved = degrade_corridor(self.G, "Hormuz", 99)
        after = capacity_weighted_betweenness(starved)["Hormuz"]
        self.assertLess(after, before)

    def test_capacity_weighted_betweenness_survives_a_fully_closed_corridor(self):
        """Zero effective_capacity must not raise (EPS floor) — a fully closed
        corridor is a legitimate model state, not an error."""
        closed = degrade_corridor(self.G, "Hormuz", 100)
        bc = capacity_weighted_betweenness(closed)
        self.assertEqual(len(bc), closed.number_of_nodes())


class ResidualCriticalityTests(CriticalityTestBase):
    def test_stranded_volumes_reproduce_documented_figures(self):
        rows = {r["port"]: r for r in compute_port_criticality(self.G)}
        for port, expected in STRANDED_MBD.items():
            self.assertAlmostEqual(rows[port]["stranded_mbd"], expected, places=3, msg=port)

    def test_ports_not_listed_as_oversupplied_have_no_stranded_crude(self):
        for row in compute_port_criticality(self.G):
            if row["port"] not in STRANDED_MBD:
                self.assertAlmostEqual(row["stranded_mbd"], 0.0, places=6, msg=row["port"])

    def test_single_corridor_port_has_full_residual_need(self):
        """A port fed by exactly one corridor has no redundancy, so that
        corridor's residual need is its full downstream refinery draw."""
        single = [
            p for p in nodes_by_kind(self.G, "port")
            if len({d["corridor"] for _u, _v, d in self.G.in_edges(p, data=True)
                    if d.get("corridor")}) == 1
        ]
        self.assertTrue(single, "seed data has no single-corridor port to test")
        for port in single:
            residual = residual_port_criticality(self.G, port)
            need = sum(d["volume"] for _u, _v, d in self.G.out_edges(port, data=True))
            self.assertAlmostEqual(sum(residual.values()), need, places=6, msg=port)

    def test_oversupplied_port_discounts_its_corridors(self):
        """The whole point of the redundancy correction: at an oversupplied
        port, residual need must be strictly below naive inbound volume."""
        for port in STRANDED_MBD:
            residual = residual_port_criticality(self.G, port)
            naive = sum(
                d["volume"] for _u, _v, d in self.G.in_edges(port, data=True)
                if d.get("corridor")
            )
            self.assertLess(sum(residual.values()), naive, msg=port)

    def test_residual_need_is_never_negative(self):
        for port in nodes_by_kind(self.G, "port"):
            for corridor, value in residual_port_criticality(self.G, port).items():
                self.assertGreaterEqual(value, 0.0, msg=f"{port}/{corridor}")

    def test_corridor_load_bearing_ports_covers_every_port_it_feeds(self):
        for corridor in nodes_by_kind(self.G, "corridor"):
            fed = {
                v for _u, v, d in self.G.out_edges(corridor, data=True)
                if d.get("corridor") == corridor
            }
            self.assertEqual(set(corridor_load_bearing_ports(self.G, corridor)), fed, msg=corridor)


class EngineTests(CriticalityTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # a risk-differentiated graph, so static and risk-weighted views diverge
        cls.G_risk = build_graph(persist=False)
        update_edge_weights(cls.G_risk, {"Hormuz": 0.90, "Red Sea": 0.50, "Cape": 0.05})

    def test_returns_every_corridor_with_all_documented_keys(self):
        rows = compute_criticality(self.G)
        self.assertEqual(len(rows), len(nodes_by_kind(self.G, "corridor")))
        expected_keys = {
            "corridor", "static_rank", "risk_rank", "rank_shift", "centrality",
            "static_centrality", "capacity_loss_mbd", "static_capacity_loss_mbd",
            "live_risk_score", "residual_load_bearing_mbd",
        }
        for row in rows:
            self.assertEqual(set(row), expected_keys)

    def test_ranks_are_permutations_of_one_through_n(self):
        rows = compute_criticality(self.G_risk)
        n = len(rows)
        self.assertEqual(sorted(r["static_rank"] for r in rows), list(range(1, n + 1)))
        self.assertEqual(sorted(r["risk_rank"] for r in rows), list(range(1, n + 1)))

    def test_rank_shift_is_the_difference_of_the_two_ranks(self):
        for row in compute_criticality(self.G_risk):
            self.assertEqual(row["rank_shift"], row["static_rank"] - row["risk_rank"])

    def test_rows_are_sorted_by_risk_rank(self):
        rows = compute_criticality(self.G_risk)
        self.assertEqual([r["risk_rank"] for r in rows], sorted(r["risk_rank"] for r in rows))

    def test_static_losses_match_documented_deltas(self):
        for row in compute_criticality(self.G_risk):
            self.assertAlmostEqual(
                row["static_capacity_loss_mbd"],
                STATIC_CUT_MBD[row["corridor"]],
                places=2, msg=row["corridor"],
            )

    def test_static_ranking_is_unaffected_by_live_risk(self):
        """The static half must be a pure structural baseline — if risk leaked
        into it, the whole static-vs-risk delta would be meaningless."""
        plain = {r["corridor"]: r["static_rank"] for r in compute_criticality(self.G)}
        risky = {r["corridor"]: r["static_rank"] for r in compute_criticality(self.G_risk)}
        self.assertEqual(plain, risky)

    def test_capacity_losses_stay_within_sane_bounds(self):
        static_base, _ = baseline_max_flow(self.G_risk)
        risk_base, _ = risk_weighted_max_flow(self.G_risk)
        for row in compute_criticality(self.G_risk):
            self.assertGreaterEqual(row["capacity_loss_mbd"], -1e-9)
            self.assertLessEqual(row["capacity_loss_mbd"], risk_base + 1e-9)
            self.assertGreaterEqual(row["static_capacity_loss_mbd"], -1e-9)
            self.assertLessEqual(row["static_capacity_loss_mbd"], static_base + 1e-9)

    def test_risk_weighted_flow_never_exceeds_static_flow(self):
        """effective_capacity <= volume always holds by construction of
        update_edge_weights' clamp, so risk can only reduce flow."""
        self.assertLessEqual(
            risk_weighted_max_flow(self.G_risk)[0], baseline_max_flow(self.G_risk)[0] + 1e-9
        )

    def test_heavily_degraded_corridor_loses_rank(self):
        """Mechanism check for the thesis's headline claim: a corridor already
        throttled by risk has less REMAINING flow to lose, so a corridor left
        intact rises past it in the risk-weighted ranking."""
        G = build_graph(persist=False)
        update_edge_weights(G, {"Hormuz": 0.99, "Red Sea": 0.0, "Cape": 0.0})
        rows = {r["corridor"]: r for r in compute_criticality(G)}
        self.assertEqual(rows["Hormuz"]["static_rank"], 1)
        self.assertGreater(rows["Hormuz"]["risk_rank"], 1)
        self.assertLess(rows["Hormuz"]["rank_shift"], 0)

    def test_rank_by_centrality_is_accepted_and_changes_the_ranking_key(self):
        rows = compute_criticality(self.G_risk, rank_by="centrality")
        ordered = sorted(rows, key=lambda r: r["risk_rank"])
        centralities = [r["centrality"] for r in ordered]
        self.assertEqual(centralities, sorted(centralities, reverse=True))

    def test_rank_by_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            compute_criticality(self.G, rank_by="vibes")

    def test_warns_when_risk_scores_never_reached_the_edges(self):
        """The silent-duplication failure mode: a graph straight out of
        build_graph has effective_capacity == volume, so risk_rank mirrors
        static_rank and rank_shift is all zeros — indistinguishable from a
        real 'no shift' finding unless something says so."""
        G = build_graph(persist=False)
        G.nodes["Hormuz"]["live_risk_score"] = 0.9  # scored, but edges untouched
        with self.assertLogs("criticality.engine", level="WARNING") as captured:
            rows = compute_criticality(G)
        self.assertIn("Hormuz", "\n".join(captured.output))
        self.assertTrue(all(r["rank_shift"] == 0 for r in rows))

    def test_does_not_warn_when_risk_has_been_applied(self):
        with self.assertNoLogs("criticality.engine", level="WARNING"):
            compute_criticality(self.G_risk)

    def test_does_not_warn_on_a_genuinely_riskless_graph(self):
        """A graph with no risk anywhere is a valid thing to analyse
        (--ignore-risk), so it must not be flagged."""
        G = build_graph(persist=False)
        for corridor in nodes_by_kind(G, "corridor"):
            G.nodes[corridor]["live_risk_score"] = 0.0
        with self.assertNoLogs("criticality.engine", level="WARNING"):
            compute_criticality(G)

    def test_missing_graph_raises_rather_than_returning_empty(self):
        from graph.state import GraphState

        state = GraphState.get_instance()
        state.clear()
        with self.assertRaises(ValueError):
            compute_criticality()


class CascadeTests(CriticalityTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.steps = cascading_failure_simulation("Hormuz", G=cls.G)

    def test_produces_one_row_per_ten_percent_step(self):
        self.assertEqual([s["degradation_pct"] for s in self.steps], list(range(10, 101, 10)))

    def test_custom_step_size_is_honoured(self):
        steps = cascading_failure_simulation("Hormuz", G=self.G, step_pct=25)
        self.assertEqual([s["degradation_pct"] for s in steps], [25, 50, 75, 100])

    def test_capacity_loss_is_monotonic_in_degradation(self):
        losses = [s["capacity_loss_mbd"] for s in self.steps]
        for earlier, later in zip(losses, losses[1:]):
            self.assertGreaterEqual(later, earlier - 1e-9)

    def test_affected_count_is_monotonic_in_degradation(self):
        counts = [s["affected_count"] for s in self.steps]
        for earlier, later in zip(counts, counts[1:]):
            self.assertGreaterEqual(later, earlier)

    def test_full_closure_matches_the_engines_static_cut(self):
        """Cross-validates cascade.py against engine.py: on a risk-neutral
        graph the 100% cascade step must equal the documented static cut,
        computed by an independently written code path."""
        self.assertAlmostEqual(
            self.steps[-1]["capacity_loss_mbd"], STATIC_CUT_MBD["Hormuz"], places=2
        )

    def test_unknown_corridor_raises(self):
        with self.assertRaises(ValueError):
            cascading_failure_simulation("Atlantis", G=self.G)

    def test_does_not_mutate_the_graph_it_is_given(self):
        before = {(u, v): d["effective_capacity"] for u, v, d in self.G.edges(data=True)}
        cascading_failure_simulation("Hormuz", G=self.G)
        after = {(u, v): d["effective_capacity"] for u, v, d in self.G.edges(data=True)}
        self.assertEqual(before, after)

    def test_affected_refineries_carry_a_real_shortfall(self):
        for step in self.steps:
            for a in step["affected_refineries"]:
                self.assertGreater(a["shortfall_mbd"], 0.0)
                self.assertLess(a["pct_of_baseline"], 1.0)

    def test_affected_refineries_are_sorted_by_shortfall(self):
        for step in self.steps:
            shortfalls = [a["shortfall_mbd"] for a in step["affected_refineries"]]
            self.assertEqual(shortfalls, sorted(shortfalls, reverse=True))

    def test_no_refinery_is_flagged_when_nothing_changed(self):
        """Guards against float dust from the max-flow solver flagging
        everything as affected — the reason threshold_pct is not 1.0."""
        _flow, flow_dict = nx.maximum_flow(self.G, SOURCE, SINK, capacity="effective_capacity")
        self.assertEqual(check_refineries(self.G, flow_dict, flow_dict), [])

    def test_min_run_rate_mode_is_explicitly_unimplemented(self):
        _flow, flow_dict = nx.maximum_flow(self.G, SOURCE, SINK, capacity="effective_capacity")
        with self.assertRaises(NotImplementedError):
            check_refineries(self.G, flow_dict, flow_dict, min_run_rate_aware=True)


class ScenarioTests(CriticalityTestBase):
    def test_every_scenario_declares_the_keys_its_mechanism_needs(self):
        for key, scenario in SCENARIOS.items():
            self.assertIn("name", scenario, key)
            self.assertIn("source", scenario, key)
            self.assertIn(scenario["mechanism"], ("corridor", "supply"), key)
            if scenario["mechanism"] == "corridor":
                self.assertIn(scenario["corridor"], self.G, key)
                self.assertIsNotNone(scenario["degradation_pct"], key)
            else:
                self.assertIsNone(scenario["corridor"], key)
                self.assertGreater(scenario["supply_reduction_mbd"], 0, key)

    def test_every_scenario_runs_and_reports_a_loss(self):
        for key in SCENARIOS:
            result = run_scenario(key, G=self.G)
            self.assertGreaterEqual(result["capacity_loss_mbd"], 0.0, key)
            self.assertEqual(result["key"], key)
            self.assertEqual(result["affected_count"], len(result["affected_refineries"]), key)

    def test_full_closure_costs_at_least_as_much_as_partial(self):
        partial = run_scenario("hormuz_30", G=self.G)["capacity_loss_mbd"]
        full = run_scenario("hormuz_full", G=self.G)["capacity_loss_mbd"]
        self.assertGreaterEqual(full, partial)

    def test_full_closure_matches_the_documented_static_cut(self):
        result = run_scenario("hormuz_full", G=self.G)
        self.assertAlmostEqual(result["capacity_loss_mbd"], STATIC_CUT_MBD["Hormuz"], places=2)

    def test_supply_scenario_uses_the_supply_mechanism_not_a_corridor_cut(self):
        """opec_cut must bite even though it names no corridor — if it were
        silently routed through degrade_corridor it would be a no-op."""
        result = run_scenario("opec_cut", G=self.G)
        self.assertGreater(result["capacity_loss_mbd"], 0.0)

    def test_unknown_scenario_raises(self):
        with self.assertRaises(ValueError):
            run_scenario("hormuz_eleventy", G=self.G)

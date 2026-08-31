"""Tests for graph/builder.py and graph/state.py.

Loads the real seed data into the test database, builds the graph, and asserts
the structural + flow properties the criticality engine depends on.
"""
import networkx as nx
from django.core.management import call_command
from django.test import TestCase

from graph.builder import SINK, SOURCE, build_graph, nodes_by_kind
from graph.state import GraphState

EXPECTED = {"supplier": 12, "corridor": 3, "port": 10, "refinery": 23}


class GraphBuildTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_db", verbosity=0)
        cls.G = build_graph(persist=False)

    # ---- structure ------------------------------------------------------
    def test_node_counts(self):
        for kind, n in EXPECTED.items():
            self.assertEqual(len(nodes_by_kind(self.G, kind)), n, kind)
        self.assertEqual(self.G.number_of_nodes(), sum(EXPECTED.values()) + 2)

    def test_source_and_sink_connected(self):
        self.assertIn(SOURCE, self.G)
        self.assertIn(SINK, self.G)
        self.assertTrue(nx.has_path(self.G, SOURCE, SINK))

    def test_every_supplier_fed_from_source(self):
        for s in nodes_by_kind(self.G, "supplier"):
            self.assertTrue(self.G.has_edge(SOURCE, s), s)

    def test_every_corridor_has_in_and_out(self):
        for c in nodes_by_kind(self.G, "corridor"):
            self.assertGreater(self.G.in_degree(c), 0, c)
            self.assertGreater(self.G.out_degree(c), 0, c)

    def test_every_refinery_has_inbound_and_sink_edge(self):
        for r in nodes_by_kind(self.G, "refinery"):
            self.assertGreater(self.G.in_degree(r), 0, r)
            self.assertTrue(self.G.has_edge(r, SINK), r)

    # ---- edge attributes ---------------------------------------------------
    def test_all_edges_carry_flow_attributes(self):
        for u, v, d in self.G.edges(data=True):
            self.assertIn("volume", d)
            self.assertIn("capacity", d)
            self.assertIn("effective_capacity", d)
            self.assertEqual(d["effective_capacity"], d["volume"])
            self.assertEqual(d["capacity"], d["volume"])

    def test_only_corridor_to_port_edges_are_tagged(self):
        corridors = set(nodes_by_kind(self.G, "corridor"))
        ports = set(nodes_by_kind(self.G, "port"))
        tagged = [
            (u, v) for u, v, d in self.G.edges(data=True)
            if d.get("corridor") is not None
        ]
        self.assertTrue(tagged)
        for u, v in tagged:
            self.assertIn(u, corridors)
            self.assertIn(v, ports)

    # ---- flow behaviour the criticality engine relies on ------------------
    def test_baseline_maxflow_matches_refinery_deliverable(self):
        """The graph has two distinct ceilings (see edges.json validation):
        corridor throughput (~4.93 mb/d of crude reaching ports) and
        refinery-deliverable (~4.23 mb/d after refinery offtake limits).
        Baseline max-flow is bound by the latter, not by total supply --
        crude stranded at an oversupplied port produces no fuel."""
        flow, _ = nx.maximum_flow(self.G, SOURCE, SINK, capacity="volume")
        supply = sum(d["volume"] for _, _, d in self.G.out_edges(SOURCE, data=True))
        deliverable = sum(
            d["volume"] for u, v, d in self.G.edges(data=True)
            if self.G.nodes[u].get("kind") == "port"
            and self.G.nodes[v].get("kind") == "refinery"
        )
        self.assertLessEqual(flow, supply + 1e-6)          # can't route more than supplied
        self.assertAlmostEqual(flow, deliverable, places=2)  # binding constraint is refinery offtake

    def test_maxflow_works_on_effective_capacity_too(self):
        f_vol, _ = nx.maximum_flow(self.G, SOURCE, SINK, capacity="volume")
        f_eff, _ = nx.maximum_flow(self.G, SOURCE, SINK, capacity="effective_capacity")
        self.assertAlmostEqual(f_vol, f_eff, places=6)

    def test_cutting_each_corridor_reduces_maxflow(self):
        base, _ = nx.maximum_flow(self.G, SOURCE, SINK, capacity="volume")
        for c in nodes_by_kind(self.G, "corridor"):
            H = self.G.copy()
            for _u, _v, d in H.edges(data=True):
                if d.get("corridor") == c:
                    d["volume"] = 0.0
            cut, _ = nx.maximum_flow(H, SOURCE, SINK, capacity="volume")
            self.assertLess(cut, base - 1e-6, f"cutting {c} left flow unchanged")

    def test_betweenness_centrality_runs(self):
        bc = nx.betweenness_centrality(self.G)
        self.assertEqual(len(bc), self.G.number_of_nodes())
        top_corridor = max(
            nodes_by_kind(self.G, "corridor"), key=lambda n: bc[n]
        )
        self.assertEqual(top_corridor, "Hormuz")

    # ---- singleton -------------------------------------------------------
    def test_persist_populates_singleton(self):
        state = GraphState.get_instance()
        state.clear()
        self.assertFalse(state.is_loaded())
        build_graph(persist=True)
        self.assertTrue(state.is_loaded())
        self.assertIsNotNone(state.built_at)
        # the live graph must not be the caller's to mutate
        self.assertIsNot(state.get_graph(), state.get_graph_copy())
        state.clear()

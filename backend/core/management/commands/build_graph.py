"""Build the NetworkX knowledge graph from the DB + data/edges.json and print a
summary: node/edge counts, baseline max-flow, and the max-flow lost when each
corridor is fully cut. Use this to sanity-check seed data before the criticality
engine runs.

    python manage.py build_graph
"""
import networkx as nx
from django.core.management.base import BaseCommand

from graph.builder import SINK, SOURCE, build_graph, nodes_by_kind


class Command(BaseCommand):
    help = "Build the crude-import graph and print a summary."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-persist", action="store_true",
            help="Do not store the built graph in the GraphState singleton.",
        )

    def handle(self, *args, **options):
        G = build_graph(persist=not options["no_persist"])

        w = self.stdout.write
        ok = self.style.SUCCESS
        warn = self.style.WARNING

        w(ok(f"\nGraph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges"))
        for kind in ("supplier", "corridor", "port", "refinery"):
            w(f"  {kind:9}: {len(nodes_by_kind(G, kind))}")

        base, _ = nx.maximum_flow(G, SOURCE, SINK, capacity="volume")
        supply = sum(d["volume"] for _, _, d in G.out_edges(SOURCE, data=True))
        w(ok(f"\nBaseline max-flow  : {base:.3f} mb/d"))
        w(f"Modelled supply    : {supply:.3f} mb/d")
        w(f"Unroutable at base : {supply - base:.3f} mb/d")

        w("\nCorridor cut test (flow when that corridor -> port capacity = 0):")
        w(f"  {'corridor':10} {'flow_after':>10} {'flow_lost':>10}")
        for c in sorted(nodes_by_kind(G, "corridor")):
            H = G.copy()
            for u, v, d in H.edges(data=True):
                if d.get("corridor") == c:
                    d["volume"] = 0.0
            cut, _ = nx.maximum_flow(H, SOURCE, SINK, capacity="volume")
            tag = "" if (base - cut) > 1e-6 else "   <-- no impact!"
            style = (lambda s: s) if (base - cut) > 1e-6 else warn
            w(style(f"  {c:10} {cut:10.3f} {base - cut:10.3f}{tag}"))

        # cheap centrality read (unweighted) just to confirm it runs
        bc = nx.betweenness_centrality(G)
        top = sorted(bc.items(), key=lambda kv: kv[1], reverse=True)[:5]
        w("\nTop betweenness (unweighted):")
        for name, score in top:
            w(f"  {name:12} {score:.3f}")

        orphan_ref = [
            n for n in nodes_by_kind(G, "refinery")
            if G.in_degree(n) == 0
        ]
        if orphan_ref:
            w(warn(f"\nRefineries with no inbound edge: {orphan_ref}"))
        else:
            w(ok("\nAll refineries have an inbound edge."))

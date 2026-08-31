"""Render the crude-import graph to a PNG (layered SOURCE -> ... -> SINK view).

    python manage.py visualize_graph
    python manage.py visualize_graph --out figs/network.png --dpi 300

Node size = throughput (corridor = India inflow, refinery = nameplate),
edge width = nominal volume, the three corridor -> port layers coloured by
corridor. Regenerates from the live database each run, so it tracks seed-data
changes.
"""
from pathlib import Path

import networkx as nx
from django.conf import settings
from django.core.management.base import BaseCommand

from graph.builder import SINK, SOURCE, build_graph

# kind -> column index (SOURCE and SINK share the "virtual" kind, split by id)
COL = {"supplier": 1, "corridor": 2, "port": 3, "refinery": 4}
COL_LABEL = ["source", "suppliers", "corridors", "ports", "refineries", "sink"]

COLOR = {
    "supplier": "#8499a0",
    "port": "#45585f",
    "refinery": "#7b8a8f",
    "virtual": "#b7c2c4",
    "Hormuz": "#c25e1a",
    "Red Sea": "#0e7c8b",
    "Cape": "#5f7d43",
}
EDGE_GREY = "#aeb9bd"


class Command(BaseCommand):
    help = "Render the crude-import graph to a PNG."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="graph/graph.png",
                            help="output path (relative to backend/ or absolute)")
        parser.add_argument("--dpi", type=int, default=200)
        parser.add_argument("--width", type=float, default=15.0, help="figure width (in)")
        parser.add_argument("--height", type=float, default=11.0, help="figure height (in)")

    def handle(self, *args, **opts):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        G = build_graph(persist=False)

        corr_inflow = {}
        for c in [n for n, d in G.nodes(data=True) if d.get("kind") == "corridor"]:
            corr_inflow[c] = sum(d["volume"] for _u, _v, d in G.in_edges(c, data=True))

        def column(node, kind):
            if kind == "virtual":
                return 0 if node == SOURCE else 5
            return COL[kind]

        # group nodes by column, keep insertion order from the builder
        cols = {i: [] for i in range(6)}
        for n, d in G.nodes(data=True):
            cols[column(n, d["kind"])].append(n)

        # vertical span each column is allowed to occupy (corridors sit centred
        # so their fan-out to ports stays legible)
        SPAN = {0: (0.5, 0.5), 1: (0.0, 1.0), 2: (0.30, 0.70),
                3: (0.0, 1.0), 4: (0.0, 1.0), 5: (0.5, 0.5)}

        pos, size, color = {}, {}, {}
        for ci, nodes in cols.items():
            k = len(nodes)
            lo, hi = SPAN[ci]
            for i, n in enumerate(nodes):
                y = (lo + hi) / 2 if k == 1 else hi - (hi - lo) * i / (k - 1)
                pos[n] = (ci, y)
                d = G.nodes[n]
                if d["kind"] == "corridor":
                    tp = corr_inflow.get(n, 0.3)
                    color[n] = COLOR[n]
                elif d["kind"] == "refinery":
                    tp = d.get("capacity_mbd", 0.05)
                    color[n] = COLOR["refinery"]
                elif d["kind"] == "virtual":
                    tp = 1.2
                    color[n] = COLOR["virtual"]
                else:
                    tp = max(
                        sum(e["volume"] for *_x, e in G.in_edges(n, data=True)),
                        sum(e["volume"] for *_x, e in G.out_edges(n, data=True)),
                    )
                    color[n] = COLOR[d["kind"]]
                size[n] = 90 + min(tp, 2.6) ** 0.85 * 900

        fig, ax = plt.subplots(figsize=(opts["width"], opts["height"]))

        # edges
        for u, v, d in G.edges(data=True):
            c = d.get("corridor")
            nx.draw_networkx_edges(
                G, pos, edgelist=[(u, v)], ax=ax,
                width=max(0.4, d["volume"] ** 0.5 * 3.2),
                edge_color=COLOR.get(c, EDGE_GREY),
                alpha=0.75 if c else 0.2,
                arrows=False,
            )

        # nodes
        nx.draw_networkx_nodes(
            G, pos, ax=ax,
            node_size=[size[n] for n in G.nodes()],
            node_color=[color[n] for n in G.nodes()],
            edgecolors="white", linewidths=0.6,
        )

        # labels
        small = {n: n for n, d in G.nodes(data=True) if d["kind"] in ("refinery", "supplier")}
        big = {n: n for n, d in G.nodes(data=True) if d["kind"] not in ("refinery", "supplier")}
        nx.draw_networkx_labels(G, pos, labels=small, ax=ax, font_size=6.5,
                                horizontalalignment="left",
                                bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.65))
        nx.draw_networkx_labels(G, pos, labels=big, ax=ax, font_size=8, font_weight="bold")

        for i, lbl in enumerate(COL_LABEL):
            ax.text(i, 1.06, lbl.upper(), ha="center", va="bottom",
                    fontsize=8, color="#67797f", family="monospace")

        base, _ = nx.maximum_flow(G, SOURCE, SINK, capacity="volume")
        supply = sum(d["volume"] for _u, _v, d in G.out_edges(SOURCE, data=True))
        ax.set_title(
            f"India crude import network  ·  {G.number_of_nodes()} nodes / {G.number_of_edges()} edges  ·  "
            f"baseline max-flow {base:.3f} / {supply:.3f} mb/d",
            fontsize=10, color="#17242b", pad=18,
        )
        ax.set_xlim(-0.4, 5.4)
        ax.set_ylim(-0.08, 1.14)
        ax.axis("off")
        fig.tight_layout()

        out = Path(opts["out"])
        if not out.is_absolute():
            out = Path(settings.BASE_DIR) / out
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=opts["dpi"], bbox_inches="tight", facecolor="white")
        plt.close(fig)
        self.stdout.write(self.style.SUCCESS(f"wrote {out}"))

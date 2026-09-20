"""The Phase 4 deliverable: static vs. risk-weighted corridor criticality.

The thesis's core research contribution is the *delta* between two rankings:

  * **static**  — structure only. Cuts each corridor on the ``volume``
    attribute, which ``graph/updater.py`` never mutates, so this is the
    permanent structural baseline regardless of today's news.
  * **risk-weighted** — cuts each corridor on ``effective_capacity``, which
    carries the live risk score pushed on by ``graph/updater.py``.

``rank_shift = static_rank - risk_rank`` is the headline number: positive means
the corridor is more critical under current geopolitical conditions than its
structural baseline alone would suggest.

Ranking key is capacity-loss in mb/day, not centrality — it is directly
interpretable in a physical unit, whereas betweenness is a dimensionless
structural score. Both centrality measures are still reported per corridor as
complementary diagnostics, and ``rank_by="centrality"`` recomputes the ranks
off ``centrality`` for comparison in the thesis write-up.
"""
import logging

import networkx as nx

from graph.algorithms import (
    baseline_max_flow,
    capacity_weighted_betweenness,
    corridor_load_bearing_ports,
    degrade_corridor,
    residual_port_criticality,
    risk_weighted_max_flow,
    structural_betweenness,
)
from graph.builder import SINK, SOURCE, nodes_by_kind
from graph.state import GraphState

logger = logging.getLogger(__name__)

RANK_KEYS = ("capacity_loss", "centrality")


def _resolve_graph(G):
    if G is not None:
        return G
    graph = GraphState.get_instance().get_graph()
    if graph is None:
        raise ValueError(
            "no graph loaded — run `manage.py build_graph` first, or pass G explicitly"
        )
    return graph


def _warn_if_risk_never_reached_the_edges(G):
    """A graph straight out of ``build_graph`` has ``effective_capacity ==
    volume``, so the risk-weighted ranking comes out identical to the static
    one and ``rank_shift`` is all zeros — which reads as a legitimate "no
    shift" finding rather than as missing data. Corridor nodes carry
    ``live_risk_score`` from the DB even when the edges were never updated, so
    that mismatch is a reliable tell. Warn rather than raise: a genuinely
    risk-free graph is a valid thing to analyse (``--ignore-risk``)."""
    scored = [
        c for c in nodes_by_kind(G, "corridor")
        if G.nodes[c].get("live_risk_score", 0.0) > 0
    ]
    degraded = {
        d.get("corridor") for _u, _v, d in G.edges(data=True)
        if d.get("corridor") is not None and d["effective_capacity"] < d["volume"] - 1e-9
    }
    stale = sorted(set(scored) - degraded)
    if stale:
        logger.warning(
            "corridors %s carry a live_risk_score but their edges are still at full "
            "volume - risk-weighted ranking will duplicate the static one. Apply "
            "graph.updater.update_edge_weights (or run `manage.py score_risk`) first.",
            stale,
        )


def _assign_ranks(rows, key, rank_field):
    """Rank 1 = most critical (highest value of ``key``). Ties break on
    corridor name so ranks are deterministic across runs."""
    ordered = sorted(rows, key=lambda r: (-r[key], r["corridor"]))
    for position, row in enumerate(ordered, start=1):
        row[rank_field] = position


def compute_criticality(G=None, rank_by="capacity_loss"):
    """Return one row per corridor comparing static and risk-weighted
    criticality. Reads the graph only — every cut happens on a copy.

    Row shape (extends CLAUDE.md's documented ``/api/criticality/`` shape
    additively; every original key keeps its documented meaning)::

        {
          "corridor", "static_rank", "risk_rank", "rank_shift",
          "centrality",              # capacity-weighted betweenness
          "static_centrality",       # unweighted betweenness
          "capacity_loss_mbd",       # risk-weighted full-cut flow loss
          "static_capacity_loss_mbd",# static full-cut flow loss
          "live_risk_score",
          "residual_load_bearing_mbd",
        }
    """
    if rank_by not in RANK_KEYS:
        raise ValueError(f"rank_by must be one of {RANK_KEYS}, got {rank_by!r}")
    G = _resolve_graph(G)
    _warn_if_risk_never_reached_the_edges(G)

    # whole-graph computations — once each, never per corridor
    static_bc = structural_betweenness(G)
    risk_bc = capacity_weighted_betweenness(G)
    static_base_flow, _ = baseline_max_flow(G)
    risk_base_flow, _ = risk_weighted_max_flow(G)

    rows = []
    for corridor in nodes_by_kind(G, "corridor"):
        static_cut = degrade_corridor(G, corridor, 100, capacity_attr="volume")
        static_cut_flow, _ = nx.maximum_flow(static_cut, SOURCE, SINK, capacity="volume")

        risk_cut = degrade_corridor(G, corridor, 100, capacity_attr="effective_capacity")
        risk_cut_flow, _ = nx.maximum_flow(
            risk_cut, SOURCE, SINK, capacity="effective_capacity"
        )

        rows.append(
            {
                "corridor": corridor,
                "centrality": risk_bc[corridor],
                "static_centrality": static_bc[corridor],
                "capacity_loss_mbd": risk_base_flow - risk_cut_flow,
                "static_capacity_loss_mbd": static_base_flow - static_cut_flow,
                "live_risk_score": G.nodes[corridor].get("live_risk_score", 0.0),
                "residual_load_bearing_mbd": sum(
                    corridor_load_bearing_ports(G, corridor).values()
                ),
            }
        )

    if rank_by == "capacity_loss":
        _assign_ranks(rows, "static_capacity_loss_mbd", "static_rank")
        _assign_ranks(rows, "capacity_loss_mbd", "risk_rank")
    else:
        _assign_ranks(rows, "static_centrality", "static_rank")
        _assign_ranks(rows, "centrality", "risk_rank")

    for row in rows:
        row["rank_shift"] = row["static_rank"] - row["risk_rank"]

    rows.sort(key=lambda r: r["risk_rank"])
    logger.info(
        "criticality computed for %d corridors (rank_by=%s, static_flow=%.3f, risk_flow=%.3f)",
        len(rows), rank_by, static_base_flow, risk_base_flow,
    )
    return rows


def compute_port_criticality(G=None):
    """Per-port view of the redundancy-aware methodology, so the correction is
    inspectable at port granularity rather than only folded into a corridor
    number.

    ``stranded_mbd`` is the oversupply documented in edges.json's validation
    block: crude routed to a port that has no matching refinery offtake to
    place it. It is a port-level quantity (inflow minus downstream need), NOT
    the sum of per-corridor residuals — a port can be oversupplied overall
    while each individual corridor is still partly load-bearing.

    Row shape::

        {"port", "corridors": {corridor: residual_need_mbd},
         "total_naive_inflow_mbd", "downstream_need_mbd",
         "total_residual_need_mbd", "stranded_mbd"}
    """
    G = _resolve_graph(G)

    rows = []
    for port in nodes_by_kind(G, "port"):
        residual = residual_port_criticality(G, port)
        naive_inflow = sum(
            d.get("volume", 0.0)
            for _u, _v, d in G.in_edges(port, data=True)
            if d.get("corridor") is not None
        )
        downstream_need = sum(
            d.get("volume", 0.0) for _u, _v, d in G.out_edges(port, data=True)
        )
        rows.append(
            {
                "port": port,
                "corridors": residual,
                "total_naive_inflow_mbd": naive_inflow,
                "downstream_need_mbd": downstream_need,
                "total_residual_need_mbd": sum(residual.values()),
                "stranded_mbd": max(0.0, naive_inflow - downstream_need),
            }
        )

    rows.sort(key=lambda r: -r["stranded_mbd"])
    return rows

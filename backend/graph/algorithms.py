"""Generic NetworkX primitives for the criticality engine.

Pure functions only — no GraphState access, no DB access. Callers in
``criticality/`` are responsible for choosing which graph (live singleton vs.
a copy vs. a test fixture) to pass in.

Betweenness centrality note (methodology): NetworkX's ``weight=`` kwarg for
``betweenness_centrality`` is interpreted as edge *distance* for shortest-path
computation (lower = more traversable / more central), not edge importance.
CLAUDE.md's original spec, ``nx.betweenness_centrality(G, weight='effective_capacity')``,
would therefore invert the intended signal: a corridor becoming *safer* (higher
effective_capacity) would read as a *longer* path and register as *less*
central. ``capacity_weighted_betweenness`` below fixes this by distance-
transforming every edge as ``1 / capacity`` before calling betweenness, so a
corridor degraded toward zero effective capacity correctly becomes an
expensive/near-infinite-distance edge and its centrality drops.
"""
import networkx as nx

from graph.builder import SINK, SOURCE

EPS = 1e-9


def structural_betweenness(G):
    """Plain unweighted betweenness centrality — topological/structural
    criticality, independent of current risk or capacity. This is the
    measure ``tests/test_graph.py::test_betweenness_centrality_runs``
    asserts on; keep this signature stable."""
    return nx.betweenness_centrality(G)


def capacity_weighted_betweenness(G, capacity_attr="effective_capacity"):
    """Betweenness on a distance-transformed copy of ``G``: every edge gets
    ``distance = 1 / max(capacity_attr, EPS)``. Applied uniformly to every
    edge (not just corridor-tagged ones) so the transform is not cherry-picked
    for one layer of the graph."""
    H = G.copy()
    for _u, _v, d in H.edges(data=True):
        d["_distance"] = 1.0 / max(d.get(capacity_attr, EPS), EPS)
    return nx.betweenness_centrality(H, weight="_distance")


def baseline_max_flow(G):
    """SOURCE->SINK max-flow on the static ``volume`` ceiling."""
    return nx.maximum_flow(G, SOURCE, SINK, capacity="volume")


def risk_weighted_max_flow(G):
    """SOURCE->SINK max-flow on ``effective_capacity`` (today's live risk)."""
    return nx.maximum_flow(G, SOURCE, SINK, capacity="effective_capacity")


def degrade_corridor(G, corridor_name, pct, *, capacity_attr="effective_capacity"):
    """Return a NEW graph (``G`` is never mutated) with every edge tagged
    ``corridor == corridor_name`` scaled by ``(1 - pct/100)`` on
    ``capacity_attr``. Shared by ``criticality/cascade.py`` (10%-step loop)
    and ``criticality/scenarios.py`` (single-shot scenarios) so the two call
    sites cannot drift apart."""
    if not (0 <= pct <= 100):
        raise ValueError(f"pct must be in [0, 100], got {pct}")
    H = G.copy()
    factor = 1.0 - pct / 100.0
    for _u, _v, d in H.edges(data=True):
        if d.get("corridor") == corridor_name:
            d[capacity_attr] = d[capacity_attr] * factor
    return H


def degrade_supply(G, reduction_mbd, *, capacity_attr="effective_capacity"):
    """Return a NEW graph with every SOURCE->supplier edge's ``capacity_attr``
    scaled down proportionally so the total SOURCE outflow drops by
    ``reduction_mbd``. Models a supply-side shock (e.g. an OPEC+ production
    cut) as opposed to ``degrade_corridor``'s corridor/transit shock — the two
    are different physical mechanisms and CLAUDE.md's ``opec_cut`` scenario
    schema (``supply_reduction_mbd``, no ``corridor``) already anticipated
    this distinction."""
    if reduction_mbd < 0:
        raise ValueError(f"reduction_mbd must be >= 0, got {reduction_mbd}")
    H = G.copy()
    total_supply = sum(d.get(capacity_attr, 0.0) for _u, _v, d in H.out_edges(SOURCE, data=True))
    if total_supply <= EPS:
        return H
    factor = max(0.0, 1.0 - reduction_mbd / total_supply)
    for _u, _v, d in H.out_edges(SOURCE, data=True):
        d[capacity_attr] = d[capacity_attr] * factor
    return H


def residual_port_criticality(G, port_name, *, capacity_attr="effective_capacity"):
    """Per edges.json's own reconciliation note: for ``port_name``, compute
    for EACH corridor feeding it the *residual* downstream refinery need that
    would go unmet if that corridor alone were removed — rather than treating
    the full corridor->port edge value as always load-bearing. This avoids
    overstating criticality at oversupplied ports (more inbound corridor
    volume than matching refinery offtake, e.g. Chennai/Mumbai JNPT/Paradip/
    Sikka/Vizag) and understating it at single-corridor-fed ports (e.g.
    Kochi/Mangaluru), where the full edge value IS load-bearing.

    Returns ``{corridor_name: residual_need_mbd}``.

    ``downstream_need`` = sum of this port's outbound port->refinery edge
    volumes (the true consumption ceiling — NOT the port's inbound corridor
    volume, which may include crude the port has no refinery capacity to
    place). For each corridor ``c`` feeding the port, ``inflow_without_c`` =
    sum of the OTHER corridor->port edge volumes into this port;
    ``residual_need[c] = max(0, downstream_need - inflow_without_c)``.
    """
    downstream_need = sum(
        d.get("volume", 0.0) for _u, _v, d in G.out_edges(port_name, data=True)
    )
    inbound = [
        (d.get("corridor"), d.get("volume", 0.0))
        for _u, _v, d in G.in_edges(port_name, data=True)
        if d.get("corridor") is not None
    ]
    total_inbound = sum(vol for _c, vol in inbound)
    residual = {}
    for corridor, vol in inbound:
        inflow_without_c = total_inbound - vol
        residual[corridor] = max(0.0, downstream_need - inflow_without_c)
    return residual


def corridor_load_bearing_ports(G, corridor_name):
    """For ``corridor_name``, the residual need (see
    ``residual_port_criticality``) at every port it feeds:
    ``{port_name: residual_need_mbd}``."""
    result = {}
    for _u, port_name, d in G.out_edges(corridor_name, data=True):
        if d.get("corridor") != corridor_name:
            continue
        residual = residual_port_criticality(G, port_name)
        if corridor_name in residual:
            result[port_name] = residual[corridor_name]
    return result

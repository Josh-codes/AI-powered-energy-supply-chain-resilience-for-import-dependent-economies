"""Push corridor risk scores onto the graph's dynamic layer.

Only ``Corridor -> Port`` edges carry a ``corridor`` tag (see graph/builder.py),
so a corridor's risk is applied exactly once no matter how many suppliers feed
it or ports it serves:

    effective_capacity = volume × (1 - risk_score)

``volume`` is never touched — it stays the static baseline that max-flow
comparisons in the criticality engine measure the risk-weighted flow against.
"""
import logging

from graph.state import GraphState

logger = logging.getLogger(__name__)


def update_edge_weights(G, risk_scores):
    """Rescale every tagged corridor edge in-place and return the graph."""
    updated = 0
    for _u, _v, data in G.edges(data=True):
        corridor = data.get("corridor")
        if corridor not in risk_scores:
            continue
        # Clamped because a negative effective_capacity would make nx.maximum_flow
        # raise rather than simply model a fully closed corridor.
        risk = min(1.0, max(0.0, float(risk_scores[corridor])))
        data["effective_capacity"] = data["volume"] * (1.0 - risk)
        updated += 1

    for name, risk in risk_scores.items():
        if name in G.nodes:
            G.nodes[name]["live_risk_score"] = risk

    logger.info("updated %d corridor edges from %d risk scores", updated, len(risk_scores))
    return G


def refresh_graph_risk(risk_scores):
    """Apply risk scores to the shared singleton graph, building it first if the
    process has not loaded one yet. Returns the graph, or None if it could not
    be built."""
    state = GraphState.get_instance()
    if not state.is_loaded():
        from graph.builder import build_graph

        build_graph(persist=True)

    graph = state.get_graph()
    if graph is None:
        logger.error("no graph available to update")
        return None

    update_edge_weights(graph, risk_scores)
    return graph

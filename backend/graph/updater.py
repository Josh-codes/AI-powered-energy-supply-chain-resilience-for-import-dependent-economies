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


def stored_risk_scores():
    from core.models import Corridor

    return {c.name: c.live_risk_score for c in Corridor.objects.all()}


def _risk_matches(G, risk_scores, tol=1e-12):
    return all(
        name in G.nodes and abs(G.nodes[name].get("live_risk_score", 0.0) - risk) <= tol
        for name, risk in risk_scores.items()
    )


def load_live_graph():
    """Return the singleton graph with the DB's stored ``live_risk_score`` on
    its edges, building it first if this process has none.

    For long-lived readers (the API server, the orchestrator): ``score_risk``
    may have run in another process since the graph was built, and
    ``Corridor.updated_at`` does not move when it does (``score_risk`` saves
    with ``update_fields``), so staleness is detected by comparing risk values.
    A stale graph is corrected on a COPY that is then swapped in, so a request
    already reading the old graph never sees half-updated edges.
    """
    from graph.builder import build_graph

    state = GraphState.get_instance()
    scores = stored_risk_scores()
    G = state.get_graph()
    if G is None:
        G = build_graph(persist=False)
        update_edge_weights(G, scores)
        state.set_graph(G)
        return G
    if _risk_matches(G, scores):
        return G

    logger.info("stored corridor risk changed since the graph was loaded - refreshing")
    H = G.copy()
    update_edge_weights(H, scores)
    state.set_graph(H)
    return H

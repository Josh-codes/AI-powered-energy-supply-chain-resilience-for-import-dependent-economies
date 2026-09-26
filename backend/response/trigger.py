"""Threshold trigger: decides whether the response layer (reroute + SPR) runs.

Replaces CLAUDE.md's ``centrality > 0.65 AND risk_score > 0.50``, which could
never fire, for three measured reasons:
  1. corridor betweenness on this graph is ~0.02-0.08, an order of magnitude
     below 0.65, so the centrality clause is unreachable;
  2. post-Phase-4.5 risk scores (Hormuz 0.615, Red Sea 0.537) always pass 0.50,
     so the risk clause discriminates nothing;
  3. worse, ANY ``AND risk > X`` gate vetoes the corridor the engine ranks most
     critical: Cape loses the most flow (2.053 mb/d risk-weighted) precisely
     because it is intact, and it sits at risk 0.050.

As built — a corridor crosses when EITHER:
  * ``capacity_loss_mbd / risk_weighted_baseline_flow > LOSS_FRACTION_THRESHOLD``.
    A fraction, not an absolute mb/d, so it does not hardcode today's graph
    size the way 0.65 hardcoded today's graph shape. At the 0.15 default and a
    ~3.0 mb/d baseline that is ~0.46 mb/d — about a week of SPR drawdown at the
    1.0 mb/d physical limit. A POLICY threshold with a stated physical meaning,
    not a fitted constant.
  * ``live_risk_score > RISK_ALONE_THRESHOLD`` (OR, not AND). Risk is already
    inside ``effective_capacity``, so gating on it double-counts; as an
    independent clause at a high bar (0.75 ~ three severity-4 stories at full
    confidence today) it still lets an emerging crisis on a structurally minor
    corridor fire.

When several corridors cross, the one with the largest loss fraction is
``triggered_corridor`` — chosen by value, not by input order.
"""
import logging

from criticality.engine import compute_criticality
from graph.algorithms import risk_weighted_max_flow
from graph.state import GraphState

logger = logging.getLogger(__name__)

LOSS_FRACTION_THRESHOLD = 0.15  # of risk-weighted baseline max-flow
RISK_ALONE_THRESHOLD = 0.75     # normalized risk score, OR-clause

EPS = 1e-9


def check_threshold(
    criticality_rows, baseline_flow_mbd, *,
    loss_fraction=LOSS_FRACTION_THRESHOLD, risk_alone=RISK_ALONE_THRESHOLD,
):
    """Pure core: evaluate ``compute_criticality`` rows against the trigger.

    Returns::

        {"threshold_crossed", "triggered_corridor", "reason", "triggered",
         "evaluated": [{"corridor", "capacity_loss_mbd", "loss_fraction",
                        "live_risk_score", "loss_crossed", "risk_crossed",
                        "crossed"}, ...],
         "baseline_flow_mbd", "loss_fraction_threshold", "risk_alone_threshold"}

    ``threshold_crossed`` / ``triggered_corridor`` are the two PipelineState
    fields Phase 6's orchestrator reads.
    """
    if not (0 < loss_fraction <= 1):
        raise ValueError(f"loss_fraction must be in (0, 1], got {loss_fraction}")
    if not (0 < risk_alone <= 1):
        raise ValueError(f"risk_alone must be in (0, 1], got {risk_alone}")
    if baseline_flow_mbd <= EPS:
        logger.warning(
            "baseline flow is %.3f mb/d - loss fractions are undefined, only the "
            "risk clause can fire", baseline_flow_mbd,
        )

    evaluated = []
    for row in criticality_rows:
        loss = row["capacity_loss_mbd"]
        frac = loss / baseline_flow_mbd if baseline_flow_mbd > EPS else 0.0
        risk = row.get("live_risk_score", 0.0)
        loss_crossed = frac > loss_fraction
        risk_crossed = risk > risk_alone
        evaluated.append(
            {
                "corridor": row["corridor"],
                "capacity_loss_mbd": loss,
                "loss_fraction": frac,
                "live_risk_score": risk,
                "loss_crossed": loss_crossed,
                "risk_crossed": risk_crossed,
                "crossed": loss_crossed or risk_crossed,
            }
        )

    crossed = sorted(
        (e for e in evaluated if e["crossed"]),
        key=lambda e: (-e["loss_fraction"], -e["live_risk_score"], e["corridor"]),
    )
    top = crossed[0] if crossed else None
    if top is None:
        reason = "no corridor crossed either clause"
    else:
        parts = []
        if top["loss_crossed"]:
            parts.append(
                f"capacity loss {top['loss_fraction']:.1%} of baseline flow > {loss_fraction:.0%}"
            )
        if top["risk_crossed"]:
            parts.append(f"risk score {top['live_risk_score']:.3f} > {risk_alone:.2f}")
        reason = "; ".join(parts)

    return {
        "threshold_crossed": top is not None,
        "triggered_corridor": top["corridor"] if top else None,
        "reason": reason,
        "triggered": [e["corridor"] for e in crossed],
        "evaluated": evaluated,
        "baseline_flow_mbd": baseline_flow_mbd,
        "loss_fraction_threshold": loss_fraction,
        "risk_alone_threshold": risk_alone,
    }


def evaluate_graph(G=None, rank_by="capacity_loss", **thresholds):
    """Convenience wrapper: run ``compute_criticality`` and the risk-weighted
    baseline max-flow on ``G`` (default: the singleton, read-only), then
    ``check_threshold``. The result also carries the ``criticality`` rows so a
    caller does not have to compute them twice."""
    if G is None:
        G = GraphState.get_instance().get_graph()
        if G is None:
            raise ValueError(
                "no graph loaded — run `manage.py build_graph` first, or pass G explicitly"
            )
    rows = compute_criticality(G, rank_by=rank_by)
    baseline_flow, _ = risk_weighted_max_flow(G)
    result = check_threshold(rows, baseline_flow, **thresholds)
    result["criticality"] = rows
    return result

"""Cascading failure simulation: degrade one corridor in steps and watch
downstream refineries starve.

Baseline note — this module answers a DIFFERENT question from
``criticality/engine.py``. The engine compares a static structural world
against today's risk-weighted world. The cascade starts *from* today's
risk-weighted world (``effective_capacity``) and asks "if this corridor
degrades FURTHER, what happens". Do not conflate the two baselines when
reading their ``capacity_loss_mbd`` figures.
"""
import logging

import networkx as nx

from graph.algorithms import degrade_corridor
from graph.builder import SINK, SOURCE, nodes_by_kind
from graph.state import GraphState

logger = logging.getLogger(__name__)

# Not 1.0: nx.maximum_flow's numeric solver leaves float dust, which at 1.0
# would flag every refinery as "affected" by epsilon amounts at every step.
# This is a numerical-stability choice, not a modelling one.
AFFECTED_THRESHOLD_PCT = 0.95


def _refinery_inflow(G, flow_dict):
    """Realized crude reaching each refinery, read off the refinery->SINK
    flow (equivalently its total inbound port flow)."""
    return {
        r: flow_dict.get(r, {}).get(SINK, 0.0) for r in nodes_by_kind(G, "refinery")
    }


def check_refineries(
    G, flow_dict, baseline_flow_dict, *,
    threshold_pct=AFFECTED_THRESHOLD_PCT, min_run_rate_aware=False,
):
    """Refineries whose realized inflow has dropped below ``threshold_pct`` of
    their OWN baseline realized inflow.

    Compared against each refinery's own baseline rather than its nameplate
    ``capacity_mbd`` because several refineries never run at nameplate even at
    baseline (grade incompatibility and the oversupply routing documented in
    edges.json already limit them) — measuring against nameplate would flag
    those permanently, drowning the real signal.

    ``min_run_rate_aware`` is a documented extension point, not yet built:
    modelling a refinery going fully offline below its ``min_run_rate`` is a
    discrete on/off behaviour that needs an iterative re-solve (shut a
    refinery, re-run max-flow, re-check), which is a different mathematical
    object from this continuous max-flow model. Deferred deliberately.
    """
    if min_run_rate_aware:
        raise NotImplementedError(
            "min_run_rate-aware discrete refinery shutdown is deferred to a "
            "later phase — see criticality/cascade.py"
        )

    current = _refinery_inflow(G, flow_dict)
    baseline = _refinery_inflow(G, baseline_flow_dict)

    affected = []
    for refinery, baseline_mbd in baseline.items():
        if baseline_mbd <= 0:
            continue
        current_mbd = current.get(refinery, 0.0)
        if current_mbd < baseline_mbd * threshold_pct:
            affected.append(
                {
                    "refinery": refinery,
                    "baseline_mbd": baseline_mbd,
                    "current_mbd": current_mbd,
                    "pct_of_baseline": current_mbd / baseline_mbd,
                    "shortfall_mbd": baseline_mbd - current_mbd,
                }
            )

    affected.sort(key=lambda a: -a["shortfall_mbd"])
    return affected


def cascading_failure_simulation(corridor_name, *, G=None, step_pct=10):
    """Degrade ``corridor_name`` from ``step_pct``% to 100% in ``step_pct``
    increments, returning one row per step.

    When ``G`` is None the graph is taken from the singleton via
    ``get_graph_copy()`` so a simulation can never mutate the shared instance.

    Row shape::

        {"degradation_pct", "flow_after_mbd", "capacity_loss_mbd",
         "affected_refineries", "affected_count"}
    """
    if G is None:
        G = GraphState.get_instance().get_graph_copy()
        if G is None:
            raise ValueError(
                "no graph loaded — run `manage.py build_graph` first, or pass G explicitly"
            )
    if corridor_name not in G:
        raise ValueError(f"unknown corridor {corridor_name!r}")

    baseline_flow, baseline_flow_dict = nx.maximum_flow(
        G, SOURCE, SINK, capacity="effective_capacity"
    )

    results = []
    for pct in range(step_pct, 101, step_pct):
        H = degrade_corridor(G, corridor_name, pct)
        disrupted_flow, flow_dict = nx.maximum_flow(
            H, SOURCE, SINK, capacity="effective_capacity"
        )
        affected = check_refineries(H, flow_dict, baseline_flow_dict)
        results.append(
            {
                "degradation_pct": pct,
                "flow_after_mbd": disrupted_flow,
                "capacity_loss_mbd": baseline_flow - disrupted_flow,
                "affected_refineries": affected,
                "affected_count": len(affected),
            }
        )

    logger.info(
        "cascade for %s: %d steps, final loss %.3f mb/d",
        corridor_name, len(results), results[-1]["capacity_loss_mbd"] if results else 0.0,
    )
    return results

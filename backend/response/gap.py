"""Supply-gap estimation: how much crude (mb/d) a disruption removes, and
where it lands. The quantity every other response module consumes.

Baseline — stated loudly because the criticality layer already has two:
this module uses the CASCADE baseline, i.e. today's risk-weighted world
(``effective_capacity``), and asks how much ADDITIONAL flow a disruption
removes from it. That is the question a response answers: how much crude must
be replaced starting from now. It is NOT the engine's static-vs-risk delta, and
``gap_mbd`` here is comparable with ``criticality/cascade.py``'s
``capacity_loss_mbd`` but not with ``criticality/engine.py``'s
``static_capacity_loss_mbd``.

``gap_mbd`` vs. the per-refinery shortfalls: ``check_refineries`` only lists a
refinery once it has lost more than 5% of its own baseline
(``AFFECTED_THRESHOLD_PCT``), so the listed shortfalls generally sum to LESS
than ``gap_mbd``. The residual is reported as ``deadband_unattributed_mbd``
rather than left for a reader to conflate the two numbers. Size responses off
``gap_mbd``; use ``affected_refineries`` for targeting.
"""
import logging

import networkx as nx

from criticality.cascade import check_refineries
from criticality.scenarios import SCENARIOS, run_scenario
from graph.algorithms import corridor_load_bearing_ports, degrade_corridor
from graph.builder import SINK, SOURCE
from graph.state import GraphState

logger = logging.getLogger(__name__)

BASELINE = "risk_weighted"


def _resolve_graph_copy(G):
    if G is not None:
        return G
    graph = GraphState.get_instance().get_graph_copy()
    if graph is None:
        raise ValueError(
            "no graph loaded — run `manage.py build_graph` first, or pass G explicitly"
        )
    return graph


def _package(*, corridor, mechanism, scenario, degradation_pct, baseline_flow,
             flow_after, affected, load_bearing, duration_days):
    gap = max(0.0, baseline_flow - flow_after)
    shortfall = sum(a["shortfall_mbd"] for a in affected)
    return {
        "corridor": corridor,
        "mechanism": mechanism,
        "scenario": scenario,
        "degradation_pct": degradation_pct,
        "baseline": BASELINE,
        "baseline_flow_mbd": baseline_flow,
        "flow_after_mbd": flow_after,
        "gap_mbd": gap,
        "affected_refineries": affected,
        "affected_count": len(affected),
        "refinery_shortfall_mbd": shortfall,
        "deadband_unattributed_mbd": max(0.0, gap - shortfall),
        "load_bearing_ports": load_bearing,
        "duration_days": duration_days,
        "total_shortfall_mb": gap * duration_days if duration_days else None,
    }


def estimate_supply_gap(corridor_name, degradation_pct=100, *, G=None, duration_days=None):
    """Gap from degrading one corridor by ``degradation_pct`` (0-100) on top of
    today's risk. A single-point solve (two max-flows) — use
    ``cascading_failure_simulation`` for the full curve, not this in a loop.

    ``G=None`` takes a COPY of the singleton, so this never mutates shared state.
    """
    G = _resolve_graph_copy(G)
    if G.nodes.get(corridor_name, {}).get("kind") != "corridor":
        # degrade_corridor silently no-ops on an unknown name, which would
        # report a typo (or "Suez", folded into Red Sea) as a zero gap.
        raise ValueError(f"unknown corridor {corridor_name!r}")
    if duration_days is not None and duration_days <= 0:
        raise ValueError(f"duration_days must be > 0, got {duration_days}")

    baseline_flow, baseline_dict = nx.maximum_flow(G, SOURCE, SINK, capacity="effective_capacity")
    H = degrade_corridor(G, corridor_name, degradation_pct)
    flow_after, flow_dict = nx.maximum_flow(H, SOURCE, SINK, capacity="effective_capacity")
    affected = check_refineries(H, flow_dict, baseline_dict)

    result = _package(
        corridor=corridor_name, mechanism="corridor", scenario=None,
        degradation_pct=degradation_pct, baseline_flow=baseline_flow,
        flow_after=flow_after, affected=affected,
        load_bearing=corridor_load_bearing_ports(G, corridor_name),
        duration_days=duration_days,
    )
    logger.info(
        "supply gap for %s at %s%%: %.3f mb/d", corridor_name, degradation_pct, result["gap_mbd"],
    )
    return result


def gap_from_scenario(scenario_key, *, G=None, duration_days=None):
    """Gap for a named scenario, in the same shape as ``estimate_supply_gap``.
    Delegates to ``run_scenario`` so both corridor shocks and the supply-side
    ``opec_cut`` go through the mechanism dispatch Phase 4 already tested;
    ``corridor`` and ``load_bearing_ports`` are None / {} for a supply shock."""
    if scenario_key not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario_key!r} — known: {sorted(SCENARIOS)}")
    if duration_days is not None and duration_days <= 0:
        raise ValueError(f"duration_days must be > 0, got {duration_days}")
    G = _resolve_graph_copy(G)
    r = run_scenario(scenario_key, G=G)
    corridor = r.get("corridor")
    return _package(
        corridor=corridor, mechanism=r["mechanism"], scenario=scenario_key,
        degradation_pct=r.get("degradation_pct"), baseline_flow=r["baseline_flow_mbd"],
        flow_after=r["flow_after_mbd"], affected=r["affected_refineries"],
        load_bearing=corridor_load_bearing_ports(G, corridor) if corridor else {},
        duration_days=duration_days,
    )

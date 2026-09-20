"""Named disruption scenarios with IEA-cited parameters.

Two distinct physical mechanisms live here, and they are NOT interchangeable:

  * **corridor shocks** (``hormuz_30``, ``hormuz_full``, ``red_sea``) throttle
    a chokepoint's corridor->port edges — ``graph.algorithms.degrade_corridor``.
  * **supply shocks** (``opec_cut``) cut production upstream of any corridor,
    hitting the SOURCE->supplier edges — ``graph.algorithms.degrade_supply``.

CLAUDE.md's original SCENARIOS dict already encoded this split implicitly by
giving ``opec_cut`` a different key schema (``supply_reduction_mbd``, and
``corridor: None``); this module makes the two mechanisms explicit.

The ``price_impact_usd`` / ``affected_volume_mbd`` figures are cited external
estimates carried as metadata for the dashboard — they are NOT computed by this
model and must not be presented as model output.
"""
import logging

import networkx as nx

from graph.algorithms import degrade_corridor, degrade_supply
from graph.builder import SINK, SOURCE
from graph.state import GraphState

logger = logging.getLogger(__name__)

SCENARIOS = {
    "hormuz_30": {
        "name": "Hormuz 30% Disruption",
        "mechanism": "corridor",
        "corridor": "Hormuz",
        "degradation_pct": 30,
        "price_impact_usd": 20,
        "affected_volume_mbd": 5.1,
        "source": "IEA Chokepoints 2023",
    },
    "hormuz_full": {
        "name": "Hormuz Full Closure",
        "mechanism": "corridor",
        "corridor": "Hormuz",
        "degradation_pct": 100,
        "price_impact_usd": 65,
        "affected_volume_mbd": 17.0,
        "source": "IEA Chokepoints 2023",
    },
    "red_sea": {
        "name": "Red Sea Suspension",
        "mechanism": "corridor",
        "corridor": "Red Sea",
        "degradation_pct": 80,
        "price_impact_usd": 5,
        "transit_day_increase": 14,
        "source": "IEA Red Sea Assessment 2024",
    },
    "opec_cut": {
        "name": "OPEC+ Emergency Cut",
        "mechanism": "supply",
        "corridor": None,
        "supply_reduction_mbd": 2.0,
        "india_impact_mbd": 0.8,
        "price_impact_usd": 15,
        "source": "IEA Oil Market Report 2024",
    },
}


def run_scenario(scenario_key, *, G=None):
    """Apply one named scenario and return its computed impact merged with the
    scenario's cited metadata.

    A single-shot solve at the scenario's defined severity — for the full
    10%-step curve use ``criticality.cascade.cascading_failure_simulation``.

    Returns the scenario dict plus ``baseline_flow_mbd``, ``flow_after_mbd``,
    ``capacity_loss_mbd``, ``affected_refineries``, ``affected_count``.
    """
    if scenario_key not in SCENARIOS:
        raise ValueError(
            f"unknown scenario {scenario_key!r} — known: {sorted(SCENARIOS)}"
        )
    scenario = SCENARIOS[scenario_key]

    if G is None:
        G = GraphState.get_instance().get_graph_copy()
        if G is None:
            raise ValueError(
                "no graph loaded — run `manage.py build_graph` first, or pass G explicitly"
            )

    baseline_flow, baseline_flow_dict = nx.maximum_flow(
        G, SOURCE, SINK, capacity="effective_capacity"
    )

    if scenario["mechanism"] == "corridor":
        H = degrade_corridor(G, scenario["corridor"], scenario["degradation_pct"])
    elif scenario["mechanism"] == "supply":
        H = degrade_supply(G, scenario["supply_reduction_mbd"])
    else:
        raise ValueError(f"unknown mechanism {scenario['mechanism']!r} in {scenario_key!r}")

    flow_after, flow_dict = nx.maximum_flow(H, SOURCE, SINK, capacity="effective_capacity")

    # imported here to avoid a circular import at module load (cascade imports
    # graph.algorithms, which this module also uses)
    from criticality.cascade import check_refineries

    affected = check_refineries(H, flow_dict, baseline_flow_dict)

    logger.info(
        "scenario %s: flow %.3f -> %.3f (loss %.3f mb/d), %d refineries affected",
        scenario_key, baseline_flow, flow_after, baseline_flow - flow_after, len(affected),
    )

    return {
        **scenario,
        "key": scenario_key,
        "baseline_flow_mbd": baseline_flow,
        "flow_after_mbd": flow_after,
        "capacity_loss_mbd": baseline_flow - flow_after,
        "affected_refineries": affected,
        "affected_count": len(affected),
    }

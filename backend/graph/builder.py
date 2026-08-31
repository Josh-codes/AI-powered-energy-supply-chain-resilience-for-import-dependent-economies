"""Build the crude-oil import DiGraph from the database (nodes) plus
data/edges.json (edges).

Topology (max-flow ready):

    SOURCE -> Supplier -> Corridor -> Port -> Refinery -> SINK

Every edge carries three numeric attributes:
  * ``volume``             — nominal mb/day for this link (static)
  * ``effective_capacity`` — risk-adjusted capacity; equals ``volume`` at build
                             time, later scaled by graph/updater.py
  * ``capacity``           — alias of ``volume`` so ``nx.maximum_flow`` works
                             with ``capacity='capacity'`` too

Only ``Corridor -> Port`` edges carry a ``corridor`` tag; that is the single
layer graph/updater.py degrades when a corridor's risk score changes, so a
corridor disruption is modelled exactly once.

The ``Refinery -> SINK`` edge is capped at the refinery's nameplate
``capacity_mbd``; this is what protects the model from upstream over-allocation
(e.g. a refinery fed from two ports).
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
from django.conf import settings

from core.models import Corridor, Port, Refinery, Supplier
from graph.state import GraphState

logger = logging.getLogger(__name__)

SOURCE = "SOURCE"
SINK = "SINK"
DATA_DIR = Path(settings.BASE_DIR) / "data"


def _load_edges():
    with (DATA_DIR / "edges.json").open(encoding="utf-8") as fh:
        return json.load(fh)


def _grade_compatible(refinery, crudes):
    """True if the refinery's API/sulfur window admits at least one of the
    crudes that can physically reach it. ``crudes`` is an iterable of
    ``(api_gravity, sulfur_pct)`` tuples."""
    for api, sulfur in crudes:
        if api is None or sulfur is None:
            continue
        if (
            refinery.api_gravity_min <= api <= refinery.api_gravity_max
            and sulfur <= refinery.sulfur_tolerance
        ):
            return True
    # No known crude reaches it, or none fits — leave routing decisions to the
    # criticality layer; default open so it is not spuriously "cut off".
    return True if not list(crudes) else False


def build_graph(persist=True):
    """Construct and return the import DiGraph. When ``persist`` is True the
    graph is stored in the process-wide :class:`GraphState` singleton."""
    edges = _load_edges()

    suppliers = {s.name: s for s in Supplier.objects.all()}
    corridors = {c.name: c for c in Corridor.objects.all()}
    ports = {p.name: p for p in Port.objects.all()}
    refineries = {r.name: r for r in Refinery.objects.all()}

    # ---- validate names up front (fail loud, per project rules) -------------
    missing = set()
    for e in edges["supplier_to_corridor"]:
        if e["supplier"] not in suppliers:
            missing.add(f"supplier {e['supplier']!r}")
        if e["corridor"] not in corridors:
            missing.add(f"corridor {e['corridor']!r}")
    for e in edges["corridor_to_port"]:
        if e["corridor"] not in corridors:
            missing.add(f"corridor {e['corridor']!r}")
        if e["port"] not in ports:
            missing.add(f"port {e['port']!r}")
    for e in edges["port_to_refinery"]:
        if e["port"] not in ports:
            missing.add(f"port {e['port']!r}")
        if e["refinery"] not in refineries:
            missing.add(f"refinery {e['refinery']!r}")
    if missing:
        raise ValueError(
            "edges.json references names not present in the database: "
            + ", ".join(sorted(missing))
            + " — run `manage.py seed_db` or fix data/edges.json"
        )

    G = nx.DiGraph()
    G.add_node(SOURCE, kind="virtual")
    G.add_node(SINK, kind="virtual")

    for name, s in suppliers.items():
        G.add_node(
            name, kind="supplier", region=s.region, country_code=s.country_code,
            sanctioned=s.sanctioned, avg_export_mbd=s.avg_export_mbd,
        )
    for name, c in corridors.items():
        G.add_node(
            name, kind="corridor", capacity_mbd=c.capacity_mbd,
            transit_days=c.transit_days, baseline_risk=c.baseline_risk,
            live_risk_score=c.live_risk_score,
        )
    for name, p in ports.items():
        G.add_node(name, kind="port", state=p.state, throughput_mbd=p.throughput_mbd)
    for name, r in refineries.items():
        G.add_node(
            name, kind="refinery", company=r.company, capacity_mbd=r.capacity_mbd,
            min_run_rate=r.min_run_rate, api_gravity_min=r.api_gravity_min,
            api_gravity_max=r.api_gravity_max, sulfur_tolerance=r.sulfur_tolerance,
        )

    def add_edge(u, v, volume, *, corridor=None, **extra):
        vol = float(volume)
        G.add_edge(
            u, v, volume=vol, effective_capacity=vol, capacity=vol,
            corridor=corridor, **extra,
        )

    # SOURCE -> supplier : cap = the supplier's total modelled export
    supplier_out = {}
    for e in edges["supplier_to_corridor"]:
        supplier_out[e["supplier"]] = supplier_out.get(e["supplier"], 0.0) + e["volume_mbd"]
    for name, vol in supplier_out.items():
        add_edge(SOURCE, name, vol)

    # supplier -> corridor : attrs volume, crude_grade (static)
    for e in edges["supplier_to_corridor"]:
        add_edge(
            e["supplier"], e["corridor"], e["volume_mbd"],
            crude_grade=e.get("crude_grade"), api_gravity=e.get("api_gravity"),
            sulfur_pct=e.get("sulfur_pct"),
        )

    # corridor -> port : the DYNAMIC layer — tagged with `corridor`
    for e in edges["corridor_to_port"]:
        add_edge(
            e["corridor"], e["port"], e["volume_mbd"], corridor=e["corridor"],
            transit_days=e.get("transit_days"),
        )

    # which crudes can reach each port (supplier -> corridor -> port)
    corr_crudes = {}
    for e in edges["supplier_to_corridor"]:
        corr_crudes.setdefault(e["corridor"], []).append(
            (e.get("api_gravity"), e.get("sulfur_pct"))
        )
    port_crudes = {}
    for e in edges["corridor_to_port"]:
        port_crudes.setdefault(e["port"], []).extend(corr_crudes.get(e["corridor"], []))

    # port -> refinery : attrs volume, grade_compatible
    for e in edges["port_to_refinery"]:
        r = refineries[e["refinery"]]
        add_edge(
            e["port"], e["refinery"], e["volume_mbd"],
            grade_compatible=_grade_compatible(r, port_crudes.get(e["port"], [])),
        )

    # refinery -> SINK : cap = nameplate capacity (the over-allocation guard)
    for name, r in refineries.items():
        if G.in_degree(name) == 0:
            logger.warning("refinery %r has no inbound edge — it will get no crude", name)
        add_edge(name, SINK, r.capacity_mbd)

    # ---- structural sanity -------------------------------------------------
    for name in corridors:
        if G.in_degree(name) == 0 or G.out_degree(name) == 0:
            logger.warning(
                "corridor %r is not fully connected (in=%d out=%d) — it will carry no flow",
                name, G.in_degree(name), G.out_degree(name),
            )
    if not nx.has_path(G, SOURCE, SINK):
        raise ValueError("graph has no SOURCE->SINK path; check edges.json")

    logger.info(
        "graph built: %d nodes / %d edges (%d suppliers, %d corridors, %d ports, %d refineries)",
        G.number_of_nodes(), G.number_of_edges(),
        len(suppliers), len(corridors), len(ports), len(refineries),
    )

    if persist:
        GraphState.get_instance().set_graph(G, built_at=datetime.now(timezone.utc))
    return G


# alias matching CLAUDE.md's File Responsibility Map
build_graph_from_db = build_graph


def nodes_by_kind(G, kind):
    return [n for n, d in G.nodes(data=True) if d.get("kind") == kind]

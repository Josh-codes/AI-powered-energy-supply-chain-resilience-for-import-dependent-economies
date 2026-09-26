"""The LangGraph orchestrator: CLAUDE.md's node order, one conditional edge.

    ingest -> extract -> score -> update_graph -> criticality -> check_threshold
        |-- [threshold_crossed]     -> response -> dashboard_update
        '-- [not threshold_crossed] ------------> dashboard_update

Synchronous, in-process, no checkpointer, no broker: the thesis build runs the
pipeline by hand (``manage.py run_pipeline``), never on a timer.

The three DATA stages are opt-in, and the defaults are chosen so that a bare
run is free and leaves the thesis figures where they are:

  * ``ingest``  - network, and GKG is ~280 MB per 24h;
  * ``extract`` - the only PAID stage;
  * ``score``   - rewrites ``Corridor.live_risk_score``, which moves every
    Thesis Snapshot figure (scores decay daily even without new events).

With all three off, the run is analysis only, on the stored scores:
update_graph -> criticality -> threshold -> response -> persist.
"""
import logging
from datetime import datetime, timezone

from langgraph.graph import END, START, StateGraph

from orchestrator import nodes
from orchestrator.state import DEFAULT_OPTIONS, PipelineState
from response.reroute import CRISIS_WEIGHTS

logger = logging.getLogger(__name__)


# What `--full` means: the whole production cycle.
FULL_CYCLE = {"ingest": ["rss", "gdelt-fallback"], "extract": True, "score": True}

_compiled = None


def build_pipeline():
    """Return the compiled StateGraph (built once per process)."""
    global _compiled
    if _compiled is not None:
        return _compiled
    g = StateGraph(PipelineState)
    order = ["ingest", "extract", "score", "update_graph", "criticality", "check_threshold"]
    for name in order + ["response", "dashboard_update"]:
        g.add_node(name, getattr(nodes, name))
    g.add_edge(START, order[0])
    for a, b in zip(order, order[1:]):
        g.add_edge(a, b)
    g.add_conditional_edges(
        "check_threshold", nodes.route_after_threshold,
        {"response": "response", "dashboard_update": "dashboard_update"},
    )
    g.add_edge("response", "dashboard_update")
    g.add_edge("dashboard_update", END)
    _compiled = g.compile()
    return _compiled


def resolve_options(**overrides):
    """Merge overrides onto DEFAULT_OPTIONS and validate them up front, so a
    typo fails before any stage runs instead of surfacing as a stage error."""
    unknown = set(overrides) - set(DEFAULT_OPTIONS)
    if unknown:
        raise ValueError(f"unknown pipeline options {sorted(unknown)}")
    opts = {**DEFAULT_OPTIONS, **overrides}
    opts["ingest"] = list(opts["ingest"] or [])
    bad = [s for s in opts["ingest"] if s not in nodes.INGEST_SOURCES]
    if bad:
        raise ValueError(f"unknown ingest sources {bad} - known: {list(nodes.INGEST_SOURCES)}")
    if opts["crisis"] not in CRISIS_WEIGHTS:
        raise ValueError(f"crisis must be one of {sorted(CRISIS_WEIGHTS)}, got {opts['crisis']!r}")
    if not isinstance(opts["duration_days"], int) or opts["duration_days"] <= 0:
        raise ValueError(f"duration_days must be a positive int, got {opts['duration_days']!r}")
    return opts


def run_pipeline(**overrides):
    """Run the orchestrator once and return the final PipelineState."""
    opts = resolve_options(**overrides)
    initial = {
        "pipeline_run_at": datetime.now(timezone.utc).isoformat(),
        "stages_run": [],
        "errors": [],
    }
    logger.info("pipeline run starting with %s", opts)
    try:
        final = build_pipeline().invoke(initial, config={"configurable": opts})
    except Exception as exc:
        # Nodes never raise, so reaching here means the orchestrator itself
        # broke. Leave a FAILED row rather than no trace of the run.
        logger.exception("pipeline orchestrator raised")
        if opts["persist"]:
            from core.models import PipelineRun

            PipelineRun.objects.create(
                started_at=datetime.fromisoformat(initial["pipeline_run_at"]),
                finished_at=datetime.now(timezone.utc),
                status=PipelineRun.STATUS_FAILED, options=opts,
                errors=[{"stage": "orchestrator", "error": f"{type(exc).__name__}: {exc}"}],
            )
        raise
    logger.info(
        "pipeline run finished: stages=%s errors=%d triggered=%s run_id=%s",
        final.get("stages_run"), len(final.get("errors", [])),
        final.get("triggered_corridor"), final.get("run_id"),
    )
    return final

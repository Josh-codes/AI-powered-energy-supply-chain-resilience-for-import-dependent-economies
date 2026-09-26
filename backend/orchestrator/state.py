"""PipelineState: the dict LangGraph threads through the orchestrator's nodes.

CLAUDE.md's spec schema, with documented deviations:

  * ``raw_articles`` / ``extracted_events`` are replaced by ``ingest_report`` /
    ``extraction_report``. Every stage persists its output to PostgreSQL
    (RawArticle, ExtractedEvent), and the downstream stages read from there,
    so carrying hundreds of article dicts through the state bought nothing.
    The reports keep what matters for a run's audit: per-corridor
    fetched/stored/sampled and the extraction counts.
  * Additions: ``threshold`` (the full ``check_threshold`` result, not only its
    two headline fields), ``baseline_flow_mbd``, ``response`` (gap + timeline,
    which the reroute list and SPR schedule alone do not explain),
    ``stages_run``, ``run_id`` and ``errors``.

``errors`` and ``stages_run`` use an ``operator.add`` reducer so each node
appends instead of overwriting.
"""
import operator
from typing import Annotated, Dict, List, Optional, TypedDict


#: Run options, passed to every node as ``config["configurable"]``. Not part
#: of the state: they configure the run rather than record its results.
DEFAULT_OPTIONS = {
    "ingest": [],             # subset of nodes.INGEST_SOURCES
    "last_minutes": None,     # ingest window; None = each source's default (24h)
    "max_records": None,      # GDELT DOC articles per corridor
    "extract": False,         # PAID
    "extract_limit": None,
    "score": False,           # moves the Thesis Snapshot
    "crisis": "normal",
    "duration_days": 14,
    "include_sanctioned": False,
    "persist": True,
}


class PipelineState(TypedDict, total=False):
    pipeline_run_at: str

    ingest_report: Optional[dict]
    extraction_report: Optional[dict]
    risk_scores: Dict[str, float]
    graph_updated: bool

    criticality_ranking: List[dict]
    baseline_flow_mbd: float
    capacity_loss_mbd: float

    threshold: dict
    threshold_crossed: bool
    triggered_corridor: Optional[str]

    reroute_recommendations: List[dict]
    spr_schedule: Optional[dict]
    response: Optional[dict]

    run_id: Optional[int]
    stages_run: Annotated[List[str], operator.add]
    errors: Annotated[List[dict], operator.add]

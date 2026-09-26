"""Orchestrator node functions: thin wrappers over the other apps.

Each node takes ``(state, config)``, reads its run options from
``config["configurable"]`` (see ``orchestrator.pipeline.DEFAULT_OPTIONS``) and
returns only the state keys it sets. A node that is switched off returns
``{}`` without appearing in ``stages_run``.

Never raises: per CLAUDE.md the pipeline must never crash, so every node
catches its own failure, appends ``{"stage", "error"}`` to ``errors`` and lets
the run continue. A downstream node whose input is missing records that as its
own error rather than computing on nothing. The run is then persisted as
``partial``, which is honest where a crash would lose the stages that worked.

The graph is not carried in the state: each graph-reading node calls
``load_live_graph()``, which returns the same singleton unless the stored risk
changed, so every node in one run reads the same world.
"""
import logging
from datetime import datetime, timezone

from core.models import PipelineRun
from criticality.engine import compute_criticality
from graph.algorithms import risk_weighted_max_flow
from graph.updater import stored_risk_scores, load_live_graph
from orchestrator.state import DEFAULT_OPTIONS
from pipeline import tasks
from response.plan import build_response
from response.trigger import check_threshold as _check_threshold

logger = logging.getLogger(__name__)

INGEST_SOURCES = ("rss", "gkg", "gdelt", "gdelt-fallback")


def _opts(config):
    """This run's options only. LangGraph also puts its own runtime objects in
    ``configurable``, which are neither ours nor JSON-serializable."""
    configurable = (config or {}).get("configurable", {})
    return {k: configurable[k] for k in DEFAULT_OPTIONS if k in configurable}


def _failed(stage, exc):
    logger.exception("pipeline stage %s failed", stage)
    return {"errors": [{"stage": stage, "error": f"{type(exc).__name__}: {exc}"}]}


def _missing(stage, what):
    logger.error("pipeline stage %s skipped: %s", stage, what)
    return {"errors": [{"stage": stage, "error": f"skipped: {what}"}]}


# ---- data stages (opt-in) ----------------------------------------------------

def _poll(source, opts):
    if source == "rss":
        return {"stored": tasks.poll_rss()}
    if source == "gkg":
        return tasks.poll_gdelt_gkg(last_minutes=opts.get("last_minutes"))
    if source == "gdelt":
        return tasks.poll_gdelt_by_corridor(
            max_records=opts.get("max_records"), last_minutes=opts.get("last_minutes"),
        )
    if source == "gdelt-fallback":
        return tasks.poll_gdelt_with_fallback(
            max_records=opts.get("max_records"), last_minutes=opts.get("last_minutes"),
        )
    raise ValueError(f"unknown ingest source {source!r} - known: {INGEST_SOURCES}")


def ingest(state, config=None):
    sources = _opts(config).get("ingest") or []
    if not sources:
        return {}
    report, errors = {}, []
    for source in sources:
        # Per source, so one dead feed does not cost the others.
        try:
            report[source] = _poll(source, _opts(config))
        except Exception as exc:
            errors += _failed("ingest", exc)["errors"]
            report[source] = None
    return {"ingest_report": report, "stages_run": ["ingest"], "errors": errors}


def extract(state, config=None):
    opts = _opts(config)
    if not opts.get("extract"):
        return {}
    try:
        report = tasks.extract_events(limit=opts.get("extract_limit"))
    except Exception as exc:
        return {**_failed("extract", exc), "stages_run": ["extract"]}
    return {"extraction_report": report, "stages_run": ["extract"]}


def score(state, config=None):
    if not _opts(config).get("score"):
        return {}
    try:
        scores = tasks.score_and_update_graph()
    except Exception as exc:
        return {**_failed("score", exc), "stages_run": ["score"]}
    if not scores:
        return {**_missing("score", "scoring returned no corridor scores"), "stages_run": ["score"]}
    return {"risk_scores": scores, "stages_run": ["score"]}


# ---- analysis stages (always run) ---------------------------------------------

def update_graph(state, config=None):
    try:
        load_live_graph()
        # Whatever the score stage wrote is now the stored risk, so reading it
        # back covers both the scored and the snapshot-only run.
        scores = stored_risk_scores()
    except Exception as exc:
        return {**_failed("update_graph", exc), "graph_updated": False, "stages_run": ["update_graph"]}
    return {"risk_scores": scores, "graph_updated": True, "stages_run": ["update_graph"]}


def criticality(state, config=None):
    if not state.get("graph_updated"):
        return {**_missing("criticality", "no graph"), "stages_run": ["criticality"]}
    try:
        G = load_live_graph()
        rows = compute_criticality(G)
        baseline, _ = risk_weighted_max_flow(G)
    except Exception as exc:
        return {**_failed("criticality", exc), "stages_run": ["criticality"]}
    return {
        "criticality_ranking": rows,
        "baseline_flow_mbd": baseline,
        # rows are sorted by risk_rank, so this is the most consequential cut
        "capacity_loss_mbd": rows[0]["capacity_loss_mbd"] if rows else 0.0,
        "stages_run": ["criticality"],
    }


def check_threshold(state, config=None):
    rows = state.get("criticality_ranking")
    if not rows:
        return {
            **_missing("check_threshold", "no criticality ranking"),
            "threshold_crossed": False, "triggered_corridor": None,
            "stages_run": ["check_threshold"],
        }
    try:
        result = _check_threshold(rows, state["baseline_flow_mbd"])
    except Exception as exc:
        return {
            **_failed("check_threshold", exc),
            "threshold_crossed": False, "triggered_corridor": None,
            "stages_run": ["check_threshold"],
        }
    return {
        "threshold": result,
        "threshold_crossed": result["threshold_crossed"],
        "triggered_corridor": result["triggered_corridor"],
        "stages_run": ["check_threshold"],
    }


def route_after_threshold(state):
    return "response" if state.get("threshold_crossed") else "dashboard_update"


def response(state, config=None):
    opts = _opts(config)
    try:
        resp = build_response(
            load_live_graph(),
            corridor=state["triggered_corridor"],
            crisis=opts.get("crisis", "normal"),
            duration_days=opts.get("duration_days", 14),
            include_sanctioned=opts.get("include_sanctioned", False),
        )
    except Exception as exc:
        return {**_failed("response", exc), "stages_run": ["response"]}
    return {
        "reroute_recommendations": resp["reroute"],
        "spr_schedule": resp["spr"],
        "response": resp,
        "stages_run": ["response"],
    }


def _parse_started(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def dashboard_update(state, config=None):
    """Persist the run as one PipelineRun row — what the dashboard reads."""
    if not _opts(config).get("persist", True):
        return {"run_id": None}
    stages = list(state.get("stages_run", [])) + ["dashboard_update"]
    try:
        run = PipelineRun.objects.create(
            started_at=_parse_started(state.get("pipeline_run_at")),
            status=PipelineRun.STATUS_PARTIAL if state.get("errors") else PipelineRun.STATUS_SUCCEEDED,
            finished_at=datetime.now(timezone.utc),
            options=dict(_opts(config)),
            stages_run=stages,
            ingest_report=state.get("ingest_report"),
            extraction_report=state.get("extraction_report"),
            risk_scores=state.get("risk_scores") or {},
            criticality=state.get("criticality_ranking") or [],
            threshold=state.get("threshold"),
            triggered_corridor=state.get("triggered_corridor"),
            capacity_loss_mbd=state.get("capacity_loss_mbd"),
            response=state.get("response"),
            errors=list(state.get("errors", [])),
        )
    except Exception as exc:
        return {**_failed("dashboard_update", exc), "run_id": None, "stages_run": ["dashboard_update"]}
    return {"run_id": run.pk, "stages_run": ["dashboard_update"]}

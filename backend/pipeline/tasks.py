"""Pipeline entry points.

Plain synchronous functions — no task queue. Celery/Redis/Beat are out of scope
for this build (no always-on host to run them); the LangGraph orchestrator calls
these directly. CELERY_BEAT_SCHEDULE in config/settings.py references these names
purely as documentation of the intended production cadence.

Each function swallows its own failures and returns a count, so one dead source
can never abort a pipeline run.
"""
import logging

from graph.updater import refresh_graph_risk
from pipeline.extract.extractor import extract_pending_events
from pipeline.ingest.gdelt import (
    CORRIDOR_QUERIES,
    FETCH_ERROR,
    fetch_by_corridor,
    store_gdelt_articles,
)
from pipeline.ingest.ofac import download_ofac_sdn
from pipeline.ingest.rss import fetch_energy_news, fetch_shipping_news, store_rss_articles
from pipeline.score.risk_scorer import compute_all_risk_scores

logger = logging.getLogger(__name__)


def poll_gdelt_by_corridor():
    """Fetch and store GDELT articles, reporting per corridor.

    Returns {corridor: {"fetched": int, "stored": int, "status": str,
    "sampled": bool}}. These must not be collapsed into one number:
    ``sampled=False`` means the query never got an answer, so that corridor's
    absence from the corpus is an artefact and its risk score is NOT comparable
    with the others. ``fetched=0`` with ``sampled=True`` is the opposite — real
    evidence the corridor is quiet.
    """
    empty_report = {
        c: {"fetched": 0, "stored": 0, "status": FETCH_ERROR, "sampled": False}
        for c in CORRIDOR_QUERIES
    }
    try:
        by_corridor = fetch_by_corridor()
    except Exception:
        logger.exception("poll_gdelt failed")
        return empty_report

    report = {}
    seen_urls = set()
    for corridor, result in by_corridor.items():
        # An article matching two corridors is stored once, against the first.
        fresh = []
        for article in result.articles:
            if article["url"] in seen_urls:
                continue
            seen_urls.add(article["url"])
            fresh.append(article)
        try:
            stored = store_gdelt_articles(fresh)
        except Exception:
            logger.exception("storing GDELT articles failed for %s", corridor)
            stored = 0
        report[corridor] = {
            "fetched": len(result.articles),
            "stored": stored,
            "status": result.status,
            "sampled": result.sampled,
        }

    return report


def poll_gdelt():
    """Fetch and store new GDELT articles for every corridor. Returns the number
    of rows created."""
    return sum(counts["stored"] for counts in poll_gdelt_by_corridor().values())


def poll_rss():
    """Fetch and store both RSS feeds. Returns the total number of rows created."""
    stored = 0
    for label, fetch in (("energy", fetch_energy_news), ("shipping", fetch_shipping_news)):
        try:
            stored += store_rss_articles(fetch())
        except Exception:
            logger.exception("poll_rss failed for the %s feed", label)
    return stored


def download_ofac():
    """Refresh the OFAC SDN cache. Returns the number of entities cached."""
    try:
        return len(download_ofac_sdn())
    except Exception:
        logger.exception("download_ofac failed")
        return 0


def extract_events(limit=None):
    """Turn unprocessed RawArticles into ExtractedEvents. Returns the counts
    dict from extract_pending_events."""
    try:
        return extract_pending_events(limit=limit)
    except Exception:
        logger.exception("extract_events failed")
        return {"articles": 0, "events": 0, "irrelevant": 0, "unparseable": 0, "call_failed": 0}


def score_and_update_graph():
    """Recompute every corridor's risk score and push it onto the graph.

    Returns {corridor: normalized_score}. The graph update is attempted even
    though scoring already persisted its results, so a graph failure still
    leaves the RiskScore history intact.
    """
    try:
        scores = compute_all_risk_scores()
    except Exception:
        logger.exception("risk scoring failed")
        return {}

    try:
        refresh_graph_risk(scores)
    except Exception:
        logger.exception("graph weight update failed")

    return scores

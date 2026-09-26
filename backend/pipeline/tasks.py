"""Pipeline entry points.

Plain synchronous functions — no task queue. Celery/Redis/Beat are out of scope
for this build (no always-on host to run them); the LangGraph orchestrator calls
these directly. CELERY_BEAT_SCHEDULE in config/settings.py references these names
purely as documentation of the intended production cadence.

Each function swallows its own failures and returns a count, so one dead source
can never abort a pipeline run.
"""
import logging
import time

from graph.updater import refresh_graph_risk
from pipeline.extract.extractor import extract_pending_events
from pipeline.ingest.gdelt import (
    _INTER_QUERY_DELAY_SECONDS as GDELT_INTER_QUERY_DELAY_SECONDS,
    CORRIDOR_QUERIES,
    FETCH_ERROR,
    fetch_by_corridor,
    fetch_corridor,
    store_gdelt_articles,
)
from pipeline.ingest.gdelt_gkg import (
    CORRIDOR_KEYWORDS as GKG_CORRIDOR_KEYWORDS,
    fetch_by_corridor as fetch_gkg_by_corridor,
    store_gkg_articles,
)
from pipeline.ingest.ofac import download_ofac_sdn
from pipeline.ingest.rss import fetch_energy_news, fetch_shipping_news, store_rss_articles
from pipeline.score.risk_scorer import compute_all_risk_scores

logger = logging.getLogger(__name__)


def poll_gdelt_by_corridor(max_records=None, last_minutes=None):
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
        by_corridor = fetch_by_corridor(
            max_records_per_corridor=max_records, last_minutes=last_minutes
        )
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


def poll_gdelt_corridor(corridor_name, max_records=None, last_minutes=None):
    """Fetch and store ONE corridor's GDELT articles.

    Returns ``{"fetched", "stored", "status", "sampled"}`` — the same shape as
    a single entry of :func:`poll_gdelt_by_corridor`, so both paths report
    identically. Raises ValueError on an unknown corridor (a typo should be
    loud, not silently reported as an unsampled corridor).

    ``max_records`` overrides ``DEFAULT_MAX_RECORDS`` (50). A smaller request
    may survive GDELT's rate limiter better — untested, since measuring it
    means deliberately tripping the limiter again.
    """
    result = fetch_corridor(
        corridor_name, max_records=max_records, last_minutes=last_minutes
    )

    try:
        stored = store_gdelt_articles(result.articles)
    except Exception:
        logger.exception("storing GDELT articles failed for %s", corridor_name)
        stored = 0

    return {
        "fetched": len(result.articles),
        "stored": stored,
        "status": result.status,
        "sampled": result.sampled,
    }


def poll_gdelt_gkg(last_minutes=None):
    """Fetch and store GDELT GKG bulk-file articles, reporting per corridor.

    Same return shape as :func:`poll_gdelt_by_corridor`, so reporting code works
    against either path. Unlike the DOC API this cannot be rate-limited, but it
    downloads roughly 300 MB for a 24h window — call it deliberately, not on a
    timer.

    All three corridors share one download and one status: they are filtered
    from the same slices, so a partial read thins every corridor equally rather
    than starving one. That is the property the Phase 2.5 fix was defending.
    """
    empty_report = {
        c: {"fetched": 0, "stored": 0, "status": FETCH_ERROR, "sampled": False}
        for c in GKG_CORRIDOR_KEYWORDS
    }
    try:
        by_corridor = fetch_gkg_by_corridor(last_minutes=last_minutes)
    except Exception:
        logger.exception("poll_gdelt_gkg failed")
        return empty_report

    report = {}
    for corridor, result in by_corridor.items():
        try:
            stored = store_gkg_articles(result.articles)
        except Exception:
            logger.exception("storing GKG articles failed for %s", corridor)
            stored = 0
        report[corridor] = {
            "fetched": len(result.articles),
            "stored": stored,
            "status": result.status,
            "sampled": result.sampled,
        }
    return report


DOC_NOT_ATTEMPTED = "not_attempted"


def poll_gdelt_with_fallback(max_records=None, last_minutes=None):
    """DOC API first, GKG bulk files the moment DOC stops answering.

    One single-shot DOC request per corridor (``attempts=1``, no backoff), in a
    fixed order. On the FIRST corridor that is not sampled (429 or error) no
    further DOC request is made — every blocked request extends GDELT's per-IP
    block, which is why the retry loop cannot win — and GKG is polled for the
    same window. GKG covers ALL three corridors from one identical corpus, so
    the fallback keeps the corridors balanced (the Phase 2.5 fairness
    property) instead of topping up only the ones DOC missed. DOC rows already
    stored are kept; ``store_*`` dedups on URL, and the two paths were measured
    to find disjoint articles anyway.

    Mixing paths is acceptable only because production scoring is the
    bounded top-3-story mean, measured at 1.00x across ingestion paths (Phase
    4.5); under the old summed score it would have inverted the ranking. Each
    corridor's ``path`` ("doc", "doc+gkg" or "gkg") is reported so a run's
    provenance is never hidden.

    Downloads ~280 MB for a 24h window when it falls back — which is why it is
    only reachable from an explicit ``run_pipeline --full`` /
    ``--ingest gdelt-fallback``, never from ``poll_sources --source all``.

    Returns ``{corridor: {"fetched", "stored", "status", "sampled", "path",
    "doc_status"}}`` — the shared per-corridor shape plus provenance.
    """
    report = {}
    fell_back = False
    for i, corridor in enumerate(CORRIDOR_QUERIES):
        if fell_back:
            report[corridor] = {
                "fetched": 0, "stored": 0, "status": DOC_NOT_ATTEMPTED,
                "sampled": False, "doc_status": DOC_NOT_ATTEMPTED,
            }
            continue
        if i:
            time.sleep(GDELT_INTER_QUERY_DELAY_SECONDS)
        result = fetch_corridor(
            corridor, max_records=max_records, last_minutes=last_minutes, attempts=1,
        )
        try:
            stored = store_gdelt_articles(result.articles)
        except Exception:
            logger.exception("storing GDELT articles failed for %s", corridor)
            stored = 0
        report[corridor] = {
            "fetched": len(result.articles), "stored": stored, "status": result.status,
            "sampled": result.sampled, "doc_status": result.status,
        }
        if not result.sampled:
            fell_back = True
            logger.warning(
                "GDELT DOC %s for %s - no further DOC requests; falling back to GKG "
                "for all corridors", result.status, corridor,
            )

    if not fell_back:
        for counts in report.values():
            counts["path"] = "doc"
        return report

    gkg = poll_gdelt_gkg(last_minutes=last_minutes)
    for corridor, counts in report.items():
        g = gkg.get(corridor, {"fetched": 0, "stored": 0, "status": FETCH_ERROR, "sampled": False})
        doc_sampled = counts["sampled"]
        counts["path"] = "doc+gkg" if doc_sampled else "gkg"
        counts["fetched"] += g["fetched"]
        counts["stored"] += g["stored"]
        counts["sampled"] = doc_sampled or g["sampled"]
        counts["status"] = g["status"] if not doc_sampled else counts["status"]
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

"""Pipeline entry points.

Plain synchronous functions — no task queue. Celery/Redis/Beat are out of scope
for this build (no always-on host to run them); the LangGraph orchestrator calls
these directly. CELERY_BEAT_SCHEDULE in config/settings.py references these names
purely as documentation of the intended production cadence.

Each function swallows its own failures and returns a count, so one dead source
can never abort a pipeline run.
"""
import logging

from pipeline.ingest.gdelt import CORRIDOR_QUERIES, fetch_by_corridor, store_gdelt_articles
from pipeline.ingest.ofac import download_ofac_sdn
from pipeline.ingest.rss import fetch_energy_news, fetch_shipping_news, store_rss_articles

logger = logging.getLogger(__name__)


def poll_gdelt_by_corridor():
    """Fetch and store GDELT articles, reporting per corridor.

    Returns {corridor: {"fetched": int, "stored": int}}. The two numbers mean
    different things and shouldn't be collapsed: fetched=0 means the query was
    throttled or matched nothing (the case worth flagging), while stored=0 with
    fetched>0 just means every hit was already in the database.
    """
    empty_report = {c: {"fetched": 0, "stored": 0} for c in CORRIDOR_QUERIES}
    try:
        by_corridor = fetch_by_corridor()
    except Exception:
        logger.exception("poll_gdelt failed")
        return empty_report

    report = {}
    seen_urls = set()
    for corridor, articles in by_corridor.items():
        # An article matching two corridors is stored once, against the first.
        fresh = []
        for article in articles:
            if article["url"] in seen_urls:
                continue
            seen_urls.add(article["url"])
            fresh.append(article)
        try:
            stored = store_gdelt_articles(fresh)
        except Exception:
            logger.exception("storing GDELT articles failed for %s", corridor)
            stored = 0
        report[corridor] = {"fetched": len(articles), "stored": stored}

    starved = [c for c, counts in report.items() if not counts["fetched"]]
    if starved:
        logger.warning("GDELT returned no articles for: %s", ", ".join(starved))
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

"""GDELT DOC 2.0 API ingestion.

Free, no API key. The DOC API returns article *metadata* (title, url, domain,
seendate) — not full body text — so raw_text is assembled from that metadata and
Phase 3 extraction works off the headline plus context.
"""
import logging
import time

import requests

from pipeline.ingest import store_articles

logger = logging.getLogger(__name__)

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
SOURCE_LABEL = "gdelt"

# One query per corridor rather than one OR'd blob: "tanker"/"oil" alone are
# broad enough to fill the whole maxrecords budget with generic oil-market
# news, crowding out corridor-specific hits — Cape of Good Hope traffic in
# particular gets no coverage at all unless it has its own query. Names match
# the exact Corridor rows in data/corridors.json (Hormuz / Red Sea / Cape).
CORRIDOR_QUERIES = {
    "Hormuz": '("Strait of Hormuz" OR Hormuz) (tanker OR oil OR sanctions OR military OR Iran) sourcelang:eng',
    "Red Sea": '("Red Sea" OR "Bab-el-Mandeb" OR "Suez Canal") (tanker OR shipping OR Houthi OR attack) sourcelang:eng',
    "Cape": '("Cape of Good Hope" OR "Cape route") (tanker OR oil OR piracy OR shipping) sourcelang:eng',
}
DEFAULT_MAX_RECORDS = 50

# GDELT allows roughly one request per 5s per IP and answers 429 otherwise. A
# run happens every 6h, so a single throttled attempt would cost a whole cycle
# of our only geopolitical-event source — retry a couple of times before giving up.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 6

# Separate from the retry backoff: the minimum gap between the *successful*
# per-corridor queries in fetch_all_corridors(), so three back-to-back calls
# don't themselves trip the 1-req/5s limit.
_INTER_QUERY_DELAY_SECONDS = 5


def fetch_gdelt_articles(query, corridor_hint=None, max_records=None, timeout=15):
    """Query the GDELT DOC API and return a list of normalized article dicts.

    Never raises — logs and returns [] on any failure, so a dead or rate-limited
    GDELT can't take the pipeline down. `corridor_hint`, when given, is stitched
    into raw_text and used only for logging (no schema change / no DB write).
    """
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_records or DEFAULT_MAX_RECORDS,
        "sort": "datedesc",
    }
    payload = _get_with_retry(params, timeout)
    if payload is None:
        return []

    articles = []
    for item in payload.get("articles", []):
        url = item.get("url")
        title = (item.get("title") or "").strip()
        if not url or not title:
            logger.debug("skipping GDELT record with no url/title: %r", item)
            continue
        articles.append(
            {
                "url": url,
                "source": SOURCE_LABEL,
                "title": title[:500],
                "raw_text": _build_raw_text(item, title, corridor_hint),
            }
        )

    logger.info("GDELT query %r returned %d usable articles", corridor_hint or query, len(articles))
    return articles


def fetch_by_corridor(max_records_per_corridor=None, timeout=15):
    """Run one GDELT query per corridor (Hormuz / Red Sea / Cape).

    Returns {corridor: [articles]}. A corridor whose query was throttled or
    matched nothing maps to an empty list — the caller can tell which corridor
    came back empty rather than only seeing a smaller total.
    """
    results = {}
    for i, (corridor, query) in enumerate(CORRIDOR_QUERIES.items()):
        if i > 0:
            time.sleep(_INTER_QUERY_DELAY_SECONDS)
        results[corridor] = fetch_gdelt_articles(
            query, corridor_hint=corridor, max_records=max_records_per_corridor, timeout=timeout
        )
    return results


def fetch_all_corridors(max_records_per_corridor=None, timeout=15):
    """Flat, URL-deduplicated view of fetch_by_corridor() — an article can
    legitimately match more than one corridor's query."""
    return dedupe_by_url(fetch_by_corridor(max_records_per_corridor, timeout).values())


def dedupe_by_url(article_lists):
    """Flatten an iterable of article lists, keeping the first sighting of each URL."""
    seen_urls = set()
    merged = []
    for articles in article_lists:
        for article in articles:
            if article["url"] in seen_urls:
                continue
            seen_urls.add(article["url"])
            merged.append(article)
    return merged


def _get_with_retry(params, timeout):
    """Return the decoded GDELT payload, or None if every attempt failed."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            response = requests.get(GDELT_DOC_API, params=params, timeout=timeout)
            response.raise_for_status()
            # GDELT serves HTML or truncated JSON when throttled, so a decode
            # failure here is an expected operational case, not a bug.
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.warning("GDELT fetch failed after %d attempts: %s", attempt, exc)
                return None
            logger.debug("GDELT attempt %d failed (%s), retrying", attempt, exc)
            time.sleep(_RETRY_BACKOFF_SECONDS)


def _build_raw_text(item, title, corridor_hint=None):
    """GDELT gives no body text — stitch the available metadata into something
    the extraction prompt can reason over. corridor_hint is which query matched
    (not a claim about the article's true corridor — Phase 3 extraction still
    makes that call), included as a cheap signal for the LLM prompt."""
    parts = [title]
    if corridor_hint:
        parts.append(f"matched_corridor_query: {corridor_hint}")
    for key in ("domain", "sourcecountry", "seendate"):
        value = item.get(key)
        if value:
            parts.append(f"{key}: {value}")
    return "\n".join(parts)


def store_gdelt_articles(articles):
    """Persist fetched GDELT articles, deduplicated by URL. Returns count created."""
    created = store_articles(articles)
    logger.info("stored %d new GDELT articles", created)
    return created

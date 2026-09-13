"""GDELT DOC 2.0 API ingestion.

Free, no API key. The DOC API returns article *metadata* (title, url, domain,
seendate) — not full body text — so raw_text is assembled from that metadata and
Phase 3 extraction works off the headline plus context.
"""
import logging
import random
import time
from typing import NamedTuple

import requests

from pipeline.ingest import store_articles

logger = logging.getLogger(__name__)

# A query that was throttled and one that genuinely matched nothing both used to
# return [], which made a one-sided corpus invisible: on 2026-09-13 the Red Sea
# query returned 29 articles while Hormuz and Cape were silently 429'd, and the
# resulting corridor risk ranking reflected which query survived rather than
# which corridor was at risk. These statuses keep the two cases apart.
FETCH_OK = "ok"                # answered, articles returned
FETCH_EMPTY = "empty"          # answered, nothing matched — a real signal
FETCH_THROTTLED = "throttled"  # rate-limited; we never got an answer
FETCH_ERROR = "error"          # anything else (network, malformed payload)

#: Statuses meaning "this corridor was not sampled this run" — never treat these
#: as evidence that a corridor is quiet.
FETCH_FAILED = (FETCH_THROTTLED, FETCH_ERROR)


class CorridorFetch(NamedTuple):
    corridor: str
    articles: list
    status: str

    @property
    def sampled(self):
        return self.status not in FETCH_FAILED

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

# GDELT allows roughly one request per 5s per IP and answers 429 otherwise — in
# practice noticeably stricter, and it stays angry for a while once tripped. A
# run happens every 6h, so a throttled attempt costs a whole cycle of our only
# geopolitical-event source. Exponential backoff with jitter, and Retry-After
# honoured when GDELT bothers to send it.
_RETRY_ATTEMPTS = 5
_RETRY_BASE_SECONDS = 5
_RETRY_MAX_SECONDS = 60

# Separate from the retry backoff: the minimum gap between the per-corridor
# queries, so three back-to-back calls don't themselves trip the limit.
_INTER_QUERY_DELAY_SECONDS = 10

# Corridors throttled on the first pass are retried once after this cooldown,
# rather than being written off for the whole cycle.
_SECOND_PASS_COOLDOWN_SECONDS = 30


def fetch_gdelt_articles(query, corridor_hint=None, max_records=None, timeout=15):
    """Query GDELT and return a list of normalized article dicts.

    Never raises — returns [] on any failure. Callers that need to tell a
    throttled query from one that genuinely matched nothing must use
    :func:`fetch_gdelt_result` instead; this wrapper discards that distinction.
    """
    return fetch_gdelt_result(query, corridor_hint, max_records, timeout).articles


def fetch_gdelt_result(query, corridor_hint=None, max_records=None, timeout=15):
    """Query the GDELT DOC API and return a :class:`CorridorFetch`.

    Never raises, so a dead or rate-limited GDELT can't take the pipeline down —
    but the outcome is reported honestly via ``.status``. `corridor_hint`, when
    given, is stitched into raw_text and used for logging (no schema change).
    """
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_records or DEFAULT_MAX_RECORDS,
        "sort": "datedesc",
    }
    payload, status = _get_with_retry(params, timeout)
    if payload is None:
        logger.warning(
            "GDELT query for %r was not sampled (%s)", corridor_hint or query, status
        )
        return CorridorFetch(corridor_hint, [], status)

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
    return CorridorFetch(corridor_hint, articles, FETCH_OK if articles else FETCH_EMPTY)


def fetch_by_corridor(max_records_per_corridor=None, timeout=15):
    """Run one GDELT query per corridor (Hormuz / Red Sea / Cape).

    Returns {corridor: CorridorFetch}, so callers can distinguish a corridor
    that is genuinely quiet from one that was never successfully sampled.

    Two fairness measures matter here, because throttling tends to kill whichever
    queries run last: the corridor order is shuffled each run so no corridor is
    systematically starved, and any corridor throttled on the first pass gets a
    second attempt after a cooldown.
    """
    order = random.sample(list(CORRIDOR_QUERIES.items()), len(CORRIDOR_QUERIES))

    results = {}
    for i, (corridor, query) in enumerate(order):
        if i > 0:
            time.sleep(_INTER_QUERY_DELAY_SECONDS)
        results[corridor] = fetch_gdelt_result(
            query, corridor_hint=corridor, max_records=max_records_per_corridor, timeout=timeout
        )

    retry = [c for c, result in results.items() if result.status == FETCH_THROTTLED]
    if retry:
        logger.warning(
            "GDELT throttled %s on the first pass — retrying after %ds cooldown",
            ", ".join(retry), _SECOND_PASS_COOLDOWN_SECONDS,
        )
        time.sleep(_SECOND_PASS_COOLDOWN_SECONDS)
        for i, corridor in enumerate(retry):
            if i > 0:
                time.sleep(_INTER_QUERY_DELAY_SECONDS)
            results[corridor] = fetch_gdelt_result(
                CORRIDOR_QUERIES[corridor], corridor_hint=corridor,
                max_records=max_records_per_corridor, timeout=timeout,
            )

    starved = [c for c, result in results.items() if not result.sampled]
    if starved:
        logger.error(
            "GDELT did not sample %s this run — corridor risk scores computed from "
            "this corpus are NOT comparable across corridors",
            ", ".join(starved),
        )
    return results


def fetch_all_corridors(max_records_per_corridor=None, timeout=15):
    """Flat, URL-deduplicated view of fetch_by_corridor() — an article can
    legitimately match more than one corridor's query."""
    return dedupe_by_url(
        result.articles
        for result in fetch_by_corridor(max_records_per_corridor, timeout).values()
    )


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


def _is_throttled(exc):
    """True if this failure is GDELT rate-limiting us rather than a real error."""
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    # Tests (and some GDELT responses) carry the code only in the message.
    return "429" in str(exc) or "Too Many Requests" in str(exc)


def _retry_after_seconds(exc):
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        return min(float(headers.get("Retry-After")), _RETRY_MAX_SECONDS)
    except (TypeError, ValueError):
        return None


def _backoff_seconds(attempt, exc):
    """Exponential backoff with jitter, or Retry-After when GDELT sends one.

    Jitter matters because all three corridor queries back off in lockstep
    otherwise, and retry into the same rate-limit window together.
    """
    explicit = _retry_after_seconds(exc)
    if explicit is not None:
        return explicit
    delay = min(_RETRY_BASE_SECONDS * (2 ** (attempt - 1)), _RETRY_MAX_SECONDS)
    return delay + random.uniform(0, delay * 0.25)


def _get_with_retry(params, timeout):
    """Return ``(payload, status)``; payload is None unless status is FETCH_OK.

    The status is what lets callers separate "GDELT never answered" from "GDELT
    answered, nothing matched" — see the FETCH_* constants.
    """
    last_status = FETCH_ERROR
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            response = requests.get(GDELT_DOC_API, params=params, timeout=timeout)
            response.raise_for_status()
            # GDELT serves HTML or truncated JSON when throttled, so a decode
            # failure here is an expected operational case, not a bug.
            return response.json(), FETCH_OK
        except (requests.RequestException, ValueError) as exc:
            last_status = FETCH_THROTTLED if _is_throttled(exc) else FETCH_ERROR
            if attempt == _RETRY_ATTEMPTS:
                logger.warning(
                    "GDELT fetch failed after %d attempts (%s): %s",
                    attempt, last_status, exc,
                )
                return None, last_status
            delay = _backoff_seconds(attempt, exc)
            logger.debug(
                "GDELT attempt %d failed (%s), retrying in %.1fs", attempt, exc, delay
            )
            time.sleep(delay)
    return None, last_status


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

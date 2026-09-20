"""GDELT GKG 2.0 bulk-file ingestion — the un-throttled alternative to the DOC API.

Why this exists
---------------
``gdelt.py`` queries the DOC 2.0 API, which is rate-limited per IP and in
practice answers 429 after a burst, then stays angry for minutes (see the Phase
2.5 notes in CLAUDE.md). This module reads the same underlying corpus from
GDELT's *static* 15-minute CSV drops instead. Those are plain files on a CDN
with no per-request limiter, and GDELT never deletes them — so the identical
code path serves both live polling and Phase 7's historical reconstruction.

The trade-off, measured rather than assumed (2026-09-20, one real slice):

    slice size          3.1 MB zipped  (~300 MB for a 24h window, 96 slices)
    rows per slice      676 articles
    PAGE_TITLE          676/676 rows — 100%
    PAGE_PRECISEPUBTIMESTAMP  377/676 rows — 56%
    publication lag     13:15 slice available, 13:30 not yet, at 14:05 UTC

So: far more bytes than the DOC API's few KB of JSON, but the request always
arrives. Corridor selection moves from GDELT's server-side search to the local
matcher below.

That relocation is a genuine improvement for the Phase 2.5 sampling-bias
problem, not just a workaround: all three corridors are filtered out of one
identical corpus, so throttling can no longer starve one corridor relative to
another. It does mean the recall characteristics differ from the DOC API's
search, so a GKG-sourced corpus is NOT directly comparable with a DOC-sourced
one — which is why rows are stored under their own ``SOURCE_LABEL``.

File format
-----------
Tab-delimited, 27 columns, no header row. GKG has no title *column*; the title
lives in the V2EXTRASXML field as ``<PAGE_TITLE>``, which is why
:func:`_extract_tag` exists. Titles matter beyond display: Phase 3's story
deduplication clusters on headline similarity, so a source with no title could
not feed it.
"""
import html
import io
import logging
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone

import requests

from pipeline.ingest import store_articles
from pipeline.ingest.gdelt import (
    FETCH_EMPTY,
    FETCH_ERROR,
    FETCH_OK,
    CorridorFetch,
)

logger = logging.getLogger(__name__)

GKG_BASE_URL = "https://data.gdeltproject.org/gdeltv2"

#: Distinct from gdelt.SOURCE_LABEL ("gdelt") on purpose. The two paths select
#: articles by different mechanisms — GDELT's search vs. our keyword matcher —
#: so a corpus mixing them is not homogeneous. Keeping the label separate means
#: that difference can be measured later instead of assumed away.
SOURCE_LABEL = "gdelt_gkg"

#: GDELT publishes one set of files every 15 minutes, on the quarter hour.
SLICE_MINUTES = 15

#: Slices newer than this are skipped rather than requested. GDELT documents a
#: rolling indexing delay; measured above, the 13:30 slice was still absent at
#: 14:05, so anything under an hour old is unreliable. Requesting it just buys
#: 404s and makes real gaps harder to see.
PUBLICATION_LAG_MINUTES = 90

#: Matches gdelt.DEFAULT_LAST_MINUTES so both paths default to the same window.
DEFAULT_LAST_MINUTES = 1440

# --- column layout (0-indexed, 27 columns total) -----------------------------
COL_DATE = 1              # V2.1DATE — when GDELT saw it, YYYYMMDDHHMMSS
COL_DOMAIN = 3            # V2SOURCECOMMONNAME
COL_URL = 4               # V2DOCUMENTIDENTIFIER
COL_THEMES = 7            # V1THEMES, semicolon-delimited
COL_TRANSLATION = 25      # V2.1TRANSLATIONINFO — non-empty means translated
COL_EXTRAS = 26           # V2EXTRASXML — holds PAGE_TITLE
GKG_COLUMNS = 27

#: Unambiguous, geographic corridor names. A hit here needs only the general
#: topic gate below, because the place itself is the corridor.
CORRIDOR_KEYWORDS = {
    "Hormuz": ("strait of hormuz", "hormuz"),
    "Red Sea": ("red sea", "bab-el-mandeb", "bab el-mandeb", "mandeb",
                "suez canal", "gulf of aden"),
    "Cape": ("cape of good hope", "cape route"),
}

#: Actors and places that *imply* a corridor without naming it. These need a
#: MARITIME signal, not merely the general topic gate.
#:
#: This tier exists because of a measured failure, not a hypothetical one. With
#: "houthi" in CORRIDOR_KEYWORDS and "missile"/"attack"/"military" in
#: TOPIC_KEYWORDS, the first live run tagged 27 articles Red Sea of which ~24
#: were Houthi ballistic-missile attacks on Riyadh — Yemen-Saudi land conflict
#: with no maritime content at all. Over-matching is tolerable (the extractor
#: overrides the hint), but at 89% of a corridor's intake it stops being a hint
#: and starts being the corpus.
CORRIDOR_WEAK_KEYWORDS = {
    "Red Sea": ("houthi", "bab al-mandab", "yanbu", "jeddah", "hodeidah",
                "mocha", "perim", "eritrea", "djibouti"),
}

#: An article must also look like it is about oil/shipping/disruption, mirroring
#: the second clause of each DOC query (e.g. ``(tanker OR oil OR sanctions ...)``).
#: Without this, "Red Sea" alone matches tourism and marine-biology coverage.
#:
#: This gate is deliberately PERMISSIVE, because it only ever applies to the
#: unambiguous geographic names in CORRIDOR_KEYWORDS — a headline containing
#: "Hormuz" is about the strait essentially always, so the gate's job is to
#: reject coral reefs, not to adjudicate relevance. Closure/transit vocabulary
#: is included because measurement showed its absence dropped
#: "Hormuz to stay closed until US meets conditions" — the most on-topic
#: headline in the whole sample. Military words are safe here now that `houthi`
#: sits in CORRIDOR_WEAK_KEYWORDS behind the strict maritime gate.
TOPIC_KEYWORDS = (
    "oil", "crude", "tanker", "opec", "petroleum", "refinery", "refining",
    "shipping", "vessel", "cargo", "freight", "lng", "sanction", "embargo",
    "blockade", "aramco", "barrel", "export", "import", "pipeline", "port",
    "closed", "closure", "close", "reopen", "reopening", "shut", "transit",
    "traffic", "convoy", "escort", "strait", "chokepoint", "disruption",
    "deployment", "strike", "attack", "missile", "drone", "naval", "navy",
    "military", "escalation", "escalate", "seize", "seized", "warship",
    "crisis", "tension", "standoff", "blockade",
)

#: The stricter gate for CORRIDOR_WEAK_KEYWORDS: the story must be about
#: shipping or a maritime/naval action, not a land war.
MARITIME_KEYWORDS = (
    "tanker", "vessel", "ship", "shipping", "convoy", "port", "strait",
    "shipping lane", "sea lane", "maritime", "naval", "navy", "freight",
    "cargo", "seafarer", "chokepoint", "blockade", "pipeline", "terminal",
    "refinery", "aramco", "crude", "oil",
)

#: GKG's own topic codes, an additional route past TOPIC_KEYWORDS — a headline
#: can be about an oil disruption without using any of those words. Measured on
#: one slice: ENV_OIL 56 rows, MARITIME 61, ECON_OILPRICE 5.
#:
#: MILITARY and ARMEDCONFLICT are deliberately absent for the same reason the
#: military words moved out of TOPIC_KEYWORDS — they tag every regional
#: conflict story in the feed.
TOPIC_THEMES = (
    "ENV_OIL", "MARITIME", "ECON_OILPRICE", "MARITIME_INCIDENT",
    "ECON_SANCTIONS",
)

# Static files on a CDN, so there is no rate limiter to back off from — these
# retries exist only for transient network failure, and stay deliberately
# shorter than gdelt.py's (which is fighting an actual limiter).
_RETRY_ATTEMPTS = 3
_RETRY_BASE_SECONDS = 2

#: Below this fraction of a window's slices, the window is treated as not
#: sampled at all rather than reported as thin coverage.
_MIN_COVERAGE = 0.5


def slice_timestamps(last_minutes=None, now=None):
    """Return the 15-minute slice timestamps covering the window, oldest first.

    Timestamps are datetimes aligned to :data:`SLICE_MINUTES` and exclude
    anything inside :data:`PUBLICATION_LAG_MINUTES` of *now*.
    """
    window = DEFAULT_LAST_MINUTES if last_minutes is None else last_minutes
    now = now or datetime.now(timezone.utc)

    newest = _floor_to_slice(now - timedelta(minutes=PUBLICATION_LAG_MINUTES))
    oldest = _floor_to_slice(now - timedelta(minutes=window))

    stamps = []
    current = oldest
    while current <= newest:
        stamps.append(current)
        current += timedelta(minutes=SLICE_MINUTES)
    return stamps


def slice_url(stamp):
    """URL of the GKG file for one slice, e.g. ``.../20260920130000.gkg.csv.zip``."""
    return f"{GKG_BASE_URL}/{stamp.strftime('%Y%m%d%H%M%S')}.gkg.csv.zip"


def fetch_slice(stamp, timeout=60):
    """Download and parse one slice. Returns a list of record dicts, or None.

    None means the slice could not be read — missing (GDELT skips slices and
    lags behind its own pointer file) or a network failure. That is distinct
    from an empty list, which would mean a slice with no usable rows.
    """
    url = slice_url(stamp)
    payload = _get_with_retry(url, timeout)
    if payload is None:
        return None
    try:
        return list(parse_gkg_zip(payload))
    except (zipfile.BadZipFile, ValueError) as exc:
        logger.warning("could not parse GKG slice %s: %s", url, exc)
        return None


def parse_gkg_zip(payload):
    """Yield record dicts from a raw GKG ``.zip`` payload."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        if not names:
            raise ValueError("empty GKG archive")
        text = archive.read(names[0]).decode("utf-8", errors="replace")
    yield from parse_gkg_csv(text)


def parse_gkg_csv(text):
    """Yield one record dict per usable GKG row.

    Rows are skipped when they are truncated, carry no URL or title, or are
    translated from another language — the DOC path pins ``sourcelang:eng`` in
    every query, so admitting translated coverage here would make the two
    sources differ in a way unrelated to the corridor filter.
    """
    for line in text.split("\n"):
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < GKG_COLUMNS:
            logger.debug("skipping GKG row with %d columns", len(fields))
            continue
        if fields[COL_TRANSLATION].strip():
            continue

        url = fields[COL_URL].strip()
        # GDELT does not decode entities before writing PAGE_TITLE — a raw
        # "&#xA0;" or "&amp;" in a headline would otherwise reach both the
        # extraction prompt and Phase 3's headline-similarity dedup, where it
        # depresses the match ratio between two copies of the same wire story.
        title = html.unescape(_extract_tag(fields[COL_EXTRAS], "PAGE_TITLE"))
        if not url or not title:
            continue

        yield {
            "url": url,
            "title": title[:500],
            "domain": fields[COL_DOMAIN].strip(),
            "themes": fields[COL_THEMES],
            "seen_at": _parse_gkg_date(fields[COL_DATE]),
            "published_at": _parse_gkg_date(
                _extract_tag(fields[COL_EXTRAS], "PAGE_PRECISEPUBTIMESTAMP")
            ),
        }


def match_corridors(record):
    """Return the corridors this record provisionally belongs to.

    Two tiers, mirroring the two-clause shape of each DOC API query but with a
    stricter second clause for implied corridors:

    * a geographic name in :data:`CORRIDOR_KEYWORDS` needs a general topic signal
    * an implied corridor in :data:`CORRIDOR_WEAK_KEYWORDS` needs a *maritime*
      signal, so a Houthi land offensive does not enter as Red Sea shipping risk

    A record can match more than one corridor, exactly as one article can
    satisfy two DOC queries. The result is still only a hint: it becomes
    ``matched_corridor_query`` in raw_text, and Phase 3's extractor makes the
    real corridor call (Phase 2.5 measured it correctly reassigning 28 of 50).
    """
    haystack = f"{record['title']} {record['url']}".lower()
    themes = record.get("themes", "")

    maritime = _contains_term(haystack, MARITIME_KEYWORDS)
    topical = (
        maritime
        or _contains_term(haystack, TOPIC_KEYWORDS)
        or any(theme in themes for theme in TOPIC_THEMES)
    )

    matched = []
    for corridor, keywords in CORRIDOR_KEYWORDS.items():
        if topical and _contains_term(haystack, keywords):
            matched.append(corridor)

    for corridor, keywords in CORRIDOR_WEAK_KEYWORDS.items():
        if corridor in matched:
            continue
        if maritime and _contains_term(haystack, keywords):
            matched.append(corridor)

    return matched


def fetch_by_corridor(last_minutes=None, now=None, timeout=60):
    """Read a window of GKG slices and sort the matches into corridors.

    Returns ``{corridor: CorridorFetch}`` — the same shape as
    ``gdelt.fetch_by_corridor``, so callers and reporting code are unchanged.

    All three corridors share one download and one status, which is the point:
    they are filtered from an identical corpus, so no corridor can be sampled
    while another is not. A partially-readable window is reported as sampled
    but logged with its coverage; a window where too few slices could be read
    at all is reported as :data:`FETCH_ERROR` for every corridor, because the
    absence of matches would then be an artefact rather than evidence of quiet.
    """
    stamps = slice_timestamps(last_minutes, now)
    if not stamps:
        logger.error("GKG window resolved to zero slices — check last_minutes")
        return {c: CorridorFetch(c, [], FETCH_ERROR) for c in CORRIDOR_KEYWORDS}

    by_corridor = {corridor: [] for corridor in CORRIDOR_KEYWORDS}
    seen_urls = set()
    read = 0

    for stamp in stamps:
        records = fetch_slice(stamp, timeout=timeout)
        if records is None:
            continue
        read += 1
        for record in records:
            if record["url"] in seen_urls:
                continue
            corridors = match_corridors(record)
            if not corridors:
                continue
            seen_urls.add(record["url"])
            # Stored once, against the first matching corridor — same rule as
            # the DOC path's cross-corridor dedup.
            by_corridor[corridors[0]].append(_to_article(record, corridors[0]))

    coverage = read / len(stamps)
    logger.info(
        "GKG window: %d/%d slices read (%.0f%%), %d corridor-matched articles",
        read, len(stamps), coverage * 100, len(seen_urls),
    )

    if coverage < _MIN_COVERAGE:
        logger.error(
            "GKG read only %d of %d slices (%.0f%%) — treating the window as NOT "
            "sampled; corridor risk scores from this run are not comparable",
            read, len(stamps), coverage * 100,
        )
        return {c: CorridorFetch(c, [], FETCH_ERROR) for c in CORRIDOR_KEYWORDS}

    if read < len(stamps):
        logger.warning(
            "GKG window incomplete: %d of %d slices unreadable — coverage is thin "
            "but every corridor was filtered from the same slices, so their "
            "scores stay comparable with each other",
            len(stamps) - read, len(stamps),
        )

    return {
        corridor: CorridorFetch(
            corridor, articles, FETCH_OK if articles else FETCH_EMPTY
        )
        for corridor, articles in by_corridor.items()
    }


def store_gkg_articles(articles):
    """Persist fetched GKG articles, deduplicated by URL. Returns count created."""
    created = store_articles(articles)
    logger.info("stored %d new GKG articles", created)
    return created


def _to_article(record, corridor):
    """Normalize a GKG record to the RawArticle field shape."""
    return {
        "url": record["url"],
        "source": SOURCE_LABEL,
        "title": record["title"],
        "raw_text": _build_raw_text(record, corridor),
    }


def _build_raw_text(record, corridor):
    """Build raw_text in the SAME shape as ``gdelt._build_raw_text``.

    Two deliberate constraints:

    The ``seendate:`` line uses GDELT's compact stamp so that
    ``gdelt.parse_seendate`` recovers it unchanged — Phase 3 reads that line to
    date its ExtractedEvents, and a different key here would silently fall back
    to ingest time, reintroducing the timestamp error fixed on 2026-09-20.
    ``PAGE_PRECISEPUBTIMESTAMP`` is preferred when present (56% of rows) because
    it is the article's real publication time rather than when GDELT saw it.

    GKG themes are NOT included, even though they are available and would be
    useful to the LLM. Adding them would make GKG-sourced prompts richer than
    DOC-sourced ones, so any difference in extraction quality between the two
    corpora could no longer be attributed to the corridor filter.
    """
    stamp = record.get("published_at") or record.get("seen_at")
    parts = [record["title"], f"matched_corridor_query: {corridor}"]
    if record.get("domain"):
        parts.append(f"domain: {record['domain']}")
    if stamp:
        parts.append(f"seendate: {stamp.strftime('%Y%m%dT%H%M%SZ')}")
    return "\n".join(parts)


#: Compiled word-boundary matchers, built once per term tuple.
_TERM_PATTERNS = {}


def _contains_term(haystack, terms):
    """True if any term appears in *haystack* as a whole word.

    Plain substring matching is wrong here and was measured doing damage: with
    ``"port"`` in MARITIME_KEYWORDS, ``"Riyadh airport"`` and ``"supports
    measures"`` both satisfied the maritime gate, letting Houthi land-war
    stories back in as Red Sea shipping risk — the exact failure the two-tier
    split was added to prevent. ``"mine"`` in "determine" and ``"close"`` in
    "closely" are the same trap.

    Boundaries are applied per term so multi-word phrases still match. Hyphens
    count as boundaries rather than word characters, so ``mandeb`` still matches
    inside ``el-mandeb`` — the false positives above are all letter-adjacent,
    so there is nothing to gain from being stricter about hyphens.

    A trailing ``s`` is optional, so the term lists stay singular: without it
    "Tankers divert from Hormuz" failed to match ``tanker``, trading one class
    of miss for another.
    """
    pattern = _TERM_PATTERNS.get(terms)
    if pattern is None:
        alternatives = "|".join(re.escape(term) for term in terms)
        pattern = re.compile(rf"(?<!\w)(?:{alternatives})s?(?!\w)")
        _TERM_PATTERNS[terms] = pattern
    return bool(pattern.search(haystack))


def _extract_tag(extras, tag):
    """Pull one ``<TAG>value</TAG>`` out of GKG's V2EXTRASXML pseudo-XML.

    Not real XML — the field is a bare concatenation of tags with no root
    element and no escaping, so a parser would reject it.
    """
    if not extras:
        return ""
    match = re.search(rf"<{tag}>(.*?)</{tag}>", extras, re.DOTALL)
    return match.group(1).strip() if match else ""


def _parse_gkg_date(value):
    """Parse GKG's ``YYYYMMDDHHMMSS`` into an aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.debug("unparsable GKG date: %r", value)
        return None


def _floor_to_slice(moment):
    """Round a datetime down to the containing 15-minute slice boundary."""
    return moment.replace(
        minute=moment.minute - (moment.minute % SLICE_MINUTES),
        second=0,
        microsecond=0,
    )


def _get_with_retry(url, timeout):
    """Return the raw bytes of a GKG file, or None.

    A 404 is an expected, non-retryable outcome: GDELT lags behind its own
    ``lastupdate.txt`` pointer and occasionally skips a slice entirely.
    """
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            response = requests.get(url, timeout=timeout)
            if response.status_code == 404:
                logger.debug("GKG slice not published: %s", url)
                return None
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.warning(
                    "GKG fetch failed after %d attempts: %s (%s)",
                    attempt, url, exc,
                )
                return None
            time.sleep(_RETRY_BASE_SECONDS * attempt)
    return None

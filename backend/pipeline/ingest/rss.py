"""RSS ingestion for energy and shipping news.

CLAUDE.md names Reuters Energy and Lloyd's List, but neither is usable: Reuters
retired its public RSS feeds in 2020 and Lloyd's List is subscription-only.
ENERGY_NEWS_RSS_URL / SHIPPING_NEWS_RSS_URL below stand in as free, live
equivalents.
"""
import calendar
import logging
from datetime import datetime, timezone

import feedparser

from pipeline.ingest import store_articles

logger = logging.getLogger(__name__)

ENERGY_SOURCE = "oilprice"
SHIPPING_SOURCE = "gcaptain"

ENERGY_NEWS_RSS_URL = "https://oilprice.com/rss/main"
SHIPPING_NEWS_RSS_URL = "https://gcaptain.com/feed/"


def fetch_rss_feed(feed_url, source_label, limit=50):
    """Parse one RSS feed and return a list of normalized article dicts.

    Never raises — logs and returns [] on failure.
    """
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as exc:  # feedparser is lenient, but a bad URL can still blow up
        logger.warning("RSS fetch failed for %s: %s", feed_url, exc)
        return []

    # feedparser signals errors via bozo rather than raising, and still returns
    # whatever entries it managed to parse. Healthy feeds routinely set bozo over
    # trivia (OilPrice's media type, for one), so only escalate when it cost us
    # every entry.
    if getattr(parsed, "bozo", 0):
        detail = getattr(parsed, "bozo_exception", "")
        if parsed.entries:
            logger.debug("RSS feed %s flagged bozo but parsed: %s", feed_url, detail)
        else:
            logger.warning("RSS feed %s yielded no entries: %s", feed_url, detail)

    articles = []
    for entry in parsed.entries[:limit]:
        url = entry.get("link")
        title = (entry.get("title") or "").strip()
        if not url or not title:
            logger.debug("skipping RSS entry with no link/title from %s", feed_url)
            continue
        body = entry.get("summary") or entry.get("description") or title
        articles.append(
            {
                "url": url,
                "source": source_label,
                "title": title[:500],
                "raw_text": _build_raw_text(body, _entry_timestamp(entry)),
            }
        )

    logger.info("%s returned %d usable articles", source_label, len(articles))
    return articles


def _entry_timestamp(entry):
    """Publication time of an RSS entry as an aware UTC datetime, or None.

    feedparser normalizes whatever date format a feed uses into a
    ``time.struct_time`` already converted to UTC, so ``calendar.timegm`` (UTC)
    is correct here and ``time.mktime`` (local time) would be wrong.

    ``published_parsed`` is preferred; ``updated_parsed`` is the fallback, since
    some feeds only carry the latter. Both are absent often enough that the
    caller must handle None.
    """
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if not parsed:
            continue
        try:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            logger.debug("unusable RSS %s: %r", key, parsed)
    return None


def _build_raw_text(body, published_at):
    """Append a ``seendate:`` line so the event can be dated from the article.

    Deliberately the SAME line format ``gdelt.parse_seendate`` reads, so Phase 3
    dates RSS events from publication time without knowing the source. Without
    it, ``ExtractedEvent.timestamp`` silently falls back to ingest time — and
    unlike GDELT's seendate (recoverable by regex from raw_text) there would be
    nothing left in the row to repair it from later, so an RSS event ingested a
    week after publication would be permanently over-weighted by the 0.1/day
    decay.

    No ``matched_corridor_query`` line: RSS feeds are corridor-agnostic, so
    there is no query hint to pass on and the extractor judges corridor purely
    from content.
    """
    if published_at is None:
        return body
    return f"{body}\nseendate: {published_at.strftime('%Y%m%dT%H%M%SZ')}"


def fetch_energy_news():
    return fetch_rss_feed(ENERGY_NEWS_RSS_URL, ENERGY_SOURCE)


def fetch_shipping_news():
    return fetch_rss_feed(SHIPPING_NEWS_RSS_URL, SHIPPING_SOURCE)


def store_rss_articles(articles):
    """Persist fetched RSS articles, deduplicated by URL. Returns count created."""
    created = store_articles(articles)
    logger.info("stored %d new RSS articles", created)
    return created

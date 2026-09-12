"""RSS ingestion for energy and shipping news.

CLAUDE.md names Reuters Energy and Lloyd's List, but neither is usable: Reuters
retired its public RSS feeds in 2020 and Lloyd's List is subscription-only.
ENERGY_NEWS_RSS_URL / SHIPPING_NEWS_RSS_URL below stand in as free, live
equivalents.
"""
import logging

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
        articles.append(
            {
                "url": url,
                "source": source_label,
                "title": title[:500],
                "raw_text": entry.get("summary") or entry.get("description") or title,
            }
        )

    logger.info("%s returned %d usable articles", source_label, len(articles))
    return articles


def fetch_energy_news():
    return fetch_rss_feed(ENERGY_NEWS_RSS_URL, ENERGY_SOURCE)


def fetch_shipping_news():
    return fetch_rss_feed(SHIPPING_NEWS_RSS_URL, SHIPPING_SOURCE)


def store_rss_articles(articles):
    """Persist fetched RSS articles, deduplicated by URL. Returns count created."""
    created = store_articles(articles)
    logger.info("stored %d new RSS articles", created)
    return created

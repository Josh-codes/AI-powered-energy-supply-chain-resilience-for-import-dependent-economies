"""News ingestion sources (GDELT, RSS, OFAC).

Every source normalizes its records to the RawArticle field shape
(url / source / title / raw_text) and hands them to store_articles(), which owns
the one place dedup happens.
"""
import logging

from django.db import IntegrityError, transaction

from core.models import RawArticle

logger = logging.getLogger(__name__)


def store_articles(articles):
    """Insert new RawArticle rows, skipping URLs already seen.

    Uses get_or_create (not update_or_create) so re-ingesting a known URL never
    resets its `processed` flag. Returns the number of rows actually created.
    """
    created_count = 0
    for article in articles:
        url = article.get("url")
        if not url:
            logger.debug("skipping article with no url: %r", article.get("title"))
            continue
        try:
            with transaction.atomic():
                _, created = RawArticle.objects.get_or_create(
                    url=url,
                    defaults={
                        "source": article.get("source", ""),
                        "title": article.get("title", "")[:500],
                        "raw_text": article.get("raw_text", ""),
                    },
                )
        except (IntegrityError, ValueError) as exc:
            logger.warning("could not store article %s: %s", url, exc)
            continue
        if created:
            created_count += 1
    return created_count

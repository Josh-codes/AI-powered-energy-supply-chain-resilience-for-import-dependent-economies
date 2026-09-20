"""LLM event extraction: RawArticle -> ExtractedEvent.

Each unprocessed article is sent to OpenAI once and the JSON answer is coerced
into the ExtractedEvent shape. Nothing here raises: a dead API or a nonsense
answer costs one article, never the pipeline run.

The two failure modes are treated differently on purpose:

  * the call itself failed (network, rate limit, auth) — transient, so the
    article is left ``processed=False`` and retried on the next cycle;
  * the model answered but the answer was unusable — deterministic, so the
    article is marked processed rather than paid for again every cycle.
"""
import json
import logging
import re

from django.conf import settings

from core.models import Corridor, ExtractedEvent, RawArticle
from pipeline.extract.prompt import CORRIDOR_VALUES, EVENT_TYPE_VALUES, build_prompt
from pipeline.ingest.gdelt import parse_seendate

logger = logging.getLogger(__name__)

# Suez is not a corridor in this model (folded into Red Sea in Phase 1), but it
# is still listed in the vestigial ExtractedEvent.CORRIDOR_CHOICES and is the
# obvious thing for a model to answer when an article is about the canal.
CORRIDOR_ALIASES = {
    "suez": "Red Sea",
    "suez canal": "Red Sea",
    "bab-el-mandeb": "Red Sea",
    "bab el mandeb": "Red Sea",
    "gulf of aden": "Red Sea",
    "strait of hormuz": "Hormuz",
    "persian gulf": "Hormuz",
    "cape of good hope": "Cape",
    "none": "None",
    "null": "None",
    "": "None",
}

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _get_client():
    """Build the LLM client. Imported lazily so the package is only needed when
    extraction actually runs (tests mock this function).

    Calls go to OpenRouter, which implements the OpenAI wire protocol — hence
    the openai SDK with a swapped base_url rather than hand-rolled requests.
    """
    from openai import OpenAI

    if not settings.LLM_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set — see .env")
    return OpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)


def _call_llm(prompt):
    """Send one prompt to the model and return the raw response text.

    Returns None on any failure, which the caller reads as "transient, retry
    later" rather than "this article is unextractable".
    """
    try:
        response = _get_client().chat.completions.create(
            model=settings.LLM_MODEL,
            max_tokens=settings.LLM_MAX_TOKENS,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            # OpenRouter-specific, so it rides in extra_body rather than as a
            # named SDK argument.
            extra_body={"reasoning": {"enabled": settings.LLM_REASONING}},
        )
        return response.choices[0].message.content
    except Exception:
        logger.exception("LLM extraction call failed")
        return None


def _clamp(value, low, high):
    return max(low, min(high, value))


def parse_extraction(text):
    """Coerce a model response into the ExtractedEvent field shape.

    Returns None only when no JSON object can be recovered at all; anything
    parseable is clamped into range rather than rejected, since a severity of 7
    is still a usable signal once capped.
    """
    if not text:
        return None

    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        match = _JSON_BLOCK.search(text)
        if not match:
            logger.warning("no JSON object found in extraction response: %r", text[:200])
            return None
        try:
            payload = json.loads(match.group(0))
        except ValueError:
            logger.warning("extraction response was not valid JSON: %r", text[:200])
            return None

    if not isinstance(payload, dict):
        logger.warning("extraction response was not a JSON object: %r", text[:200])
        return None

    corridor = str(payload.get("corridor") or "None").strip()
    corridor = CORRIDOR_ALIASES.get(corridor.lower(), corridor)
    if corridor not in CORRIDOR_VALUES:
        logger.debug("unrecognized corridor %r, treating as None", corridor)
        corridor = "None"

    event_type = str(payload.get("event_type") or "other").strip().lower()
    if event_type not in EVENT_TYPE_VALUES:
        event_type = "other"

    try:
        severity = _clamp(int(float(payload.get("severity", 1))), 1, 5)
    except (TypeError, ValueError):
        severity = 1
    try:
        confidence = _clamp(float(payload.get("confidence", 0.1)), 0.0, 1.0)
    except (TypeError, ValueError):
        confidence = 0.1

    return {
        "corridor": corridor,
        "actor": str(payload.get("actor") or "unknown")[:200],
        "event_type": event_type,
        "severity": severity,
        "confidence": confidence,
        "is_relevant": bool(payload.get("is_relevant", False)),
    }


def extract_event(article):
    """Run one RawArticle through the model and return the parsed dict.

    Returns None if the call failed or the answer was unusable — callers that
    need to tell those apart (see ``extract_pending_events``) should drive
    ``_call_llm`` and ``parse_extraction`` themselves.
    """
    text = _call_llm(build_prompt(article.raw_text or article.title))
    return None if text is None else parse_extraction(text)


def extract_pending_events(limit=None):
    """Extract every unprocessed RawArticle and store the relevant ones.

    Returns {"articles": n, "events": n, "irrelevant": n, "unparseable": n,
    "call_failed": n}. ``limit`` caps how many articles are sent to the API in
    one run; None means all of them.
    """
    counts = {"articles": 0, "events": 0, "irrelevant": 0, "unparseable": 0, "call_failed": 0}

    queryset = RawArticle.objects.filter(processed=False).order_by("ingested_at")
    if limit is not None:
        queryset = queryset[:limit]
    articles = list(queryset)
    if not articles:
        logger.info("no unprocessed articles to extract")
        return counts

    corridors = {c.name: c for c in Corridor.objects.all()}

    for article in articles:
        counts["articles"] += 1
        try:
            text = _call_llm(build_prompt(article.raw_text or article.title))
        except Exception:
            logger.exception("extraction crashed for %s", article.url)
            counts["call_failed"] += 1
            continue

        if text is None:
            # The call itself failed — leave the article unprocessed so the next
            # cycle retries it.
            counts["call_failed"] += 1
            continue

        result = parse_extraction(text)
        if result is None:
            # The model answered with something unusable. Re-asking would cost
            # another call for the same garbage, so consider the article done.
            counts["unparseable"] += 1
        elif not result["is_relevant"]:
            counts["irrelevant"] += 1
        else:
            ExtractedEvent.objects.create(
                corridor=corridors.get(result["corridor"]),
                actor=result["actor"],
                event_type=result["event_type"],
                severity=result["severity"],
                confidence=result["confidence"],
                # GDELT's seendate when we have it, ingest time otherwise.
                # Ingest time alone is NOT a safe proxy: GDELT returns articles
                # it first saw days earlier (up to 4 on the stored corpus), and
                # under the scorer's 0.1/day decay a 3-day error over-weights an
                # article by ~35%. RSS rows carry no date yet, so they still
                # fall back — acceptable because feeds only list recent items.
                timestamp=parse_seendate(article.raw_text) or article.ingested_at,
                article_url=article.url,
                # Needed to spot syndicated duplicates at scoring time, once the
                # RawArticle this came from has been cleaned up.
                title=article.title,
            )
            counts["events"] += 1

        article.processed = True
        article.save(update_fields=["processed"])

    logger.info(
        "extraction: %d articles -> %d events (%d irrelevant, %d unparseable, %d call failures)",
        counts["articles"], counts["events"], counts["irrelevant"],
        counts["unparseable"], counts["call_failed"],
    )
    return counts

"""Corridor risk scoring: ExtractedEvent -> RiskScore.

The raw score is CLAUDE.md's time-decay sum over a corridor's events:

    raw = Σ  severity × confidence × exp(-λ × Δt_days)      (λ = 0.1 / day)

...summed over *stories*, not articles. Syndicated coverage means one real event
arrives as a dozen near-identical articles with distinct URLs; counting each
would score press attention rather than risk. See STORY_SIMILARITY below.

Normalizing that to 0-1 is done with a saturating transform, deliberately NOT
min-max across the three corridors:

    score = baseline + (1 - baseline) × (1 - exp(-raw / SATURATION_K))

Min-max would make the score purely relative — in any week, however calm, the
noisiest corridor reads 1.0 and the quietest 0.0, and the ``risk > 0.50``
threshold trigger would fire on ordinary news volume rather than on real
disruption. It is also undefined when every corridor scores the same (including
the all-zero cold-start case). The transform above is absolute instead: it is
monotonic in raw, sits exactly at the corridor's own ``baseline_risk`` when no
events exist, and asymptotes towards 1.0.

SATURATION_K sets how much accumulated event weight counts as "elevated". It is
a calibration constant, not a measured one, and needs fitting against observed
event volume in Phase 7's backtest. ``RiskScore.raw_score`` persists the
untransformed sum precisely so refitting never means paying for extraction again.
"""
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from core.models import Corridor, ExtractedEvent, RiskScore

logger = logging.getLogger(__name__)

LAMBDA_DECAY = 0.1
SATURATION_K = 25.0

# Events older than this contribute e^(-0.1*180) ≈ 1.5e-8 — nothing. Cutting
# them also bounds the O(n²) story clustering below, which would otherwise grow
# without limit against a permanent event table.
DECAY_LOOKBACK_DAYS = 180

# A single wire story is republished by many outlets under different URLs, so
# Phase 2's URL dedup never sees them as duplicates and each becomes its own
# event. Left alone that measures press syndication, not risk: in the first real
# corpus one Red Sea story was counted 10 times and another 8, inflating Red
# Sea's raw score 2.4x against Hormuz's 1.3x — enough to flip which corridor
# ranked as most at risk. Events are therefore grouped into stories and each
# story contributes only its strongest member.
STORY_SIMILARITY = 0.60
STORY_WINDOW_DAYS = 3

_PUNCT = re.compile(r"[^a-z0-9 ]+")


def _normalize_title(title):
    return " ".join(_PUNCT.sub(" ", (title or "").lower()).split())


def _same_story(a_title, a_time, b_title, b_time):
    """True if two events look like coverage of one real-world story.

    Untitled events are never merged — an empty title carries no evidence of
    duplication, and silently collapsing them would suppress real signal.
    """
    if not a_title or not b_title:
        return False
    if abs((a_time - b_time).total_seconds()) > STORY_WINDOW_DAYS * 86400:
        return False
    matcher = SequenceMatcher(None, a_title, b_title)
    # Cheap upper bounds first; both are O(1)/O(n) and skip most pairs.
    if matcher.real_quick_ratio() < STORY_SIMILARITY:
        return False
    if matcher.quick_ratio() < STORY_SIMILARITY:
        return False
    return matcher.ratio() >= STORY_SIMILARITY


def compute_risk_score(corridor_name, lambda_decay=LAMBDA_DECAY, now=None,
                       deduplicate=True):
    """Return the unnormalized, time-decayed event weight for one corridor.

    With ``deduplicate`` (the default) each cluster of near-identical headlines
    counts once, at its strongest member. Pass False for the raw per-article sum
    — useful for showing the syndication effect, not for scoring.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=DECAY_LOOKBACK_DAYS)

    events = list(
        ExtractedEvent.objects.filter(
            corridor__name=corridor_name, timestamp__gte=cutoff
        ).values_list("severity", "confidence", "timestamp", "title")
    )

    def weight(severity, confidence, timestamp):
        # Clamped at 0 so a clock skew that puts an event slightly in the future
        # cannot amplify it above its own severity.
        delta_days = max(0.0, (now - timestamp).total_seconds() / 86400.0)
        return severity * confidence * math.exp(-lambda_decay * delta_days)

    if not deduplicate:
        return sum(weight(s, c, t) for s, c, t, _ in events)

    # Each cluster keeps the single largest contribution among its members.
    clusters = []  # [(normalized_title, timestamp, best_weight)]
    for severity, confidence, timestamp, title in events:
        norm = _normalize_title(title)
        w = weight(severity, confidence, timestamp)
        for i, (c_title, c_time, c_weight) in enumerate(clusters):
            if _same_story(norm, timestamp, c_title, c_time):
                if w > c_weight:
                    clusters[i] = (c_title, c_time, w)
                break
        else:
            clusters.append((norm, timestamp, w))

    if len(clusters) < len(events):
        logger.debug(
            "%s: %d events collapsed to %d stories", corridor_name, len(events), len(clusters)
        )
    return sum(weight for _, _, weight in clusters)


def normalize_score(raw_score, baseline_risk=0.0, k=SATURATION_K):
    """Map a raw score onto [baseline_risk, 1.0) — see the module docstring."""
    if raw_score <= 0:
        return baseline_risk
    return baseline_risk + (1.0 - baseline_risk) * (1.0 - math.exp(-raw_score / k))


def compute_all_risk_scores(persist=True, now=None):
    """Score every corridor and return {corridor_name: normalized_score}.

    When ``persist`` is set, a RiskScore row is written per corridor (permanent
    history, used by the backtest and the trend charts) and each Corridor's
    ``live_risk_score`` is refreshed so the next graph build starts current.
    """
    now = now or datetime.now(timezone.utc)
    scores = {}

    for corridor in Corridor.objects.all():
        raw = compute_risk_score(corridor.name, now=now)
        score = normalize_score(raw, baseline_risk=corridor.baseline_risk)
        scores[corridor.name] = score

        if persist:
            RiskScore.objects.create(corridor=corridor, score=score, raw_score=raw)
            corridor.live_risk_score = score
            corridor.save(update_fields=["live_risk_score"])

        logger.info("risk score %s: raw=%.3f normalized=%.3f", corridor.name, raw, score)

    return scores

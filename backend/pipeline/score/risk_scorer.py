"""Corridor risk scoring: ExtractedEvent -> RiskScore.

Every event carries a time-decayed weight, CLAUDE.md's formula unchanged:

    weight = severity × confidence × exp(-λ × Δt_days)      (λ = 0.1 / day)

Weights are computed over *stories*, not articles. Syndicated coverage means one
real event arrives as a dozen near-identical articles with distinct URLs;
counting each would score press attention rather than risk. See STORY_SIMILARITY.

THE CORRIDOR SCORE IS THE ZERO-PADDED MEAN OF THE THREE STRONGEST STORIES
------------------------------------------------------------------------
    raw   = (sum of the 3 largest story weights) / 3
    score = baseline_risk + (1 - baseline_risk) × min(1, raw / MAX_EVENT_WEIGHT)

This replaced a *sum* over all stories on 2026-09-26. The sum was unbounded in
story count, so it measured how deeply the corpus had been sampled as much as
how dangerous the world was. Measured on the real corpus: the same crisis scored
Hormuz 16.6 from the GDELT DOC path and 44.1 from GKG, and 75.3 from both
together — a 1.71x jump from ingesting more articles about one unchanged week,
which was enough to invert the corridor ranking. Both corridors then normalized
to ~0.99, and Phase 4's risk-weighted capacity loss for Hormuz collapsed to
0.087 mb/d on a corridor carrying 2.316 mb/d of India's inflow. Under the
top-3 statistic that same comparison is 1.00x: adding a whole second ingestion
path does not move the score at all, because it does not find anything worse.

Two consequences worth stating:

* **There is no free calibration constant left.** The statistic is bounded by
  MAX_EVENT_WEIGHT, so ``score = raw / 5`` is a definition, not a fit: 1.0 means
  "three severity-5, confidence-1.0 stories today", i.e. a corridor reported
  closed. ``SATURATION_K`` is no longer on the production path.
* **Zero-padding is deliberate.** Dividing by 3 even when fewer than 3 stories
  exist means one moderate headline cannot read like a sustained campaign of
  them. It is the difference between "something happened" and "this is ongoing".

Normalization is absolute, not min-max across corridors. Min-max would make the
score purely relative — in any week, however calm, the noisiest corridor reads
1.0 and the quietest 0.0, the ``risk > 0.50`` threshold trigger would fire on
ordinary news volume, and it is undefined when every corridor scores the same
(including the all-zero cold start). Here a corridor with no events sits exactly
at its own ``baseline_risk``.

``compute_risk_score`` (the old unbounded sum) is kept as a diagnostic — it is
what quantifies the syndication effect, and Phase 7 may want to report it — but
nothing in the live pipeline scores from it. ``pipeline/score/candidates.py``
plus ``manage.py compare_scoring`` hold the full comparison this choice came out
of; ``RiskScore.raw_score`` persists the untransformed statistic so the choice
can be revisited without paying for extraction again.
"""
import heapq
import logging
import math
import re
from collections import defaultdict, namedtuple
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from core.models import Corridor, ExtractedEvent, RiskScore

logger = logging.getLogger(__name__)

LAMBDA_DECAY = 0.1

#: How many of a corridor's strongest stories the score averages over. 3 is the
#: smallest window that distinguishes a one-off from a campaign while still
#: being driven by the serious end of the corpus rather than its bulk. Measured
#: against the real corpus, k = 1/3/5 all gave a volume-sensitivity ratio of
#: exactly 1.00; k = 3 gave the widest Hormuz/Red Sea separation (0.078).
TOP_K_STORIES = 3

#: Retained for ``normalize_score`` (the superseded saturating transform) and
#: for the comparison harness. NOT used by the production scoring path — see the
#: module docstring for why a bounded statistic removed the need to fit it.
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

#: One real-world story, after near-duplicate coverage has been collapsed.
#: ``weight``/``severity``/``confidence``/``timestamp`` come from the cluster's
#: single strongest member; ``title`` and ``members`` describe the cluster.
Story = namedtuple("Story", "weight severity confidence timestamp title members")

#: The largest weight a single event can contribute: severity 5 x confidence
#: 1.0 x no decay. Candidate formulas that are bounded in severity units (see
#: pipeline/score/candidates.py) divide by this.
MAX_EVENT_WEIGHT = 5.0

#: RiskScore rows computed at or after this instant hold the bounded top-k
#: statistic in ``raw_score``; earlier rows hold the superseded unbounded sum.
#: Read off the dev DB: last summed rows 2026-09-20 15:11 UTC (ids <= 33),
#: first top-k rows 2026-09-26 06:26 UTC (id 34). Trend charts must not put
#: the two on one axis.
TOP_K_FORMULA_SINCE = datetime(2026, 9, 26, tzinfo=timezone.utc)


def event_weight(severity, confidence, timestamp, now, lambda_decay=LAMBDA_DECAY):
    """severity x confidence x exp(-lambda*days). Clamped at 0 days so a clock
    skew that puts an event slightly in the future cannot amplify it above its
    own severity."""
    delta_days = max(0.0, (now - timestamp).total_seconds() / 86400.0)
    return severity * confidence * math.exp(-lambda_decay * delta_days)


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


def corridor_events(corridor_name, now=None, lookback_days=DECAY_LOOKBACK_DAYS,
                    exclude_future=False):
    """``(severity, confidence, timestamp, title)`` tuples inside the decay
    window, in the shape :func:`cluster_stories` and the candidate formulas in
    ``pipeline/score/candidates.py`` both expect.

    ``exclude_future`` makes the query POINT-IN-TIME: events dated after
    ``now`` are dropped. The backtest must pass it. Without it, scoring a past
    date admits every later event, and :func:`event_weight`'s clamp then gives
    each one FULL undecayed weight — scoring 2026-02-11 would read the whole
    March crisis and September's corpus as if they had just happened.

    It defaults off because live scoring legitimately sees slightly-future
    events: 1 of the first 1,642 was dated 2 hours after its own extraction
    (a publisher timestamp error), which is exactly what the clamp absorbs.
    Turning the filter on globally would silently change live scores.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    rows = ExtractedEvent.objects.filter(
        corridor__name=corridor_name, timestamp__gte=cutoff
    )
    if exclude_future:
        rows = rows.filter(timestamp__lte=now)
    # Date order, not whatever order PostgreSQL returns rows in. Clustering is
    # greedy (an event joins the FIRST matching story), so the same events in
    # a different order can cluster differently: on the live corpus of
    # 2026-09-28, storage order vs date order changed the story count by one
    # per corridor (Hormuz 224 vs 223, Red Sea 88 vs 89), though the top-3
    # statistic was bit-identical. Storage order is not stable across a
    # dump/restore, so without this a restored database could score
    # differently. Date order is also what lets the backtest snapshot one
    # incremental pass per day (see StoryClusterer).
    rows = rows.order_by("timestamp", "id")
    return list(rows.values_list("severity", "confidence", "timestamp", "title"))


def cluster_stories(events, now=None, lambda_decay=LAMBDA_DECAY):
    """Collapse near-identical coverage into one :class:`Story` per real event.

    ``events`` is an iterable of ``(severity, confidence, timestamp, title)``.
    Each returned Story carries its cluster's single strongest member — the
    cluster's *representative* title and time stay those of the first member
    seen, which is what every later membership test compares against.

    Exposed (rather than inlined into :func:`compute_risk_score`) so alternative
    scoring formulas measure the same stories the production formula does; a
    second clustering implementation would be free to drift from this one. The
    backtest does not have a second one either: it drives the same
    :class:`StoryClusterer` incrementally.
    """
    clusterer = StoryClusterer(now=now, lambda_decay=lambda_decay)
    for severity, confidence, timestamp, title in events:
        clusterer.add(severity, confidence, timestamp, title)
    return clusterer.stories()


class _Rep:
    """A cluster's fixed representative, with everything precomputed that the
    per-comparison tests would otherwise rebuild."""
    __slots__ = ("time", "length", "masks", "full", "matcher")

    def __init__(self, time, norm):
        self.time = time
        self.length = len(norm)
        masks = {}
        for i, ch in enumerate(norm):
            masks[ch] = masks.get(ch, 0) | (1 << i)
        self.masks = masks
        self.full = (1 << len(norm)) - 1
        # difflib caches its analysis of seq2, so the rep goes there once and
        # only seq1 is swapped per comparison. Same arguments as the original
        # SequenceMatcher(None, event_title, rep_title), so same autojunk.
        self.matcher = SequenceMatcher(None, "", norm)


def _lcs_length(a, rep):
    """Longest common subsequence of *a* and the rep's title, bit-parallel
    (Hyyro 2004): one pass over *a*, a few integer operations per character."""
    v = rep.full
    masks = rep.masks
    for ch in a:
        u = v & masks.get(ch, 0)
        v = ((v + u) | (v - u)) & rep.full
    return rep.length - v.bit_count()


class StoryClusterer:
    """Incremental story clustering: :func:`cluster_stories` one event at a time.

    Makes exactly the decisions of the original loop — every event tested
    against every existing cluster in creation order with :func:`_same_story`,
    joining the first that matches — which ``tests/test_scoring.py`` keeps as a
    reference implementation and compares against on random input. That loop
    took 112 s for the 2,652 Hormuz events of 2026-03-05 and grew
    quadratically; the backtest needed 14,483. Four savings, none of which can
    change a decision:

    * **Day index.** Only a cluster whose rep lies within STORY_WINDOW_DAYS can
      match, so clusters are bucketed by day and only nearby buckets are
      scanned, merged back into creation order.
    * **LCS bound.** difflib's matching blocks appear in the same order in both
      strings, so together they form a common subsequence: ``ratio() <= 2 *
      LCS / (len(a) + len(b))``. A pair failing that bound cannot match.
      Measured on 90,109 real in-window headline pairs: 0 violations, 70% of
      pairs rejected at 25 us instead of difflib's 157 us. It stands in for
      ``quick_ratio()``, which it dominates (LCS <= character multiset
      overlap) and which rejected only 1.6% of the same pairs, since any two
      English headlines of similar length share most letters.
    * **Cached rep.** A rep never changes, so its SequenceMatcher and LCS masks
      are built once.
    * **Repeat headline.** While events arrive in date order, a headline seen
      before whose story is still within the window joins that story directly.
      Exact: every older cluster still in the window was already tested
      against this very headline and failed, and ratio is deterministic. Out of
      date order the shortcut disables itself for the rest of the run.

    ``now`` fixes the weights used to pick each cluster's strongest member. For
    events not after ``now`` that choice does not depend on ``now`` (every
    weight shares the factor exp(-lambda * now)), which is what lets the
    backtest cluster once and re-weight each day's snapshot.
    """

    def __init__(self, now=None, lambda_decay=LAMBDA_DECAY):
        self.now = now or datetime.now(timezone.utc)
        self.lambda_decay = lambda_decay
        self._bests = []                  # best Story per cluster, creation order
        self._reps = []                   # _Rep per cluster; None if untitled
        self._by_day = defaultdict(list)  # day bucket -> ascending cluster indices
        self._latest = {}                 # headline -> cluster its last copy joined
        self._last_time = None
        self._in_order = True

    def __len__(self):
        return len(self._bests)

    def stories(self):
        return list(self._bests)

    def add(self, severity, confidence, timestamp, title):
        norm = _normalize_title(title)
        story = Story(
            weight=event_weight(severity, confidence, timestamp, self.now, self.lambda_decay),
            severity=severity,
            confidence=confidence,
            timestamp=timestamp,
            title=title,
            members=1,
        )
        if self._last_time is not None and timestamp < self._last_time:
            self._in_order = False
            self._latest.clear()
        if self._last_time is None or timestamp > self._last_time:
            self._last_time = timestamp

        match = self._find(norm, timestamp) if norm else None
        if match is not None:
            best = self._bests[match]
            winner = story if story.weight > best.weight else best
            self._bests[match] = winner._replace(members=best.members + 1)
        else:
            match = len(self._bests)
            self._bests.append(story)
            if norm:
                self._reps.append(_Rep(timestamp, norm))
                self._by_day[_day_bucket(timestamp)].append(match)
            else:
                self._reps.append(None)  # an untitled rep can never be matched
        if norm and self._in_order:
            self._latest[norm] = match

    def _find(self, norm, timestamp):
        window = STORY_WINDOW_DAYS * 86400
        if self._in_order:
            seen = self._latest.get(norm)
            if seen is not None and abs(
                (timestamp - self._reps[seen].time).total_seconds()
            ) <= window:
                return seen

        length = len(norm)
        day = _day_bucket(timestamp)
        reach = STORY_WINDOW_DAYS + 1
        nearby = [self._by_day[d] for d in range(day - reach, day + reach + 1)
                  if d in self._by_day]
        for idx in heapq.merge(*nearby):
            rep = self._reps[idx]
            if abs((timestamp - rep.time).total_seconds()) > window:
                continue
            total = length + rep.length
            # == SequenceMatcher.real_quick_ratio(), without building anything
            if 2.0 * min(length, rep.length) / total < STORY_SIMILARITY:
                continue
            if 2.0 * _lcs_length(norm, rep) / total < STORY_SIMILARITY:
                continue
            rep.matcher.set_seq1(norm)
            if rep.matcher.ratio() >= STORY_SIMILARITY:
                return idx
        return None


def _day_bucket(moment):
    return math.floor(moment.timestamp() / 86400)


def compute_risk_score(corridor_name, lambda_decay=LAMBDA_DECAY, now=None,
                       deduplicate=True):
    """Return the unnormalized, time-decayed event weight for one corridor.

    With ``deduplicate`` (the default) each cluster of near-identical headlines
    counts once, at its strongest member. Pass False for the raw per-article sum
    — useful for showing the syndication effect, not for scoring.
    """
    now = now or datetime.now(timezone.utc)
    events = corridor_events(corridor_name, now=now)

    if not deduplicate:
        return sum(
            event_weight(s, c, t, now, lambda_decay) for s, c, t, _ in events
        )

    stories = cluster_stories(events, now=now, lambda_decay=lambda_decay)
    if len(stories) < len(events):
        logger.debug(
            "%s: %d events collapsed to %d stories", corridor_name, len(events), len(stories)
        )
    return sum(story.weight for story in stories)


def top_k_severity(stories, k=TOP_K_STORIES):
    """The production statistic: mean weight of a corridor's ``k`` strongest
    stories, **zero-padded** to ``k``.

    Padding is the whole point of the divisor being ``k`` rather than
    ``len(top)``: one severity-5 story alone scores 5/3, while three of them
    score 5 — so an isolated headline cannot read like a sustained campaign.

    Bounded above by :data:`MAX_EVENT_WEIGHT`, and — unlike a sum — unchanged by
    ingesting more coverage that is not worse than what is already there. That
    invariance is the defect fix; see the module docstring.
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    top = sorted((s.weight for s in stories), reverse=True)[:k]
    return sum(top) / k if top else 0.0


def normalize_severity(raw_score, baseline_risk=0.0):
    """Map the bounded statistic onto [baseline_risk, 1.0].

    A definition rather than a calibration: 1.0 is ``TOP_K_STORIES`` stories at
    severity 5 and confidence 1.0, dated today — a corridor reported closed.
    Clamped because :data:`MAX_EVENT_WEIGHT` is the per-event ceiling, and a
    score above 1.0 would make ``effective_capacity`` negative, which
    ``nx.maximum_flow`` raises on rather than treating as a closed corridor.
    """
    if raw_score <= 0:
        return baseline_risk
    return baseline_risk + (1.0 - baseline_risk) * min(1.0, raw_score / MAX_EVENT_WEIGHT)


def normalize_score(raw_score, baseline_risk=0.0, k=SATURATION_K):
    """SUPERSEDED by :func:`normalize_severity`; kept for the comparison harness
    and for re-reading historical ``RiskScore.raw_score`` rows written before
    2026-09-26, which hold unbounded sums this transform was shaped for.

    Saturating rather than linear because its input is unbounded. ``k`` is the
    uncalibrated constant whose removal motivated the switch.
    """
    if raw_score <= 0:
        return baseline_risk
    return baseline_risk + (1.0 - baseline_risk) * (1.0 - math.exp(-raw_score / k))


def compute_corridor_severity(corridor_name, now=None, lambda_decay=LAMBDA_DECAY,
                              k=TOP_K_STORIES, exclude_future=False):
    """The corridor's production raw score, end to end: events -> stories ->
    zero-padded top-k mean weight. Bounded by :data:`MAX_EVENT_WEIGHT`.

    Pass ``exclude_future=True`` whenever ``now`` is in the past; see
    :func:`corridor_events`.
    """
    now = now or datetime.now(timezone.utc)
    events = corridor_events(corridor_name, now=now, exclude_future=exclude_future)
    stories = cluster_stories(events, now=now, lambda_decay=lambda_decay)
    return top_k_severity(stories, k=k)


def compute_all_risk_scores(persist=True, now=None):
    """Score every corridor and return {corridor_name: normalized_score}.

    When ``persist`` is set, a RiskScore row is written per corridor (permanent
    history, used by the backtest and the trend charts) and each Corridor's
    ``live_risk_score`` is refreshed so the next graph build starts current.

    ``RiskScore.raw_score`` holds the bounded top-k statistic. Rows written
    before 2026-09-26 hold the superseded unbounded sum instead, so a trend
    chart or backtest spanning that date must not compare the two directly.
    """
    now = now or datetime.now(timezone.utc)
    scores = {}

    for corridor in Corridor.objects.all():
        raw = compute_corridor_severity(corridor.name, now=now)
        score = normalize_severity(raw, baseline_risk=corridor.baseline_risk)
        scores[corridor.name] = score

        if persist:
            RiskScore.objects.create(corridor=corridor, score=score, raw_score=raw)
            corridor.live_risk_score = score
            corridor.save(update_fields=["live_risk_score"])

        logger.info("risk score %s: raw=%.3f normalized=%.3f", corridor.name, raw, score)

    return scores

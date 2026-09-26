"""Candidate raw-score statistics, for comparing scoring formulas on one corpus.

The production formula in ``risk_scorer.py`` sums a decayed weight over every
story a corridor has. A sum is unbounded in *count*, so it measures how deeply
the corpus was sampled as much as how dangerous the world is: the same crisis
scored raw 39.4 (Hormuz) from the GDELT DOC API alone and 115.6 once GKG bulk
ingestion roughly doubled the article intake, and the corridor ranking inverted
in the process. That is the defect these alternatives exist to test against.

Each candidate takes the *same* list of :class:`risk_scorer.Story` objects — so
story clustering, time decay and the severity/confidence rubric are held
constant — and returns one unnormalized statistic. Normalization into [0, 1] is
a separate, later step (:func:`risk_scorer.normalize_score`), because the
constant in it is what Phase 7's backtest has to fit; mixing the two is what
made "just tune K" look like a fix.

The precedent for the share-based candidates is Caldara & Iacoviello's
Geopolitical Risk index (Fed IFDP 1222), which counts risk-related articles
"as a share of the total number of news articles" for exactly this reason, and
Baker/Bloom/Davis's Economic Policy Uncertainty index, which scales counts by
each paper's total article volume.

Nothing here reads the database or writes anything; the harness
(``manage.py compare_scoring``) supplies the stories and the denominators.
"""
import math
from collections import namedtuple

from pipeline.score.risk_scorer import MAX_EVENT_WEIGHT, SATURATION_K, top_k_severity

#: Free constant for the log-compressed candidate. Arbitrary, like K — log
#: compression is included to *demonstrate* that it does not address the
#: count-vs-severity conflation, not as a serious contender.
LOG_K = 2.0

#: Free constant for the share candidates, in units of "risk weight per 100
#: articles". Also arbitrary; the share's value is that it is volume-insensitive,
#: not that this number is right.
SHARE_K = 25.0

#: Denominators are reported per 100 articles so the statistic reads in a human
#: range rather than as 0.0019.
SHARE_PER = 100.0


Candidate = namedtuple("Candidate", "key label unit needs_denominator statistic normalize note")


# --------------------------------------------------------------------------
# raw statistics
# --------------------------------------------------------------------------

def stat_sum(stories, denominator=None):
    """The production statistic: total decayed weight across all stories."""
    return sum(s.weight for s in stories)


def stat_log_sum(stories, denominator=None):
    """log(1 + sum). Compresses the range but is still a function of the count:
    70 minor stories and 17 catastrophic ones both land near 4.44."""
    return math.log1p(stat_sum(stories))


def stat_mean(stories, denominator=None):
    """Decay-weighted mean over stories. Bounded and volume-insensitive, but it
    *dilutes*: ingesting one more trivial story LOWERS the corridor's risk."""
    if not stories:
        return 0.0
    return stat_sum(stories) / len(stories)


def _top(stories, k, pad):
    """Mean of the k strongest stories. ``pad`` divides by k even when fewer
    than k stories exist, so a single severe headline cannot read the same as a
    sustained campaign of them.

    The padded branch delegates to the production implementation rather than
    repeating it, so the harness's ``top3pad`` column is by construction the
    statistic the live pipeline scores from.
    """
    if pad:
        return top_k_severity(stories, k)
    top = sorted((s.weight for s in stories), reverse=True)[:k]
    if not top:
        return 0.0
    return sum(top) / len(top)


def stat_top1(stories, denominator=None):
    """The single worst story. Bounded at MAX_EVENT_WEIGHT, so the normalizing
    constant becomes interpretable in severity units instead of arbitrary."""
    return _top(stories, 1, pad=False)


def stat_top3(stories, denominator=None):
    return _top(stories, 3, pad=False)


def stat_top3_padded(stories, denominator=None):
    return _top(stories, 3, pad=True)


def stat_top5_padded(stories, denominator=None):
    return _top(stories, 5, pad=True)


def stat_share(stories, denominator=None):
    """GPR-style: total decayed weight per 100 articles ingested in the window.

    Volume-insensitive by construction — doubling the intake roughly doubles
    both numerator and denominator. The denominator must be corridor-AGNOSTIC
    (the same article pool for all three corridors). Using each corridor's own
    matched-article count instead would make the statistic relative, which is
    the min-max failure ``risk_scorer`` already rejects: the quietest corridor
    would read the same in a calm week as in a crisis.
    """
    if not denominator:
        return 0.0
    return SHARE_PER * stat_sum(stories) / denominator


# --------------------------------------------------------------------------
# normalizers: raw statistic -> [baseline_risk, 1.0)
# --------------------------------------------------------------------------

def saturating(k):
    def normalize(raw, baseline_risk):
        if raw <= 0:
            return baseline_risk
        return baseline_risk + (1.0 - baseline_risk) * (1.0 - math.exp(-raw / k))
    return normalize


def linear_in_severity(raw, baseline_risk):
    """For the bounded statistics. 1.0 means "a severity-5, confidence-1.0 story
    today" — i.e. a corridor reported closed — with no free constant at all."""
    if raw <= 0:
        return baseline_risk
    return baseline_risk + (1.0 - baseline_risk) * min(1.0, raw / MAX_EVENT_WEIGHT)


CANDIDATES = [
    Candidate(
        "sum_sat", "sum + saturating (SUPERSEDED)", "weight", False,
        stat_sum, saturating(SATURATION_K),
        f"production until 2026-09-26, K={SATURATION_K:g}; unbounded in story count",
    ),
    Candidate(
        "log_sat", "log(1+sum) + saturating", "log-weight", False,
        stat_log_sum, saturating(LOG_K),
        f"K={LOG_K:g}; cures saturation only, not count-vs-severity",
    ),
    Candidate(
        "mean", "decay-weighted mean", "severity", False,
        stat_mean, linear_in_severity,
        "bounded, but dilutes - trivial news lowers the score",
    ),
    Candidate(
        "top1", "worst single story", "severity", False,
        stat_top1, linear_in_severity,
        "bounded, no free constant; blind to a sustained campaign",
    ),
    Candidate(
        "top3", "mean of top 3 stories", "severity", False,
        stat_top3, linear_in_severity,
        "bounded; mean over however many exist (<3 allowed)",
    ),
    Candidate(
        "top3pad", "mean of top 3, zero-padded (PRODUCTION)", "severity", False,
        stat_top3_padded, linear_in_severity,
        "bounded, no free constant; 1 severe story scores 1/3 of 3 severe ones",
    ),
    Candidate(
        "top5pad", "mean of top 5, zero-padded", "severity", False,
        stat_top5_padded, linear_in_severity,
        "bounded; needs a broader campaign to saturate",
    ),
    Candidate(
        "share", "GPR-style share per 100 articles", "weight/100art", True,
        stat_share, saturating(SHARE_K),
        f"K={SHARE_K:g}; denominator is articles INGESTED, a proxy - see harness notes",
    ),
]

CANDIDATES_BY_KEY = {c.key: c for c in CANDIDATES}

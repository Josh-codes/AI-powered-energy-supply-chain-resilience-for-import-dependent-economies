"""Tests for pipeline/score/candidates.py and the story-clustering seam it uses.

No database, no network: every candidate is a pure function over
``(severity, confidence, timestamp, title)`` tuples, which is the whole point —
the comparison harness has to be able to re-score a corpus without touching the
pipeline that produced it.

The load-bearing assertion here is
``VolumeSensitivityTests.test_current_formula_conflates_count_with_severity``:
it pins the defect the candidates exist to fix (70 minor stories and 17
catastrophic ones score the same under the production sum) so that a later
formula change cannot quietly reintroduce it.
"""
import random
import string
import unittest
from datetime import datetime, timedelta, timezone

from pipeline.score.candidates import (
    CANDIDATES,
    CANDIDATES_BY_KEY,
    linear_in_severity,
    saturating,
    stat_mean,
    stat_share,
    stat_sum,
    stat_top1,
    stat_top3_padded,
)
from pipeline.score.risk_scorer import (
    MAX_EVENT_WEIGHT,
    cluster_stories,
    event_weight,
)

NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def _event(severity=4, confidence=0.9, days_ago=0, title="a headline"):
    return (severity, confidence, NOW - timedelta(days=days_ago), title)


def _distinct(n, seed, severity=4, confidence=0.9, days_ago=0):
    """n events with titles too dissimilar to cluster. Word salad alone is not
    enough — sampling from a small vocabulary collides often enough to merge
    ~7% of pairs — so each title carries a random suffix."""
    rng = random.Random(seed)
    return [
        _event(severity, confidence, days_ago,
               "".join(rng.choices(string.ascii_lowercase + " ", k=40)))
        for _ in range(n)
    ]


class EventWeightTests(unittest.TestCase):
    def test_weight_is_severity_times_confidence_when_fresh(self):
        self.assertAlmostEqual(event_weight(4, 0.9, NOW, NOW), 3.6)

    def test_weight_decays_at_lambda_per_day(self):
        ten_days = event_weight(5, 1.0, NOW - timedelta(days=10), NOW)
        self.assertAlmostEqual(ten_days, 5.0 * 2.718281828 ** -1, places=4)

    def test_future_timestamp_cannot_amplify_beyond_severity(self):
        future = event_weight(5, 1.0, NOW + timedelta(days=30), NOW)
        self.assertAlmostEqual(future, 5.0)

    def test_max_event_weight_matches_the_rubric_ceiling(self):
        self.assertAlmostEqual(event_weight(5, 1.0, NOW, NOW), MAX_EVENT_WEIGHT)


class ClusterStoriesTests(unittest.TestCase):
    def test_syndicated_copies_collapse_to_one_story(self):
        events = [
            _event(3, 0.8, title="Houthis seize Bab al-Mandeb closing oil chokepoint"),
            _event(4, 0.9, title="Houthis seize Bab al-Mandeb, closing world oil chokepoint"),
        ]
        stories = cluster_stories(events, now=NOW)
        self.assertEqual(len(stories), 1)

    def test_cluster_keeps_its_strongest_member(self):
        events = [
            _event(3, 0.8, title="Houthis seize Bab al-Mandeb closing oil chokepoint"),
            _event(4, 0.9, title="Houthis seize Bab al-Mandeb, closing world oil chokepoint"),
        ]
        story = cluster_stories(events, now=NOW)[0]
        self.assertEqual((story.severity, story.confidence), (4, 0.9))
        self.assertEqual(story.members, 2)

    def test_unrelated_headlines_stay_separate(self):
        events = [
            _event(title="Houthis seize Bab al-Mandeb closing oil chokepoint"),
            _event(title="Tanker freight rates surge on Hormuz closure fears"),
        ]
        self.assertEqual(len(cluster_stories(events, now=NOW)), 2)

    def test_untitled_events_are_never_merged(self):
        events = [_event(title=""), _event(title="")]
        self.assertEqual(len(cluster_stories(events, now=NOW)), 2)

    def test_identical_headlines_outside_the_window_stay_separate(self):
        events = [
            _event(days_ago=0, title="Houthis seize Bab al-Mandeb closing oil chokepoint"),
            _event(days_ago=30, title="Houthis seize Bab al-Mandeb closing oil chokepoint"),
        ]
        self.assertEqual(len(cluster_stories(events, now=NOW)), 2)

    def test_empty_corpus_yields_no_stories(self):
        self.assertEqual(cluster_stories([], now=NOW), [])


class BoundednessTests(unittest.TestCase):
    """Whether each candidate's statistic can be driven arbitrarily high by
    ingesting more articles, which is what the production sum does."""

    def test_sum_is_unbounded_in_story_count(self):
        small = stat_sum(cluster_stories(_distinct(10, seed=1), now=NOW))
        large = stat_sum(cluster_stories(_distinct(100, seed=2), now=NOW))
        self.assertGreater(large, 5 * small)

    def test_bounded_candidates_cannot_exceed_one_severity_five_story(self):
        stories = cluster_stories(_distinct(200, seed=3, severity=5, confidence=1.0), now=NOW)
        for key in ("mean", "top1", "top3", "top3pad", "top5pad"):
            with self.subTest(key=key):
                raw = CANDIDATES_BY_KEY[key].statistic(stories, None)
                self.assertLessEqual(raw, MAX_EVENT_WEIGHT + 1e-9)

    def test_top1_is_exactly_the_worst_story(self):
        stories = cluster_stories(
            _distinct(5, seed=4, severity=2, confidence=0.5)
            + [_event(5, 1.0, title="a uniquely catastrophic closure headline")],
            now=NOW,
        )
        self.assertAlmostEqual(stat_top1(stories, None), MAX_EVENT_WEIGHT)

    def test_padded_top3_penalises_a_lone_severe_story(self):
        lone = cluster_stories([_event(5, 1.0, title="single closure headline")], now=NOW)
        campaign = cluster_stories(_distinct(3, seed=5, severity=5, confidence=1.0), now=NOW)
        self.assertAlmostEqual(stat_top3_padded(lone, None), MAX_EVENT_WEIGHT / 3)
        self.assertAlmostEqual(stat_top3_padded(campaign, None), MAX_EVENT_WEIGHT)


class VolumeSensitivityTests(unittest.TestCase):
    """The defect this module exists to fix, and proof each candidate does or
    does not have it."""

    def setUp(self):
        self.minor = cluster_stories(
            _distinct(70, seed=10, severity=2, confidence=0.6), now=NOW
        )
        self.severe = cluster_stories(
            _distinct(17, seed=11, severity=5, confidence=1.0), now=NOW
        )
        # Same kind of news, twice as deeply sampled.
        self.minor_2x = cluster_stories(
            _distinct(70, seed=10, severity=2, confidence=0.6)
            + _distinct(70, seed=12, severity=2, confidence=0.6),
            now=NOW,
        )
        self.assertEqual(len(self.minor), 70)
        self.assertEqual(len(self.minor_2x), 140)

    def test_current_formula_conflates_count_with_severity(self):
        """70 minor stories and 17 catastrophic ones are indistinguishable
        under the production sum — 84.0 against 85.0."""
        self.assertAlmostEqual(stat_sum(self.minor), 84.0, places=6)
        self.assertAlmostEqual(stat_sum(self.severe), 85.0, places=6)

    def test_current_formula_doubles_when_sampling_doubles(self):
        self.assertAlmostEqual(stat_sum(self.minor_2x) / stat_sum(self.minor), 2.0, places=6)

    def test_log_compression_does_not_separate_them_either(self):
        log_sum = CANDIDATES_BY_KEY["log_sat"].statistic
        self.assertAlmostEqual(log_sum(self.minor, None), log_sum(self.severe, None), places=1)

    def test_bounded_candidates_separate_minor_from_catastrophic(self):
        for key in ("mean", "top1", "top3", "top3pad", "top5pad"):
            with self.subTest(key=key):
                statistic = CANDIDATES_BY_KEY[key].statistic
                self.assertLess(
                    statistic(self.minor, None), statistic(self.severe, None)
                )

    def test_bounded_candidates_are_unmoved_by_doubling_the_sampling(self):
        for key in ("mean", "top1", "top3", "top3pad", "top5pad"):
            with self.subTest(key=key):
                statistic = CANDIDATES_BY_KEY[key].statistic
                self.assertAlmostEqual(
                    statistic(self.minor_2x, None), statistic(self.minor, None), places=6
                )

    def test_mean_dilutes_when_trivial_news_is_added(self):
        """The reason the mean is listed but not recommended: ingesting a
        harmless story LOWERS the corridor's risk."""
        with_trivia = cluster_stories(
            _distinct(1, seed=13, severity=5, confidence=1.0)
            + _distinct(20, seed=14, severity=1, confidence=0.5),
            now=NOW,
        )
        alone = cluster_stories(_distinct(1, seed=13, severity=5, confidence=1.0), now=NOW)
        self.assertLess(stat_mean(with_trivia, None), stat_mean(alone, None))

    def test_share_is_invariant_when_numerator_and_denominator_both_double(self):
        single = stat_share(self.minor, denominator=100.0)
        doubled = stat_share(self.minor_2x, denominator=200.0)
        self.assertAlmostEqual(single, doubled, places=6)

    def test_share_rises_when_risk_coverage_intensifies_at_fixed_volume(self):
        quiet = stat_share(self.minor, denominator=1000.0)
        loud = stat_share(self.severe, denominator=1000.0)
        self.assertGreater(loud, quiet)

    def test_share_without_a_denominator_is_zero_not_a_crash(self):
        self.assertEqual(stat_share(self.minor, denominator=0), 0.0)
        self.assertEqual(stat_share(self.minor, denominator=None), 0.0)


class NormalizerTests(unittest.TestCase):
    def test_every_candidate_returns_exactly_baseline_on_an_empty_corpus(self):
        """A corridor with no events must read its own baseline_risk, not 0 and
        not some relative floor — that absoluteness is why the saturating
        transform replaced min-max in the first place."""
        for candidate in CANDIDATES:
            with self.subTest(key=candidate.key):
                raw = candidate.statistic([], 100.0 if candidate.needs_denominator else None)
                self.assertEqual(candidate.normalize(raw, 0.05), 0.05)

    def test_linear_normalizer_has_no_free_constant(self):
        """One severity-5, confidence-1.0 story today == a closed corridor."""
        self.assertAlmostEqual(linear_in_severity(MAX_EVENT_WEIGHT, 0.05), 1.0)

    def test_linear_normalizer_clamps_above_the_rubric_ceiling(self):
        self.assertAlmostEqual(linear_in_severity(MAX_EVENT_WEIGHT * 3, 0.05), 1.0)

    def test_saturating_normalizer_is_monotonic_and_below_one(self):
        normalize = saturating(25.0)
        previous = 0.0
        for raw in (1, 10, 50, 200):
            score = normalize(raw, 0.05)
            self.assertGreater(score, previous)
            self.assertLess(score, 1.0)
            previous = score

    def test_saturating_normalizer_never_exceeds_one(self):
        """It asymptotes to 1.0 rather than reaching it, but float underflow in
        exp() lands exactly on 1.0 past raw ~900 — worth pinning, since a score
        above 1.0 would make effective_capacity negative and nx.maximum_flow
        raise rather than model a closed corridor."""
        self.assertLessEqual(saturating(25.0)(10_000, 0.05), 1.0)

    def test_saturating_normalizer_compresses_a_wide_raw_gap(self):
        """Why K=25 is degenerate on today's corpus: a 10% raw gap at these
        levels survives as less than one point in the third decimal."""
        normalize = saturating(25.0)
        self.assertLess(normalize(115.6, 0.20) - normalize(104.7, 0.15), 0.01)


class RegistryTests(unittest.TestCase):
    def test_keys_are_unique(self):
        keys = [c.key for c in CANDIDATES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_lookup_table_matches_the_list(self):
        self.assertEqual(set(CANDIDATES_BY_KEY), {c.key for c in CANDIDATES})

    def test_only_share_candidates_declare_a_denominator(self):
        needs = {c.key for c in CANDIDATES if c.needs_denominator}
        self.assertEqual(needs, {"share"})

"""Tests for pipeline/score/risk_scorer.py and graph/updater.py.

No network, no API calls — scoring reads ExtractedEvent rows the tests create
directly, which is also how the backtest will drive it in Phase 7.
"""
import math
from datetime import timedelta

import networkx as nx
from django.contrib.gis.geos import LineString
from django.test import TestCase
from django.utils import timezone

from core.models import Corridor, ExtractedEvent, RiskScore
from graph.updater import update_edge_weights
from pipeline.score.risk_scorer import (
    LAMBDA_DECAY,
    SATURATION_K,
    compute_all_risk_scores,
    compute_risk_score,
    normalize_score,
)

BASELINES = {"Hormuz": 0.20, "Red Sea": 0.15, "Cape": 0.05}


def _make_corridors():
    line = LineString((56.0, 26.0), (57.0, 27.0))
    for name, baseline in BASELINES.items():
        Corridor.objects.create(
            name=name, geometry=line, capacity_mbd=10.0,
            transit_days=10, baseline_risk=baseline,
        )


def _event(corridor_name, severity=4, confidence=0.9, days_ago=0, now=None, title=""):
    now = now or timezone.now()
    return ExtractedEvent.objects.create(
        corridor=Corridor.objects.get(name=corridor_name),
        actor="Iran",
        event_type="military",
        severity=severity,
        confidence=confidence,
        timestamp=now - timedelta(days=days_ago),
        article_url="https://example.com/a",
        title=title,
    )


class RawScoreTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _make_corridors()

    def test_no_events_scores_zero(self):
        self.assertEqual(compute_risk_score("Hormuz"), 0.0)

    def test_fresh_event_is_undecayed(self):
        now = timezone.now()
        _event("Hormuz", severity=4, confidence=0.9, days_ago=0, now=now)
        self.assertAlmostEqual(compute_risk_score("Hormuz", now=now), 3.6, places=6)

    def test_older_event_decays_exponentially(self):
        now = timezone.now()
        _event("Hormuz", severity=4, confidence=0.9, days_ago=10, now=now)
        expected = 4 * 0.9 * math.exp(-LAMBDA_DECAY * 10)
        self.assertAlmostEqual(compute_risk_score("Hormuz", now=now), expected, places=6)

    def test_events_accumulate(self):
        now = timezone.now()
        _event("Hormuz", severity=3, confidence=1.0, days_ago=0, now=now)
        _event("Hormuz", severity=2, confidence=0.5, days_ago=0, now=now)
        self.assertAlmostEqual(compute_risk_score("Hormuz", now=now), 4.0, places=6)

    def test_other_corridors_do_not_contribute(self):
        now = timezone.now()
        _event("Red Sea", severity=5, confidence=1.0, days_ago=0, now=now)
        self.assertEqual(compute_risk_score("Hormuz", now=now), 0.0)

    def test_corridorless_events_do_not_contribute(self):
        ExtractedEvent.objects.create(
            corridor=None, actor="OPEC", event_type="policy", severity=5,
            confidence=1.0, timestamp=timezone.now(), article_url="https://example.com/b",
        )
        self.assertEqual(compute_risk_score("Hormuz"), 0.0)

    def test_future_timestamp_cannot_amplify(self):
        now = timezone.now()
        _event("Hormuz", severity=4, confidence=1.0, days_ago=-5, now=now)
        self.assertAlmostEqual(compute_risk_score("Hormuz", now=now), 4.0, places=6)


class StoryDeduplicationTests(TestCase):
    """One wire story syndicated across outlets must not out-score a real crisis.

    Observed for real on 2026-09-13: 37 Red Sea events were only 16 stories, and
    the resulting 2.4x inflation (vs Hormuz's 1.3x) was enough to flip which
    corridor ranked most at risk.
    """

    @classmethod
    def setUpTestData(cls):
        _make_corridors()

    # Real headlines from the 2026-09-13 corpus. These two are the same story
    # reworded by different outlets and cluster at ratio 0.646.
    SYNDICATED = [
        "Yemen Houthis capture a Red Sea island in threat to shipping",
        "Yemen's Houthis reach strategic island at mouth of vital shipping lane",
    ]

    def test_syndicated_coverage_counts_once(self):
        now = timezone.now()
        for title in self.SYNDICATED:
            _event("Red Sea", severity=4, confidence=0.9, now=now, title=title)

        # One story at 4 x 0.9, not two.
        self.assertAlmostEqual(compute_risk_score("Red Sea", now=now), 3.6, places=6)

    def test_cluster_keeps_its_strongest_member(self):
        now = timezone.now()
        _event("Red Sea", severity=3, confidence=0.6, now=now, title=self.SYNDICATED[0])
        _event("Red Sea", severity=5, confidence=0.9, now=now, title=self.SYNDICATED[1])

        self.assertAlmostEqual(compute_risk_score("Red Sea", now=now), 4.5, places=6)

    def test_known_limitation_semantic_duplicates_are_not_caught(self):
        """Lexical similarity cannot see that "Perim Island" and "strategic
        island at mouth of vital shipping lane" are the same rock.

        Documented rather than fixed: catching it needs semantic matching, and
        the thresholds low enough to merge these lexically (<=0.40) over-merge
        genuinely distinct stories badly enough to flip the corridor ranking.
        Under-merging leaves some inflation; over-merging destroys real signal.
        """
        now = timezone.now()
        _event("Red Sea", severity=4, confidence=0.5, now=now, title=self.SYNDICATED[1])
        _event("Red Sea", severity=4, confidence=0.5, now=now,
               title="Houthis seize strategic Perim Island in Bab el-Mandeb Strait")

        self.assertAlmostEqual(compute_risk_score("Red Sea", now=now), 4.0, places=6)

    def test_distinct_stories_both_count(self):
        now = timezone.now()
        _event("Red Sea", severity=4, confidence=0.5, now=now,
               title="Houthis seize Perim Island in Bab el-Mandeb")
        _event("Red Sea", severity=4, confidence=0.5, now=now,
               title="OPEC weighs production quota changes at Vienna meeting")

        self.assertAlmostEqual(compute_risk_score("Red Sea", now=now), 4.0, places=6)

    def test_same_headline_outside_the_window_is_a_new_event(self):
        # A recurring attack months later is a genuinely new event, not an echo.
        now = timezone.now()
        _event("Red Sea", severity=4, confidence=0.5, days_ago=0, now=now,
               title=self.SYNDICATED[0])
        _event("Red Sea", severity=4, confidence=0.5, days_ago=30, now=now,
               title=self.SYNDICATED[0])

        score = compute_risk_score("Red Sea", now=now)
        self.assertGreater(score, 2.0)  # both counted, the older one decayed

    def test_untitled_events_are_never_merged(self):
        # An empty title is absence of evidence, not evidence of duplication.
        now = timezone.now()
        _event("Red Sea", severity=3, confidence=1.0, now=now, title="")
        _event("Red Sea", severity=2, confidence=0.5, now=now, title="")

        self.assertAlmostEqual(compute_risk_score("Red Sea", now=now), 4.0, places=6)

    def test_deduplicate_false_exposes_the_syndication_effect(self):
        now = timezone.now()
        for title in self.SYNDICATED:
            _event("Red Sea", severity=4, confidence=0.9, now=now, title=title)

        inflated = compute_risk_score("Red Sea", now=now, deduplicate=False)
        self.assertAlmostEqual(inflated, 3.6 * len(self.SYNDICATED), places=6)
        self.assertGreater(inflated, compute_risk_score("Red Sea", now=now))

    def test_events_beyond_the_lookback_are_dropped(self):
        now = timezone.now()
        _event("Red Sea", severity=5, confidence=1.0, days_ago=400, now=now,
               title="Ancient history")
        self.assertEqual(compute_risk_score("Red Sea", now=now), 0.0)


class NormalizeTests(TestCase):
    def test_zero_raw_sits_at_baseline(self):
        self.assertEqual(normalize_score(0.0, baseline_risk=0.2), 0.2)

    def test_monotonic_in_raw(self):
        scores = [normalize_score(raw, baseline_risk=0.2) for raw in (1, 5, 20, 100)]
        self.assertEqual(scores, sorted(scores))

    def test_bounded_by_one(self):
        # Even an extreme week stays strictly inside the range...
        self.assertLess(normalize_score(200, baseline_risk=0.2), 1.0)
        self.assertGreater(normalize_score(200, baseline_risk=0.2), 0.99)
        # ...and an absurd raw score saturates at exactly 1.0 (which the graph
        # reads as a closed corridor) rather than overshooting it.
        self.assertEqual(normalize_score(10_000, baseline_risk=0.2), 1.0)

    def test_never_drops_below_baseline(self):
        for raw in (0.0, 0.1, 3.0, 50.0):
            self.assertGreaterEqual(normalize_score(raw, baseline_risk=0.2), 0.2)

    def test_saturation_constant_sets_the_midpoint(self):
        # At raw == K the transform has used 1-1/e of its headroom.
        self.assertAlmostEqual(
            normalize_score(SATURATION_K, baseline_risk=0.0), 1 - math.exp(-1), places=6
        )


class ComputeAllRiskScoresTests(TestCase):
    def setUp(self):
        _make_corridors()

    def test_quiet_corridor_holds_its_baseline(self):
        # The min-max regression guard: with no events anywhere, no corridor may
        # be forced to 0.0 or 1.0 by its neighbours' scores.
        scores = compute_all_risk_scores(persist=False)
        self.assertEqual(scores, BASELINES)

    def test_quiet_corridor_is_not_zeroed_by_a_noisy_one(self):
        _event("Hormuz", severity=5, confidence=1.0)
        scores = compute_all_risk_scores(persist=False)
        self.assertEqual(scores["Cape"], BASELINES["Cape"])
        self.assertGreater(scores["Hormuz"], BASELINES["Hormuz"])
        self.assertLess(scores["Hormuz"], 1.0)

    def test_ranking_follows_event_weight(self):
        _event("Hormuz", severity=5, confidence=1.0)
        _event("Hormuz", severity=5, confidence=1.0)
        _event("Red Sea", severity=2, confidence=0.5)
        scores = compute_all_risk_scores(persist=False)
        self.assertGreater(scores["Hormuz"], scores["Red Sea"])
        self.assertGreater(scores["Red Sea"], scores["Cape"])

    def test_persist_writes_history_and_live_score(self):
        now = timezone.now()
        _event("Hormuz", severity=4, confidence=0.9, days_ago=0, now=now)

        scores = compute_all_risk_scores(now=now)

        self.assertEqual(RiskScore.objects.count(), 3)
        row = RiskScore.objects.get(corridor__name="Hormuz")
        self.assertAlmostEqual(row.raw_score, 3.6, places=6)
        self.assertAlmostEqual(row.score, scores["Hormuz"], places=9)

        corridor = Corridor.objects.get(name="Hormuz")
        self.assertAlmostEqual(corridor.live_risk_score, scores["Hormuz"], places=9)

    def test_persist_false_writes_nothing(self):
        compute_all_risk_scores(persist=False)
        self.assertEqual(RiskScore.objects.count(), 0)
        self.assertEqual(Corridor.objects.get(name="Hormuz").live_risk_score, 0.0)

    def test_history_accumulates_across_runs(self):
        compute_all_risk_scores()
        compute_all_risk_scores()
        self.assertEqual(RiskScore.objects.filter(corridor__name="Cape").count(), 2)


def _toy_graph():
    """Mirrors builder.py's tagging: only corridor->port edges carry `corridor`."""
    G = nx.DiGraph()
    G.add_node("Hormuz", kind="corridor", live_risk_score=0.0)
    G.add_node("Cape", kind="corridor", live_risk_score=0.0)
    G.add_edge("Saudi Arabia", "Hormuz", volume=2.0, effective_capacity=2.0, corridor=None)
    G.add_edge("Hormuz", "Vadinar", volume=1.0, effective_capacity=1.0, corridor="Hormuz")
    G.add_edge("Hormuz", "Mundra", volume=0.5, effective_capacity=0.5, corridor="Hormuz")
    G.add_edge("Cape", "Vadinar", volume=0.8, effective_capacity=0.8, corridor="Cape")
    G.add_edge("Vadinar", "Jamnagar", volume=1.5, effective_capacity=1.5, corridor=None)
    return G


class UpdateEdgeWeightsTests(TestCase):
    def test_tagged_edges_are_scaled_by_risk(self):
        G = _toy_graph()
        update_edge_weights(G, {"Hormuz": 0.25, "Cape": 0.0})

        self.assertAlmostEqual(G["Hormuz"]["Vadinar"]["effective_capacity"], 0.75)
        self.assertAlmostEqual(G["Hormuz"]["Mundra"]["effective_capacity"], 0.375)
        self.assertAlmostEqual(G["Cape"]["Vadinar"]["effective_capacity"], 0.8)

    def test_untagged_edges_are_untouched(self):
        G = _toy_graph()
        update_edge_weights(G, {"Hormuz": 0.9})

        self.assertAlmostEqual(G["Saudi Arabia"]["Hormuz"]["effective_capacity"], 2.0)
        self.assertAlmostEqual(G["Vadinar"]["Jamnagar"]["effective_capacity"], 1.5)

    def test_static_volume_is_preserved_for_baseline_comparison(self):
        G = _toy_graph()
        update_edge_weights(G, {"Hormuz": 0.6})
        self.assertAlmostEqual(G["Hormuz"]["Vadinar"]["volume"], 1.0)

    def test_full_risk_closes_the_corridor(self):
        G = _toy_graph()
        update_edge_weights(G, {"Hormuz": 1.0})
        self.assertAlmostEqual(G["Hormuz"]["Vadinar"]["effective_capacity"], 0.0)

    def test_out_of_range_risk_never_yields_negative_capacity(self):
        G = _toy_graph()
        update_edge_weights(G, {"Hormuz": 1.8, "Cape": -0.5})

        self.assertAlmostEqual(G["Hormuz"]["Vadinar"]["effective_capacity"], 0.0)
        self.assertAlmostEqual(G["Cape"]["Vadinar"]["effective_capacity"], 0.8)

    def test_updates_are_idempotent_from_the_static_volume(self):
        G = _toy_graph()
        update_edge_weights(G, {"Hormuz": 0.5})
        update_edge_weights(G, {"Hormuz": 0.5})
        self.assertAlmostEqual(G["Hormuz"]["Vadinar"]["effective_capacity"], 0.5)

    def test_corridor_node_carries_the_live_score(self):
        G = _toy_graph()
        update_edge_weights(G, {"Hormuz": 0.42})
        self.assertAlmostEqual(G.nodes["Hormuz"]["live_risk_score"], 0.42)

    def test_unknown_corridor_is_ignored(self):
        G = _toy_graph()
        update_edge_weights(G, {"Panama": 0.9})
        self.assertAlmostEqual(G["Hormuz"]["Vadinar"]["effective_capacity"], 1.0)


class GraphIntegrationTests(TestCase):
    """The real seeded graph, to catch a tagging drift between builder and updater."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_db", verbosity=0)

    def test_risk_reduces_max_flow_on_the_real_graph(self):
        from graph.builder import SINK, SOURCE, build_graph

        G = build_graph(persist=False)
        baseline, _ = nx.maximum_flow(G, SOURCE, SINK, capacity="effective_capacity")

        update_edge_weights(G, {"Hormuz": 0.5, "Red Sea": 0.0, "Cape": 0.0})
        disrupted, _ = nx.maximum_flow(G, SOURCE, SINK, capacity="effective_capacity")

        self.assertLess(disrupted, baseline)

    def test_zero_risk_leaves_flow_unchanged(self):
        from graph.builder import SINK, SOURCE, build_graph

        G = build_graph(persist=False)
        baseline, _ = nx.maximum_flow(G, SOURCE, SINK, capacity="effective_capacity")

        update_edge_weights(G, {"Hormuz": 0.0, "Red Sea": 0.0, "Cape": 0.0})
        after, _ = nx.maximum_flow(G, SOURCE, SINK, capacity="effective_capacity")

        self.assertAlmostEqual(after, baseline, places=9)

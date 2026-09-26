"""Tests for the Phase 6 REST API (core/views.py, core/urls.py).

Mocking convention: none. Every endpoint is exercised through Django's test
client against the seeded DB with SYNTHETIC corridor risk written onto the
Corridor rows (the same fixture values test_trigger.py uses), never the live
corpus. The graph singleton is cleared around each test so no test inherits
another's graph.

What is pinned: response shapes (CLAUDE.md's documented keys must stay),
error codes, and cross-validation against the modules the views wrap. A view
is a thin wrapper, so the strongest check is "the endpoint returns exactly
what the function returns".
"""
import json
from datetime import datetime, timedelta, timezone

from django.core.management import call_command
from django.test import TestCase

from core.models import Corridor, ExtractedEvent, PipelineRun, RiskScore
from criticality.engine import compute_criticality
from graph.builder import build_graph
from graph.state import GraphState
from graph.updater import load_live_graph, update_edge_weights
from response.plan import build_response
from response.spr import compute_spr_schedule

SYNTHETIC_RISK = {"Hormuz": 0.615, "Red Sea": 0.537, "Cape": 0.05}


def _reject_constant(name):
    raise ValueError(f"non-strict JSON constant {name} in response")


class ApiTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_db", verbosity=0)
        for name, risk in SYNTHETIC_RISK.items():
            Corridor.objects.filter(name=name).update(live_risk_score=risk)

    def setUp(self):
        GraphState.get_instance().clear()

    def tearDown(self):
        GraphState.get_instance().clear()

    def get_json(self, url, params=None, status=200):
        r = self.client.get(url, params or {})
        self.assertEqual(r.status_code, status, r.content[:500])
        # strict: a NaN/Infinity would break the browser's JSON.parse
        return json.loads(r.content, parse_constant=_reject_constant)

    def post_json(self, url, body, status=200):
        r = self.client.post(url, json.dumps(body), content_type="application/json")
        self.assertEqual(r.status_code, status, r.content[:500])
        return json.loads(r.content, parse_constant=_reject_constant)

    def risk_graph(self):
        G = build_graph(persist=False)
        update_edge_weights(G, SYNTHETIC_RISK)
        return G


class RiskScoreEndpointTests(ApiTestBase):
    def test_risk_scores_is_the_documented_flat_dict(self):
        self.assertEqual(self.get_json("/api/risk-scores/"), SYNTHETIC_RISK)

    def test_risk_scores_does_not_depend_on_a_recent_score_run(self):
        """The spec's "last hour" filter would return {} here: there are no
        RiskScore rows at all, yet the stored live score is still the truth."""
        self.assertFalse(RiskScore.objects.exists())
        self.assertEqual(len(self.get_json("/api/risk-scores/")), 3)

    def test_history_labels_the_formula_either_side_of_the_cutover(self):
        hormuz = Corridor.objects.get(name="Hormuz")
        old = RiskScore.objects.create(corridor=hormuz, score=0.99, raw_score=115.6)
        new = RiskScore.objects.create(corridor=hormuz, score=0.875, raw_score=4.22)
        RiskScore.objects.filter(pk=old.pk).update(computed_at=datetime(2026, 9, 20, 15, tzinfo=timezone.utc))
        RiskScore.objects.filter(pk=new.pk).update(computed_at=datetime(2026, 9, 26, 14, tzinfo=timezone.utc))

        rows = self.get_json("/api/risk-scores/history/", {"corridor": "Hormuz"})

        self.assertEqual([r["formula"] for r in rows], ["sum_saturating", "top3pad"])  # oldest first
        self.assertEqual(rows[0]["corridor"], "Hormuz")

    def test_history_days_filter(self):
        hormuz = Corridor.objects.get(name="Hormuz")
        old = RiskScore.objects.create(corridor=hormuz, score=0.5, raw_score=2.0)
        RiskScore.objects.create(corridor=hormuz, score=0.6, raw_score=3.0)
        RiskScore.objects.filter(pk=old.pk).update(
            computed_at=datetime.now(timezone.utc) - timedelta(days=30)
        )
        self.assertEqual(len(self.get_json("/api/risk-scores/history/", {"days": 7})), 1)
        self.get_json("/api/risk-scores/history/", {"days": 0}, status=400)


class CriticalityEndpointTests(ApiTestBase):
    SPEC_KEYS = {"corridor", "static_rank", "risk_rank", "centrality", "capacity_loss_mbd"}

    def test_matches_compute_criticality_on_the_risk_weighted_graph(self):
        rows = self.get_json("/api/criticality/")

        self.assertEqual(rows, compute_criticality(self.risk_graph()))
        self.assertTrue(all(self.SPEC_KEYS <= set(r) for r in rows))

    def test_risk_actually_reached_the_edges(self):
        """The Phase 4 silent-duplication trap: an un-risked graph gives
        rank_shift 0 everywhere. Under the synthetic risk Cape overtakes Hormuz."""
        rows = {r["corridor"]: r for r in self.get_json("/api/criticality/")}
        self.assertEqual(rows["Cape"]["rank_shift"], 1)
        self.assertEqual(rows["Hormuz"]["rank_shift"], -1)

    def test_rank_by_validation(self):
        self.get_json("/api/criticality/", {"rank_by": "centrality"})
        self.get_json("/api/criticality/", {"rank_by": "vibes"}, status=400)

    def test_port_view(self):
        rows = self.get_json("/api/criticality/ports/")
        self.assertEqual(len(rows), 10)
        self.assertIn("stranded_mbd", rows[0])

    def test_stored_risk_change_reaches_the_next_request(self):
        """score_risk may run in another process; the server must notice
        without a restart, and must not mutate the graph a request already holds."""
        self.get_json("/api/criticality/")
        held = GraphState.get_instance().get_graph()

        Corridor.objects.filter(name="Hormuz").update(live_risk_score=0.2)
        rows = {r["corridor"]: r for r in self.get_json("/api/criticality/")}

        self.assertAlmostEqual(rows["Hormuz"]["live_risk_score"], 0.2)
        self.assertAlmostEqual(held.nodes["Hormuz"]["live_risk_score"], 0.615)
        self.assertIsNot(GraphState.get_instance().get_graph(), held)

    def test_unchanged_risk_reuses_the_graph(self):
        first = load_live_graph()
        self.assertIs(load_live_graph(), first)

    def test_unseeded_database_is_503_not_400(self):
        Corridor.objects.all().delete()
        body = self.get_json("/api/criticality/", status=503)
        self.assertIn("seed_db", body["detail"])


class CascadeAndScenarioEndpointTests(ApiTestBase):
    def test_cascade_steps_up_to_the_requested_degradation(self):
        rows = self.get_json("/api/cascade/", {"corridor": "Hormuz", "degradation": 30})
        self.assertEqual([r["degradation_pct"] for r in rows], [10, 20, 30])
        self.assertTrue({"capacity_loss_mbd", "affected_refineries", "affected_count"} <= set(rows[0]))

    def test_cascade_defaults_to_the_full_curve(self):
        self.assertEqual(len(self.get_json("/api/cascade/", {"corridor": "Cape"})), 10)

    def test_cascade_rejects_suez_and_missing_corridor(self):
        self.assertIn("Suez", self.get_json("/api/cascade/", {"corridor": "Suez"}, status=400)["error"])
        self.get_json("/api/cascade/", status=400)

    def test_scenarios_carry_the_spec_fraction(self):
        rows = {s["key"]: s for s in self.get_json("/api/scenarios/")}
        self.assertEqual(set(rows), {"hormuz_30", "hormuz_full", "red_sea", "opec_cut"})
        self.assertAlmostEqual(rows["hormuz_30"]["degradation"], 0.30)
        self.assertEqual(rows["hormuz_30"]["degradation_pct"], 30)
        self.assertIsNone(rows["opec_cut"]["degradation"])  # supply shock, no corridor


class RerouteAndSprEndpointTests(ApiTestBase):
    SPEC_KEYS = {"source", "score", "cost_score", "transit_score", "compat_score",
                 "transit_days", "price_premium"}

    def test_reroute_ranking(self):
        rows = self.get_json("/api/reroute/", {"corridor": "Hormuz", "crisis": "normal"})
        self.assertEqual(rows[0]["source"], "UAE via Fujairah")
        self.assertTrue(all(self.SPEC_KEYS <= set(r) for r in rows))
        self.assertFalse(any(r["sanctioned"] for r in rows))

    def test_reroute_include_sanctioned_and_gap(self):
        rows = self.get_json("/api/reroute/", {
            "corridor": "Hormuz", "include_sanctioned": "true", "gap_mbd": 0.5,
        })
        self.assertTrue(any(r["sanctioned"] for r in rows))
        self.assertIn("cumulative_coverage_pct", rows[0])

    def test_reroute_errors(self):
        self.get_json("/api/reroute/", {"corridor": "Suez"}, status=400)
        self.get_json("/api/reroute/", {"corridor": "Hormuz", "crisis": "apocalyptic"}, status=400)
        self.get_json("/api/reroute/", status=400)

    def test_spr_matches_the_lp_and_has_the_spec_alias(self):
        body = self.get_json("/api/spr/", {"gap_mbd": 2.0, "duration_days": 10, "transit_days": 10})
        expected = compute_spr_schedule(2.0, 10, 10)

        self.assertEqual(body["daily_schedule"], expected["daily_schedule"])
        self.assertEqual(body["days_until_threshold"], expected["days_of_cover"])
        for key in ("daily_schedule", "total_released_mb", "insufficient", "days_until_threshold"):
            self.assertIn(key, body)

    def test_spr_validation(self):
        self.get_json("/api/spr/", {"gap_mbd": 2.0}, status=400)
        self.get_json("/api/spr/", {"gap_mbd": -1, "duration_days": 10, "transit_days": 5}, status=400)


class SimulateEndpointTests(ApiTestBase):
    def test_matches_build_response_on_the_same_graph(self):
        body = self.post_json("/api/simulate/", {"corridor": "Cape", "degradation": 1.0, "duration_days": 14})
        expected = build_response(self.risk_graph(), corridor="Cape", degradation_pct=100, duration_days=14)

        self.assertAlmostEqual(body["gap"]["gap_mbd"], expected["gap"]["gap_mbd"], places=9)
        self.assertEqual(body["spr"]["daily_schedule"], expected["spr"]["daily_schedule"])
        self.assertEqual([r["source"] for r in body["reroute"]], [r["source"] for r in expected["reroute"]])
        self.assertEqual(body["timeline"], expected["timeline"])
        self.assertEqual(len(body["cascade"]), 10)
        self.assertIn("days_until_threshold", body["spr"])

    def test_uses_the_exact_slider_value(self):
        body = self.post_json("/api/simulate/", {"corridor": "Hormuz", "degradation": 0.55})
        expected = build_response(self.risk_graph(), corridor="Hormuz", degradation_pct=55)
        self.assertAlmostEqual(body["gap"]["degradation_pct"], 55)
        self.assertAlmostEqual(body["gap"]["gap_mbd"], expected["gap"]["gap_mbd"], places=9)

    def test_supply_scenario_has_no_corridor_curve(self):
        body = self.post_json("/api/simulate/", {"scenario": "opec_cut"})
        self.assertEqual(body["cascade"], [])
        self.assertEqual(body["gap"]["mechanism"], "supply")

    def test_validation(self):
        self.post_json("/api/simulate/", {"corridor": "Hormuz", "degradation": 1.5}, status=400)
        self.post_json("/api/simulate/", {"corridor": "Hormuz", "degradation": 0}, status=400)
        self.post_json("/api/simulate/", {"corridor": "Hormuz", "scenario": "hormuz_30"}, status=400)
        self.post_json("/api/simulate/", {}, status=400)
        self.post_json("/api/simulate/", {"corridor": "Suez"}, status=400)
        self.post_json("/api/simulate/", {"corridor": "Hormuz", "duration_days": 0}, status=400)

    def test_simulate_is_post_only(self):
        self.assertEqual(self.client.get("/api/simulate/").status_code, 405)

    def test_simulation_never_mutates_the_live_graph(self):
        before = self.get_json("/api/criticality/")
        self.post_json("/api/simulate/", {"corridor": "Hormuz", "degradation": 1.0})
        self.assertEqual(self.get_json("/api/criticality/"), before)


class MapAndEventEndpointTests(ApiTestBase):
    def test_geojson_feature_collection(self):
        body = self.get_json("/api/corridors/geojson/")
        self.assertEqual(body["type"], "FeatureCollection")
        feats = {f["properties"]["name"]: f for f in body["features"]}
        self.assertEqual(set(feats), set(SYNTHETIC_RISK))
        self.assertEqual(feats["Hormuz"]["geometry"]["type"], "LineString")
        stored = Corridor.objects.get(name="Hormuz").geometry.coords
        self.assertEqual(feats["Hormuz"]["geometry"]["coordinates"], [list(p) for p in stored])
        self.assertAlmostEqual(feats["Hormuz"]["properties"]["risk_score"], 0.615)
        # the 999 sentinel must not reach a chart axis as a real capacity
        self.assertTrue(feats["Cape"]["properties"]["capacity_unlimited"])
        self.assertIsNone(feats["Cape"]["properties"]["capacity_mbd"])
        self.assertEqual(feats["Hormuz"]["properties"]["capacity_mbd"], 17.0)

    def _event(self, corridor, title, days_ago):
        return ExtractedEvent.objects.create(
            corridor=Corridor.objects.get(name=corridor) if corridor else None,
            actor="Iran", event_type="military", severity=4, confidence=0.9, title=title,
            timestamp=datetime.now(timezone.utc) - timedelta(days=days_ago),
            article_url=f"https://example.com/{title}",
        )

    def test_events_newest_first_with_filters(self):
        self._event("Hormuz", "old", 3)
        self._event("Red Sea", "new", 1)
        self._event(None, "general", 2)

        rows = self.get_json("/api/events/live/")
        self.assertEqual([r["title"] for r in rows], ["new", "general", "old"])
        self.assertIsNone(rows[1]["corridor"])
        self.assertEqual(len(self.get_json("/api/events/live/", {"limit": 1})), 1)
        self.assertEqual(
            [r["title"] for r in self.get_json("/api/events/live/", {"corridor": "Hormuz"})], ["old"],
        )
        for key in ("corridor", "actor", "event_type", "severity", "confidence", "timestamp"):
            self.assertIn(key, rows[0])


class PipelineRunEndpointTests(ApiTestBase):
    def test_latest_is_404_before_any_run(self):
        self.get_json("/api/pipeline/latest/", status=404)

    def test_latest_runs_and_detail(self):
        older = PipelineRun.objects.create(status="succeeded")
        PipelineRun.objects.filter(pk=older.pk).update(
            started_at=datetime.now(timezone.utc) - timedelta(hours=6)
        )
        newer = PipelineRun.objects.create(
            status="partial", triggered_corridor="Cape", errors=[{"stage": "x", "error": "y"}],
        )

        self.assertEqual(self.get_json("/api/pipeline/latest/")["id"], newer.pk)
        runs = self.get_json("/api/pipeline/runs/")
        self.assertEqual([r["id"] for r in runs], [newer.pk, older.pk])
        self.assertEqual(runs[0]["error_count"], 1)
        self.assertEqual(self.get_json(f"/api/pipeline/runs/{older.pk}/")["status"], "succeeded")
        self.get_json("/api/pipeline/runs/999999/", status=404)


class CrossCuttingTests(ApiTestBase):
    def test_backtest_is_explicitly_not_implemented(self):
        self.assertIn("Phase 7", self.get_json("/api/backtest/", status=501)["error"])

    def test_cors_allows_the_dashboard_origin(self):
        r = self.client.get("/api/risk-scores/", HTTP_ORIGIN="http://localhost:3000")
        self.assertEqual(r["Access-Control-Allow-Origin"], "http://localhost:3000")

    def test_cors_rejects_other_origins(self):
        r = self.client.get("/api/risk-scores/", HTTP_ORIGIN="http://evil.example")
        self.assertNotIn("Access-Control-Allow-Origin", r)

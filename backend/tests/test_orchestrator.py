"""Tests for the Phase 6 LangGraph orchestrator (orchestrator/*) and the
``run_pipeline`` command.

Mocking convention: the DATA stages' task functions are patched on
``pipeline.tasks`` (which ``orchestrator.nodes`` calls through the module, so
that is the single patch point). No test touches the network or spends money.
The ANALYSIS stages run for real against the seeded graph under synthetic
risk written onto the Corridor rows, same fixture as test_trigger.py:
Hormuz 0.615 / Red Sea 0.537 / Cape 0.05, under which Cape crosses the loss
clause and is triggered.
"""
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.models import Corridor, PipelineRun
from graph.builder import build_graph
from graph.state import GraphState
from graph.updater import update_edge_weights
from orchestrator.pipeline import FULL_CYCLE, resolve_options, run_pipeline
from response.plan import build_response

SYNTHETIC_RISK = {"Hormuz": 0.615, "Red Sea": 0.537, "Cape": 0.05}

DATA_TASKS = ("poll_rss", "poll_gdelt_gkg", "poll_gdelt_by_corridor",
              "poll_gdelt_with_fallback", "extract_events", "score_and_update_graph")


class OrchestratorTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_db", verbosity=0)
        for name, risk in SYNTHETIC_RISK.items():
            Corridor.objects.filter(name=name).update(live_risk_score=risk)

    def setUp(self):
        GraphState.get_instance().clear()
        # Every data task is mocked in every test: a test that forgets to set
        # a return value still cannot reach the network or the paid API.
        self.tasks = {}
        for name in DATA_TASKS:
            p = patch(f"pipeline.tasks.{name}")
            self.tasks[name] = p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        GraphState.get_instance().clear()


class AnalysisOnlyRunTests(OrchestratorTestBase):
    def test_bare_run_calls_no_data_stage(self):
        run_pipeline()
        for name, mock in self.tasks.items():
            mock.assert_not_called()

    def test_bare_run_leaves_stored_risk_untouched(self):
        run_pipeline()
        self.assertEqual(
            {c.name: c.live_risk_score for c in Corridor.objects.all()}, SYNTHETIC_RISK,
        )

    def test_bare_run_follows_the_spec_order_and_triggers(self):
        s = run_pipeline()

        self.assertEqual(
            s["stages_run"],
            ["update_graph", "criticality", "check_threshold", "response", "dashboard_update"],
        )
        self.assertEqual(s["errors"], [])
        self.assertTrue(s["threshold_crossed"])
        self.assertEqual(s["triggered_corridor"], "Cape")
        self.assertEqual(s["risk_scores"], SYNTHETIC_RISK)
        self.assertEqual(s["capacity_loss_mbd"], s["criticality_ranking"][0]["capacity_loss_mbd"])

    def test_response_matches_build_response(self):
        s = run_pipeline(crisis="severe", duration_days=30)

        G = build_graph(persist=False)
        update_edge_weights(G, SYNTHETIC_RISK)
        expected = build_response(G, corridor="Cape", crisis="severe", duration_days=30)

        self.assertAlmostEqual(s["response"]["gap"]["gap_mbd"], expected["gap"]["gap_mbd"], places=9)
        self.assertEqual(s["spr_schedule"]["daily_schedule"], expected["spr"]["daily_schedule"])
        self.assertEqual(
            [r["source"] for r in s["reroute_recommendations"]],
            [r["source"] for r in expected["reroute"]],
        )

    def test_run_is_persisted_and_round_trips(self):
        s = run_pipeline()

        run = PipelineRun.objects.get(pk=s["run_id"])
        self.assertEqual(run.status, PipelineRun.STATUS_SUCCEEDED)
        self.assertEqual(run.triggered_corridor, "Cape")
        self.assertEqual(run.criticality, json.loads(json.dumps(s["criticality_ranking"])))
        self.assertEqual(run.response["spr"]["daily_schedule"], s["spr_schedule"]["daily_schedule"])
        self.assertEqual(run.stages_run[-1], "dashboard_update")
        self.assertIsNotNone(run.finished_at)
        self.assertLessEqual(run.started_at, run.finished_at)
        self.assertEqual(run.options["crisis"], "normal")

    def test_no_persist_writes_nothing(self):
        s = run_pipeline(persist=False)
        self.assertIsNone(s["run_id"])
        self.assertFalse(PipelineRun.objects.exists())


class ConditionalEdgeTests(OrchestratorTestBase):
    NOT_CROSSED = {
        "threshold_crossed": False, "triggered_corridor": None, "reason": "none",
        "triggered": [], "evaluated": [], "baseline_flow_mbd": 1.0,
        "loss_fraction_threshold": 0.15, "risk_alone_threshold": 0.75,
    }

    def test_not_crossed_skips_the_response_node(self):
        with patch("orchestrator.nodes._check_threshold", return_value=self.NOT_CROSSED):
            s = run_pipeline()

        self.assertNotIn("response", s["stages_run"])
        self.assertEqual(s["stages_run"][-1], "dashboard_update")
        self.assertIsNone(PipelineRun.objects.get(pk=s["run_id"]).response)


class FailureContainmentTests(OrchestratorTestBase):
    def test_a_failing_stage_is_recorded_and_the_run_completes(self):
        with patch("orchestrator.nodes.compute_criticality", side_effect=RuntimeError("boom")):
            s = run_pipeline()

        stages = [e["stage"] for e in s["errors"]]
        self.assertIn("criticality", stages)
        # the next stage reports its missing input instead of computing on nothing
        self.assertIn("check_threshold", stages)
        self.assertFalse(s["threshold_crossed"])
        run = PipelineRun.objects.get(pk=s["run_id"])
        self.assertEqual(run.status, PipelineRun.STATUS_PARTIAL)
        self.assertIn("boom", run.errors[0]["error"])

    def test_one_dead_ingest_source_does_not_cost_the_others(self):
        self.tasks["poll_rss"].side_effect = RuntimeError("dead feed")
        self.tasks["poll_gdelt_gkg"].return_value = {"Hormuz": {"fetched": 1}}

        s = run_pipeline(ingest=["rss", "gkg"])

        self.tasks["poll_gdelt_gkg"].assert_called_once()
        self.assertIsNone(s["ingest_report"]["rss"])
        self.assertEqual(s["ingest_report"]["gkg"], {"Hormuz": {"fetched": 1}})
        self.assertEqual(s["errors"][0]["stage"], "ingest")
        self.assertIn("response", s["stages_run"])  # analysis still ran

    def test_empty_scoring_result_is_an_error_not_silence(self):
        self.tasks["score_and_update_graph"].return_value = {}
        s = run_pipeline(score=True)
        self.assertIn("score", [e["stage"] for e in s["errors"]])

    def test_orchestrator_crash_leaves_a_failed_row(self):
        with patch("orchestrator.pipeline.build_pipeline") as mock_build:
            mock_build.return_value.invoke.side_effect = RuntimeError("graph broke")
            with self.assertRaises(RuntimeError):
                run_pipeline()
        run = PipelineRun.objects.get()
        self.assertEqual(run.status, PipelineRun.STATUS_FAILED)
        self.assertEqual(run.errors[0]["stage"], "orchestrator")


class FullCycleTests(OrchestratorTestBase):
    def test_full_cycle_calls_every_data_stage_once(self):
        self.tasks["poll_rss"].return_value = 3
        self.tasks["poll_gdelt_with_fallback"].return_value = {
            "Hormuz": {"fetched": 2, "stored": 2, "status": "ok", "sampled": True, "path": "doc"},
        }
        self.tasks["extract_events"].return_value = {"articles": 5, "events": 4}
        self.tasks["score_and_update_graph"].return_value = SYNTHETIC_RISK

        s = run_pipeline(**FULL_CYCLE, last_minutes=240, extract_limit=5)

        self.tasks["poll_rss"].assert_called_once()
        self.tasks["poll_gdelt_with_fallback"].assert_called_once_with(max_records=None, last_minutes=240)
        self.tasks["extract_events"].assert_called_once_with(limit=5)
        self.tasks["score_and_update_graph"].assert_called_once()
        self.tasks["poll_gdelt_gkg"].assert_not_called()  # only via the fallback
        self.assertEqual(s["stages_run"][:3], ["ingest", "extract", "score"])
        self.assertEqual(s["ingest_report"]["rss"], {"stored": 3})
        self.assertEqual(s["extraction_report"], {"articles": 5, "events": 4})

    def test_full_cycle_definition(self):
        self.assertEqual(FULL_CYCLE["ingest"], ["rss", "gdelt-fallback"])
        self.assertTrue(FULL_CYCLE["extract"] and FULL_CYCLE["score"])


class OptionValidationTests(TestCase):
    def test_bad_options_fail_before_any_stage(self):
        for bad in ({"ingest": ["reuters"]}, {"crisis": "mild"}, {"duration_days": 0}, {"colour": 1}):
            with self.assertRaises(ValueError, msg=bad):
                resolve_options(**bad)

    def test_defaults_are_analysis_only(self):
        opts = resolve_options()
        self.assertEqual(opts["ingest"], [])
        self.assertFalse(opts["extract"])
        self.assertFalse(opts["score"])


class RunPipelineCommandTests(OrchestratorTestBase):
    def call(self, *args):
        out = StringIO()
        call_command("run_pipeline", *args, stdout=out)
        return out.getvalue()

    def test_bare_command(self):
        out = self.call()
        self.assertIn("analysis only", out)
        self.assertIn("TRIGGERED: Cape", out)
        self.assertIn("saved PipelineRun", out)
        self.assertEqual(PipelineRun.objects.count(), 1)
        out.encode("ascii")  # the Windows console mangles non-ASCII

    def test_full_command_warns_about_cost_and_the_snapshot(self):
        self.tasks["poll_rss"].return_value = 0
        self.tasks["poll_gdelt_with_fallback"].return_value = {}
        self.tasks["extract_events"].return_value = {"articles": 0}
        self.tasks["score_and_update_graph"].return_value = SYNTHETIC_RISK

        out = self.call("--full")

        self.assertIn("PAID", out)
        self.assertIn("Thesis Snapshot", out)
        self.tasks["poll_gdelt_with_fallback"].assert_called_once()

    def test_explicit_ingest_overrides_the_full_default(self):
        self.tasks["poll_gdelt_gkg"].return_value = {}
        self.tasks["extract_events"].return_value = {}
        self.tasks["score_and_update_graph"].return_value = SYNTHETIC_RISK
        self.call("--full", "--ingest", "gkg")
        self.tasks["poll_gdelt_gkg"].assert_called_once()
        self.tasks["poll_gdelt_with_fallback"].assert_not_called()

    def test_ingest_report_shows_provenance_and_unsampled(self):
        self.tasks["poll_gdelt_with_fallback"].return_value = {
            "Hormuz": {"fetched": 3, "stored": 3, "status": "ok", "sampled": True, "path": "doc+gkg"},
            "Cape": {"fetched": 0, "stored": 0, "status": "error", "sampled": False, "path": "gkg"},
        }
        out = self.call("--ingest", "gdelt-fallback", "--no-persist")
        self.assertIn("via doc+gkg", out)
        self.assertIn("NOT SAMPLED", out)

    def test_unknown_source_is_a_command_error(self):
        with self.assertRaises(CommandError):
            self.call("--ingest", "reuters")


class NoCeleryGuardTests(TestCase):
    def test_orchestrator_has_no_celery_coupling(self):
        root = Path(settings.BASE_DIR) / "orchestrator"
        for path in root.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("import celery", "from celery", "shared_task", ".delay(", "apply_async"):
                self.assertNotIn(forbidden, source, f"{forbidden!r} in {path.name}")

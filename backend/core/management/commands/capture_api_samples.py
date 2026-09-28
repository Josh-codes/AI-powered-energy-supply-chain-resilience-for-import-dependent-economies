"""Capture one real response per API route to data/api_samples/.

    python manage.py capture_api_samples
    python manage.py capture_api_samples --run-id 3 --out data/api_samples

The samples are the frontend contract: a real payload per route cannot drift
from the code the way prose can, and API_DOCS.md is written from them. Goes
through Django's test client, so no server has to be running.

READ-ONLY. Every captured route is a GET or /api/simulate/, which degrades a
copy of the graph and persists nothing, so capturing cannot move
``Corridor.live_risk_score`` or the Thesis Snapshot. What it captures IS the
live DB state at capture time: the index records the live risk scores next to
the samples, so a reader can tell which world they describe.
"""
import json
import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.test import Client
from django.utils import timezone

from core.models import Corridor, PipelineRun

logger = logging.getLogger(__name__)

DEFAULT_OUT = Path(settings.BASE_DIR) / "data" / "api_samples"

#: The Thesis Snapshot run (CLAUDE.md): scores + the 14-day response.
DEFAULT_RUN_ID = 3

#: (name, method, path, query-or-body, expected status, description).
#: ``{run_id}`` in a path is filled from ``--run-id``.
SAMPLES = [
    ("risk_scores", "GET", "/api/risk-scores/", {}, 200,
     "Live risk score per corridor (Corridor.live_risk_score)."),
    ("risk_scores_history_hormuz", "GET", "/api/risk-scores/history/",
     {"corridor": "Hormuz"}, 200,
     "RiskScore rows oldest-first; each row labelled with its formula."),
    ("criticality", "GET", "/api/criticality/", {}, 200,
     "Static vs risk-weighted ranking, ranked by capacity loss (the default)."),
    ("criticality_by_centrality", "GET", "/api/criticality/",
     {"rank_by": "centrality"}, 200,
     "Same rows, ranks recomputed off centrality (diagnostic only)."),
    ("criticality_ports", "GET", "/api/criticality/ports/", {}, 200,
     "Per-port residual / stranded-crude view."),
    ("cascade_hormuz", "GET", "/api/cascade/",
     {"corridor": "Hormuz", "degradation": 100}, 200,
     "Cascade curve, 10% steps up to degradation (a PERCENT, 1-100)."),
    ("scenarios", "GET", "/api/scenarios/", {}, 200,
     "Named scenario definitions."),
    ("reroute_cape", "GET", "/api/reroute/",
     {"corridor": "Cape", "crisis": "normal"}, 200,
     "Ranked alternative suppliers for a corridor."),
    ("spr", "GET", "/api/spr/",
     {"gap_mbd": 2.0, "duration_days": 14, "transit_days": 10}, 200,
     "SPR drawdown schedule for a constant gap."),
    ("simulate_cape_full", "POST", "/api/simulate/",
     {"corridor": "Cape", "degradation": 1.0, "duration_days": 14}, 200,
     "Scenario slider: cascade + gap + reroute + timeline + SPR. "
     "degradation is a FRACTION (0-1]."),
    ("simulate_scenario_hormuz_30", "POST", "/api/simulate/",
     {"scenario": "hormuz_30"}, 200,
     "Same, driven by a named scenario instead of a corridor."),
    ("corridors_geojson", "GET", "/api/corridors/geojson/", {}, 200,
     "GeoJSON FeatureCollection for Leaflet, risk in properties."),
    ("events_live", "GET", "/api/events/live/", {"limit": 5}, 200,
     "Latest extracted events (default limit 50; 5 here to keep it short)."),
    ("pipeline_latest", "GET", "/api/pipeline/latest/", {}, 200,
     "The most recent PipelineRun, all columns."),
    ("pipeline_runs", "GET", "/api/pipeline/runs/", {"limit": 10}, 200,
     "PipelineRun summaries, newest first."),
    ("pipeline_run_snapshot", "GET", "/api/pipeline/runs/{run_id}/", {}, 200,
     "The cited Thesis Snapshot run: every thesis figure lives in here."),
    ("backtest_list", "GET", "/api/backtest/", {}, 200,
     "Defined backtest events with their verdicts."),
    ("backtest_2026_verdict", "GET", "/api/backtest/",
     {"event": "2026_hormuz_closure", "series": "false"}, 200,
     "One backtest report without the daily series."),
    ("backtest_2026_full", "GET", "/api/backtest/",
     {"event": "2026_hormuz_closure"}, 200,
     "One backtest report with the daily series (the chart data)."),
    ("backtest_2025_full", "GET", "/api/backtest/",
     {"event": "2025_iran_standoff"}, 200,
     "The 2025 report: rows carry day_sampled / unsampled_days_in_lookback."),
    # ---- the error contract ----
    ("error_simulate_suez", "POST", "/api/simulate/",
     {"corridor": "Suez", "degradation": 0.5}, 400,
     "Domain error -> 400 {\"error\": ...}. Suez is folded into Red Sea."),
    ("error_cascade_missing_corridor", "GET", "/api/cascade/", {}, 400,
     "Field validation error -> 400 with DRF's per-field messages."),
    ("error_backtest_unknown_event", "GET", "/api/backtest/",
     {"event": "no_such_event"}, 400,
     "Unknown backtest key -> 400."),
    ("error_pipeline_run_missing", "GET", "/api/pipeline/runs/999999/", {}, 404,
     "Missing run -> 404 {\"error\": ...}."),
]


def _url(path, query):
    if not query:
        return path
    return path + "?" + "&".join(f"{k}={v}" for k, v in query.items())


def capture_samples(out_dir, run_id=DEFAULT_RUN_ID, client=None):
    """Write every sample plus ``index.json`` to *out_dir*. Returns the index.

    A status that differs from the expected one is recorded, logged and
    reported, never raised: an unrun backtest or a fresh DB with no
    PipelineRun is a legitimate state to document.
    """
    # The test client's default host "testserver" is not in ALLOWED_HOSTS
    # outside `manage.py test`, so present as localhost.
    client = client or Client(HTTP_HOST="localhost")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for name, method, path, params, expected, description in SAMPLES:
        path = path.format(run_id=run_id)
        if method == "GET":
            response = client.get(path, params)
            request = {"method": method, "url": _url(path, params)}
        else:
            response = client.post(path, json.dumps(params), content_type="application/json")
            request = {"method": method, "url": path, "body": params}
        try:
            body = json.loads(response.content)
        except ValueError:
            body = response.content.decode("utf-8", errors="replace")

        sample = {
            "name": name,
            "description": description,
            "request": request,
            "status": response.status_code,
            "response": body,
        }
        filename = f"{name}.json"
        with open(out_dir / filename, "w", encoding="utf-8") as fh:
            json.dump(sample, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

        if response.status_code != expected:
            logger.warning("%s: expected HTTP %s, got %s", name, expected, response.status_code)
        entries.append({
            "name": name,
            **request,
            "status": response.status_code,
            "expected_status": expected,
            "file": filename,
        })

    index = {
        "captured_at": timezone.now().isoformat(timespec="seconds"),
        "live_risk_scores": dict(
            Corridor.objects.order_by("name").values_list("name", "live_risk_score")
        ),
        "latest_pipeline_run": PipelineRun.objects.values_list("pk", flat=True).first(),
        "snapshot_run_id": run_id,
        "samples": entries,
    }
    with open(out_dir / "index.json", "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return index


class Command(BaseCommand):
    help = "Capture one real response per API route to data/api_samples/ (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--out", default=str(DEFAULT_OUT),
                            help="output directory (default data/api_samples/)")
        parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID,
                            help="PipelineRun to capture as the snapshot sample (default 3)")

    def handle(self, *args, **options):
        index = capture_samples(options["out"], run_id=options["run_id"])
        w = self.stdout.write
        off = [s for s in index["samples"] if s["status"] != s["expected_status"]]
        w(f"  {len(index['samples'])} samples -> {options['out']}")
        w(f"  live risk scores at capture: {index['live_risk_scores']}")
        for s in off:
            w(self.style.WARNING(
                f"  {s['name']}: HTTP {s['status']} (expected {s['expected_status']})"
            ))
        if not off:
            w(self.style.SUCCESS("  every route answered with its expected status."))

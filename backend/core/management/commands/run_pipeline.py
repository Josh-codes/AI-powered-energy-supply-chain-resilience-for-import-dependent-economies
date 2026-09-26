"""Run the Phase 6 LangGraph orchestrator once and print a stage summary.

A bare run is ANALYSIS ONLY, on the stored corridor risk: free, no network,
and it leaves the Thesis Snapshot figures unchanged. It still writes one
PipelineRun row (skip with --no-persist), which never touches
Corridor.live_risk_score.

    python manage.py run_pipeline                      # analysis + persist
    python manage.py run_pipeline --full               # whole cycle (PAID, re-scores)
    python manage.py run_pipeline --ingest rss,gkg --extract --score
    python manage.py run_pipeline --crisis severe --duration-days 30

--full = --ingest rss,gdelt-fallback --extract --score. gdelt-fallback polls the
GDELT DOC API once per corridor and, on the first 429, switches to the GKG bulk
files (~280 MB per 24h) for all three corridors.
"""
from django.core.management.base import BaseCommand, CommandError

from orchestrator.nodes import INGEST_SOURCES
from orchestrator.pipeline import FULL_CYCLE, resolve_options, run_pipeline
from response.reroute import CRISIS_WEIGHTS


class Command(BaseCommand):
    help = "Run the orchestrated pipeline once (analysis-only unless stages are requested)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--full", action="store_true",
            help="Whole cycle: --ingest rss,gdelt-fallback --extract --score. PAID, and re-scores.",
        )
        parser.add_argument(
            "--ingest", default="",
            help=f"Comma-separated sources to poll first: {','.join(INGEST_SOURCES)}.",
        )
        parser.add_argument("--last-minutes", type=int, help="Ingest window in minutes (default 1440).")
        parser.add_argument("--max-records", type=int, help="GDELT DOC articles per corridor (default 50).")
        parser.add_argument("--extract", action="store_true", help="Run LLM extraction (PAID).")
        parser.add_argument("--extract-limit", type=int, help="Cap articles extracted this run.")
        parser.add_argument(
            "--score", action="store_true",
            help="Re-score corridors. MOVES THE THESIS SNAPSHOT figures.",
        )
        parser.add_argument("--crisis", choices=sorted(CRISIS_WEIGHTS), default="normal")
        parser.add_argument("--duration-days", type=int, default=14, help="SPR planning horizon.")
        parser.add_argument("--include-sanctioned", action="store_true")
        parser.add_argument("--no-persist", action="store_true", help="Do not write a PipelineRun row.")

    def handle(self, *args, **options):
        w = self.stdout.write
        ok, warn, err = self.style.SUCCESS, self.style.WARNING, self.style.ERROR

        overrides = {
            "ingest": [s.strip() for s in options["ingest"].split(",") if s.strip()],
            "last_minutes": options["last_minutes"],
            "max_records": options["max_records"],
            "extract": options["extract"],
            "extract_limit": options["extract_limit"],
            "score": options["score"],
            "crisis": options["crisis"],
            "duration_days": options["duration_days"],
            "include_sanctioned": options["include_sanctioned"],
            "persist": not options["no_persist"],
        }
        if options["full"]:
            overrides.update(
                {k: v for k, v in FULL_CYCLE.items() if k != "ingest" or not overrides["ingest"]}
            )
        try:
            resolve_options(**overrides)
        except ValueError as exc:
            raise CommandError(str(exc))

        if overrides["extract"]:
            w(warn("extract is ON - this run makes PAID OpenRouter calls."))
        if overrides["score"]:
            w(warn("score is ON - Corridor.live_risk_score will change, moving every "
                   "Thesis Snapshot figure. Regenerate all figures from this run."))
        if not (overrides["ingest"] or overrides["extract"] or overrides["score"]):
            w("analysis only, on the stored corridor risk (snapshot-safe).")

        s = run_pipeline(**overrides)

        w(ok(f"\nstages run : {' -> '.join(s.get('stages_run', []))}"))
        if s.get("ingest_report") is not None:
            w("ingest:")
            for source, rep in s["ingest_report"].items():
                if rep is None:
                    w(err(f"  {source:15} FAILED"))
                elif "stored" in rep and not isinstance(rep["stored"], dict):
                    w(f"  {source:15} stored {rep['stored']}")
                else:
                    for corridor, c in rep.items():
                        path = f" via {c['path']}" if "path" in c else ""
                        line = (f"  {source:15} {corridor:8} fetched {c['fetched']:>4} "
                                f"stored {c['stored']:>4} {c['status']}{path}")
                        w(line if c["sampled"] else warn(line + "  NOT SAMPLED"))
        if s.get("extraction_report") is not None:
            w("extract    : " + ", ".join(f"{k}={v}" for k, v in s["extraction_report"].items()))
        if s.get("risk_scores"):
            w("risk       : " + ", ".join(f"{n}={v:.3f}" for n, v in sorted(s["risk_scores"].items())))

        rows = s.get("criticality_ranking") or []
        if rows:
            w(ok(f"\nCriticality (risk-weighted flow {s['baseline_flow_mbd']:.3f} mb/d)"))
            w(f"  {'corridor':10} {'static':>6} {'risk':>5} {'shift':>6} {'loss':>7}")
            for r in rows:
                w(f"  {r['corridor']:10} {r['static_rank']:>6} {r['risk_rank']:>5} "
                  f"{r['rank_shift']:>+6} {r['capacity_loss_mbd']:>7.3f}")

        t = s.get("threshold")
        if t:
            if t["threshold_crossed"]:
                w(warn(f"\nTRIGGERED: {t['triggered_corridor']} - {t['reason']}"
                       f"  (all crossing: {', '.join(t['triggered'])})"))
            else:
                w("\nNot triggered: no corridor crossed either clause.")

        resp = s.get("response")
        if resp:
            gap, tl, spr = resp["gap"], resp["timeline"], resp["spr"]
            w(ok(f"\nResponse for {gap['corridor']} ({options['crisis']}, "
                 f"{options['duration_days']}-day horizon)"))
            w(f"  gap          : {gap['gap_mbd']:.3f} mb/d")
            if resp["reroute"]:
                w(f"  top option   : {resp['reroute'][0]['source']} "
                  f"(score {resp['reroute'][0]['score']:.3f})")
            if tl["alternatives_used"]:
                w(f"  replacement  : {len(tl['alternatives_used'])} alternatives, "
                  f"slowest lands day {tl['transit_days']}; residual {tl['residual_gap_mbd']:.3f} mb/d")
            w(f"  SPR          : {spr['total_released_mb']:.2f} of {spr['required_mb']:.2f} mb "
              f"({spr['gap_covered_pct']:.1f}%), {spr['status']}")
            if spr["reserve_exhausted_day"] is not None:
                w(warn(f"  reserve runs dry on day {spr['reserve_exhausted_day']}"))

        errors = s.get("errors") or []
        for e in errors:
            w(err(f"\nERROR in {e['stage']}: {e['error']}"))
        if s.get("run_id"):
            w(ok(f"\nsaved PipelineRun {s['run_id']} "
                 f"({'partial' if errors else 'succeeded'})"))
        elif overrides["persist"]:
            w(err("\nPipelineRun was NOT saved - see errors above"))

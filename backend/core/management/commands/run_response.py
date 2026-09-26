"""Run the Phase 5 response layer and print its results: threshold trigger ->
supply gap -> ranked alternatives -> SPR drawdown schedule.

Writes nothing to the database, but it is NOT side-effect free: like
``run_criticality`` it rebuilds the graph into the GraphState singleton and
pushes each Corridor's stored ``live_risk_score`` onto that graph's edges
(``--ignore-risk`` skips the second step). Free: no network, no API calls.

    python manage.py run_response --auto
    python manage.py run_response --corridor Hormuz --degradation 100 --crisis severe
    python manage.py run_response --corridor Cape --duration-days 30
    python manage.py run_response --scenario hormuz_full
    python manage.py run_response --corridor Hormuz --include-sanctioned
"""
from django.core.management.base import BaseCommand

from core.models import Corridor
from criticality.scenarios import SCENARIOS
from graph.builder import build_graph
from graph.state import GraphState
from graph.updater import update_edge_weights
from response.plan import build_response
from response.reroute import CRISIS_WEIGHTS
from response.trigger import evaluate_graph


class Command(BaseCommand):
    help = "Evaluate the threshold trigger and compute reroute + SPR recommendations."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group()
        target.add_argument("--corridor", help="Corridor to disrupt.")
        target.add_argument("--scenario", choices=sorted(SCENARIOS), help="Named scenario to respond to.")
        target.add_argument(
            "--auto", action="store_true",
            help="Respond to whichever corridor the threshold trigger selects (nothing if none crosses).",
        )
        parser.add_argument(
            "--degradation", type=int, default=100,
            help="Degradation %% for --corridor, on top of today's risk (default: 100).",
        )
        parser.add_argument("--crisis", choices=sorted(CRISIS_WEIGHTS), default="normal", help="MCDM weight set.")
        parser.add_argument("--duration-days", type=int, default=14, help="SPR planning horizon (default: 14).")
        parser.add_argument("--include-sanctioned", action="store_true", help="Keep sanctioned alternatives.")
        parser.add_argument(
            "--no-rebuild", action="store_true",
            help="Use the graph already in the GraphState singleton instead of rebuilding.",
        )
        parser.add_argument(
            "--ignore-risk", action="store_true",
            help="Skip applying stored corridor risk (effective_capacity == volume).",
        )

    def handle(self, *args, **options):
        w = self.stdout.write
        ok, warn, err = self.style.SUCCESS, self.style.WARNING, self.style.ERROR

        if options["no_rebuild"]:
            G = GraphState.get_instance().get_graph()
            if G is None:
                w(err("no graph in the singleton - drop --no-rebuild"))
                return
        else:
            G = build_graph(persist=True)

        # Same reason as run_criticality: without this push the risk-weighted
        # world silently equals the static one.
        if not options["ignore_risk"]:
            scores = {c.name: c.live_risk_score for c in Corridor.objects.all()}
            update_edge_weights(G, scores)
            w("applied stored corridor risk: "
              + ", ".join(f"{n}={s:.3f}" for n, s in sorted(scores.items())))

        # ---- 1. threshold ---------------------------------------------------
        t = evaluate_graph(G)
        w(ok(
            f"\nThreshold trigger (loss > {t['loss_fraction_threshold']:.0%} of "
            f"{t['baseline_flow_mbd']:.3f} mb/d risk-weighted flow, OR risk > "
            f"{t['risk_alone_threshold']:.2f})"
        ))
        w(f"  {'corridor':10} {'loss':>8} {'of_flow':>8} {'risk':>6}  crossed")
        for e in t["evaluated"]:
            clauses = [n for n, hit in (("loss", e["loss_crossed"]), ("risk", e["risk_crossed"])) if hit]
            line = (
                f"  {e['corridor']:10} {e['capacity_loss_mbd']:>8.3f} {e['loss_fraction']:>8.1%} "
                f"{e['live_risk_score']:>6.3f}  {'+'.join(clauses) or '-'}"
            )
            w(warn(line) if e["crossed"] else line)
        if t["threshold_crossed"]:
            w(warn(f"\n  TRIGGERED: {t['triggered_corridor']} - {t['reason']}"))
        else:
            w("\n  Not triggered: no corridor crossed either clause.")

        # ---- 2. what to respond to -------------------------------------------
        duration = options["duration_days"]
        if duration <= 0:
            w(err("\n--duration-days must be > 0"))
            return
        common = dict(
            crisis=options["crisis"], duration_days=duration,
            include_sanctioned=options["include_sanctioned"],
        )
        try:
            if options["scenario"]:
                resp = build_response(G, scenario=options["scenario"], **common)
            elif options["corridor"]:
                resp = build_response(
                    G, corridor=options["corridor"], degradation_pct=options["degradation"], **common,
                )
            elif options["auto"]:
                if not t["threshold_crossed"]:
                    w("\n  --auto: nothing crossed, so no recommendations generated.")
                    return
                resp = build_response(G, corridor=t["triggered_corridor"], **common)
            else:
                w("\n  Pass --corridor NAME, --scenario KEY or --auto for recommendations.")
                return
        except ValueError as exc:
            w(err(f"\n{exc}"))
            return
        gap, ranked, timeline, spr = resp["gap"], resp["reroute"], resp["timeline"], resp["spr"]

        # ---- 3. gap ---------------------------------------------------------
        label = (
            f"scenario {gap['scenario']}" if gap["scenario"]
            else f"{gap['corridor']} at {gap['degradation_pct']}% degradation"
        )
        w(ok(f"\nSupply gap: {label}  (baseline: {gap['baseline']}, i.e. on top of today's risk)"))
        w(f"  flow             : {gap['baseline_flow_mbd']:.3f} -> {gap['flow_after_mbd']:.3f} mb/d")
        w(f"  gap              : {gap['gap_mbd']:.3f} mb/d"
          + (f"  ({gap['total_shortfall_mb']:.2f} mb over {duration} days)" if gap["total_shortfall_mb"] else ""))
        w(f"  refineries hit   : {gap['affected_count']}  "
          f"(listed shortfall {gap['refinery_shortfall_mbd']:.3f}, "
          f"below 5% deadband {gap['deadband_unattributed_mbd']:.3f})")
        for a in gap["affected_refineries"][:5]:
            w(f"    {a['refinery']:24} -{a['shortfall_mbd']:.3f} mb/d  ({a['pct_of_baseline']:.0%} of baseline)")
        if gap["mechanism"] == "supply":
            w(warn("  supply shock: no corridor excluded from rerouting; OPEC membership of "
                   "alternatives is not modelled."))

        # ---- 4. reroute -----------------------------------------------------
        w(ok(f"\nReroute ranking (crisis={options['crisis']}, "
             f"weights cost/transit/compat={CRISIS_WEIGHTS[options['crisis']]})"))
        if not ranked:
            w(warn("  no eligible alternatives - every modelled route transits this corridor or is sanctioned"))
        else:
            w(f"  {'#':>2} {'source':22} {'score':>6} {'cost':>5} {'trans':>5} {'compat':>6} "
              f"{'days':>4} {'prem':>6} {'+mb/d':>6} {'cum%':>6}")
            for i, r in enumerate(ranked, start=1):
                line = (
                    f"  {i:>2} {r['source']:22} {r['score']:>6.3f} {r['cost_score']:>5.2f} "
                    f"{r['transit_score']:>5.2f} {r['compat_score']:>6.2f} {r['transit_days']:>4} "
                    f"{r['price_premium']:>+6.1f} {r['max_incremental_mbd']:>6.2f} "
                    f"{r['cumulative_coverage_pct']:>5.0f}%"
                )
                w(warn(line) if r["sanctioned"] else line)
            w("  (+mb/d volumes are cited ESTIMATES, not a reconciled balance - coverage is indicative)")

        if timeline["transit_days"] is None:
            if gap["gap_mbd"] > 0:
                w(warn(f"  no replacement crude: SPR faces the full gap for the whole {duration}-day horizon"))
        else:
            w(f"  replacement      : {', '.join(timeline['alternatives_used'])} - "
              f"slowest lands day {timeline['transit_days']}")
            if not timeline["covers_gap"]:
                w(warn(f"  residual gap     : {timeline['residual_gap_mbd']:.3f} mb/d stays "
                       "uncovered even after every alternative arrives"))

        # ---- 5. SPR ---------------------------------------------------------
        # The gap steps down as each cargo lands and keeps any residual open,
        # rather than one step to zero at the slowest arrival.
        profile = timeline["daily_gap_mbd"]
        w(ok(f"\nSPR drawdown ({duration}-day horizon, gap open on {spr['bridge_days']} days; "
             f"{spr['available_mb']:.2f} mb releasable)"))
        w(f"  status           : {spr['status']}")
        steps = [f"{profile[0]:.3f} (day 0)"] + [
            f"{profile[t]:.3f} (day {t})" for t in range(1, len(profile)) if abs(profile[t] - profile[t - 1]) > 1e-9
        ]
        w("  gap by day       : " + " -> ".join(steps) + " mb/d")
        w(f"  released         : {spr['total_released_mb']:.2f} of {spr['required_mb']:.2f} mb required "
          f"({spr['gap_covered_pct']:.1f}%)")
        if spr["days_of_cover"] is not None:
            w(f"  days of cover    : {spr['days_of_cover']:.1f} at the initial gap's drawdown rate")
        w("  daily (mb/d)     : " + " ".join(f"{v:.2f}" for v in spr["daily_schedule"]))
        if spr["insufficient"]:
            w(warn(f"  INSUFFICIENT: {spr['total_unmet_mb']:.2f} mb of the gap is unmet "
                   f"(peak daily shortfall {max(spr['unmet_mbd']):.3f} mb/d)"))
        if spr["reserve_exhausted_day"] is not None:
            w(warn(f"  reserve runs dry on day {spr['reserve_exhausted_day']} with gap still open"))
        if spr["gap_at_horizon_end_mbd"] > 1e-9:
            w(warn(f"  gap still {spr['gap_at_horizon_end_mbd']:.3f} mb/d on the last planned day "
                   "- the crisis outlasts this horizon"))

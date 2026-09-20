"""Run the criticality engine and print its results.

Writes nothing to the database, but it is NOT side-effect free: it rebuilds the
graph into the GraphState singleton and pushes each Corridor's stored
``live_risk_score`` onto that graph's edges (``--ignore-risk`` skips the
second step). Without that push a freshly built graph sits at
``effective_capacity == volume`` and the risk-weighted ranking silently
duplicates the static one.

    python manage.py run_criticality
    python manage.py run_criticality --port-view
    python manage.py run_criticality --corridor Hormuz --cascade
    python manage.py run_criticality --scenario hormuz_full
    python manage.py run_criticality --rank-by centrality
"""
from django.core.management.base import BaseCommand

from core.models import Corridor
from criticality.cascade import cascading_failure_simulation
from criticality.engine import RANK_KEYS, compute_criticality, compute_port_criticality
from criticality.scenarios import SCENARIOS, run_scenario
from graph.builder import build_graph
from graph.state import GraphState
from graph.updater import update_edge_weights


class Command(BaseCommand):
    help = "Compute and print static vs. risk-weighted corridor criticality."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rank-by", choices=RANK_KEYS, default="capacity_loss",
            help="Metric driving static_rank/risk_rank (default: capacity_loss).",
        )
        parser.add_argument("--port-view", action="store_true", help="Also print the per-port residual/stranded breakdown.")
        parser.add_argument("--corridor", help="Corridor to run the cascade simulation on.")
        parser.add_argument("--cascade", action="store_true", help="Run the 10%%-step cascade for --corridor.")
        parser.add_argument("--scenario", choices=sorted(SCENARIOS), help="Run one named scenario.")
        parser.add_argument(
            "--no-rebuild", action="store_true",
            help="Use the graph already in the GraphState singleton instead of rebuilding.",
        )
        parser.add_argument(
            "--ignore-risk", action="store_true",
            help="Skip applying stored corridor risk, leaving effective_capacity == volume "
                 "(both rankings then read the same structural world).",
        )

    def handle(self, *args, **options):
        w = self.stdout.write
        ok, warn = self.style.SUCCESS, self.style.WARNING

        if options["no_rebuild"]:
            G = GraphState.get_instance().get_graph()
            if G is None:
                w(self.style.ERROR("no graph in the singleton — drop --no-rebuild"))
                return
        else:
            G = build_graph(persist=True)

        # A freshly built graph has effective_capacity == volume, so without
        # this the risk-weighted ranking would silently be a second copy of the
        # static one. Scores come from Corridor.live_risk_score, last written
        # by `manage.py score_risk`.
        if not options["ignore_risk"]:
            scores = {c.name: c.live_risk_score for c in Corridor.objects.all()}
            update_edge_weights(G, scores)
            w(f"applied stored corridor risk: "
              + ", ".join(f"{n}={s:.3f}" for n, s in sorted(scores.items())))

        rows = compute_criticality(G, rank_by=options["rank_by"])

        w(ok(f"\nCorridor criticality (rank_by={options['rank_by']}, rank 1 = most critical)"))
        w(
            f"  {'corridor':10} {'static':>6} {'risk':>5} {'shift':>6} "
            f"{'st_loss':>8} {'rk_loss':>8} {'st_cent':>8} {'rk_cent':>8} {'risk_sc':>8} {'residual':>9}"
        )
        for r in rows:
            line = (
                f"  {r['corridor']:10} {r['static_rank']:>6} {r['risk_rank']:>5} "
                f"{r['rank_shift']:>+6} {r['static_capacity_loss_mbd']:>8.3f} "
                f"{r['capacity_loss_mbd']:>8.3f} {r['static_centrality']:>8.4f} "
                f"{r['centrality']:>8.4f} {r['live_risk_score']:>8.3f} "
                f"{r['residual_load_bearing_mbd']:>9.3f}"
            )
            w(warn(line) if r["rank_shift"] else line)

        shifted = [r for r in rows if r["rank_shift"]]
        if shifted:
            w(warn(
                "\n  Rank shift detected - current conditions move "
                + ", ".join(f"{r['corridor']} ({r['rank_shift']:+d})" for r in shifted)
                + " relative to the structural baseline."
            ))
        else:
            w("\n  No rank shift: risk-weighted ranking matches the structural baseline.")

        if options["port_view"]:
            w(ok("\nPer-port residual criticality (redundancy-aware)"))
            w(f"  {'port':14} {'inflow':>8} {'need':>8} {'stranded':>9}  per-corridor residual need")
            for p in compute_port_criticality(G):
                residual = ", ".join(f"{c}={v:.3f}" for c, v in sorted(p["corridors"].items()))
                line = (
                    f"  {p['port']:14} {p['total_naive_inflow_mbd']:>8.3f} "
                    f"{p['downstream_need_mbd']:>8.3f} {p['stranded_mbd']:>9.3f}  {residual}"
                )
                w(warn(line) if p["stranded_mbd"] > 1e-6 else line)

        if options["cascade"]:
            corridor = options["corridor"]
            if not corridor:
                w(self.style.ERROR("\n--cascade needs --corridor NAME"))
                return
            w(ok(f"\nCascading failure: {corridor}"))
            w(f"  {'pct':>4} {'flow_after':>11} {'loss':>8} {'affected':>9}  top-3 shortfalls")
            for step in cascading_failure_simulation(corridor, G=G):
                top = ", ".join(
                    f"{a['refinery']} -{a['shortfall_mbd']:.3f}"
                    for a in step["affected_refineries"][:3]
                )
                w(
                    f"  {step['degradation_pct']:>4} {step['flow_after_mbd']:>11.3f} "
                    f"{step['capacity_loss_mbd']:>8.3f} {step['affected_count']:>9}  {top}"
                )

        if options["scenario"]:
            r = run_scenario(options["scenario"], G=G)
            w(ok(f"\nScenario: {r['name']}  [{r['source']}]"))
            w(f"  mechanism        : {r['mechanism']}")
            if r["mechanism"] == "corridor":
                w(f"  corridor         : {r['corridor']} at {r['degradation_pct']}% degradation")
            else:
                w(f"  supply reduction : {r['supply_reduction_mbd']:.2f} mb/d")
            w(f"  flow             : {r['baseline_flow_mbd']:.3f} -> {r['flow_after_mbd']:.3f} mb/d")
            w(f"  capacity loss    : {r['capacity_loss_mbd']:.3f} mb/d")
            w(f"  refineries hit   : {r['affected_count']}")
            if r.get("price_impact_usd"):
                w(f"  cited price est. : +${r['price_impact_usd']}/bbl (external estimate, not model output)")

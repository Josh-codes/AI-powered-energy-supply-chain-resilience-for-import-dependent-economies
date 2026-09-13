"""Recompute corridor risk scores and push them onto the in-memory graph.

Writes a RiskScore row per corridor (permanent history) and refreshes each
Corridor's live_risk_score. No API calls, no cost — safe to re-run.

    python manage.py score_risk
"""
from django.core.management.base import BaseCommand

from core.models import Corridor, ExtractedEvent
from graph.state import GraphState
from pipeline.score.risk_scorer import compute_risk_score
from pipeline.tasks import score_and_update_graph


class Command(BaseCommand):
    help = "Recompute corridor risk scores and update the graph's edge weights."

    def handle(self, *args, **options):
        scores = score_and_update_graph()
        if not scores:
            self.stdout.write(self.style.ERROR("scoring failed — see logs"))
            return

        self.stdout.write(f"{'corridor':12} {'events':>7} {'raw':>9} {'score':>8}")
        for corridor in Corridor.objects.all():
            events = ExtractedEvent.objects.filter(corridor=corridor).count()
            raw = compute_risk_score(corridor.name)
            score = scores.get(corridor.name, 0.0)
            style = self.style.WARNING if score > 0.50 else self.style.SUCCESS
            self.stdout.write(
                f"{corridor.name:12} {events:>7} {raw:>9.3f} " + style(f"{score:>8.3f}")
            )

        graph = GraphState.get_instance().get_graph()
        if graph is None:
            self.stdout.write(self.style.WARNING("\ngraph not loaded — edge weights unchanged"))
            return

        self.stdout.write("\ncorridor -> port effective capacity (mb/day):")
        for u, v, data in graph.edges(data=True):
            if data.get("corridor"):
                self.stdout.write(
                    f"  {u:10} -> {v:16} {data['volume']:.3f} -> {data['effective_capacity']:.3f}"
                )

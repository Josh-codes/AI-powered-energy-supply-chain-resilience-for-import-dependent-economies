"""Manually trigger Phase 2 ingestion (GDELT + RSS + OFAC) without the orchestrator.

Hits the real external sources and writes RawArticle rows / the OFAC cache file.
Safe to re-run: articles are deduplicated by URL.

    python manage.py poll_sources
    python manage.py poll_sources --source gdelt
"""
from django.core.management.base import BaseCommand

from core.models import RawArticle
from pipeline.tasks import download_ofac, poll_gdelt_by_corridor, poll_rss

SOURCES = {
    "gdelt": ("GDELT", None, "articles"),   # reported per corridor, see _poll_gdelt
    "rss": ("RSS feeds", poll_rss, "articles"),
    "ofac": ("OFAC SDN", download_ofac, "entities"),
}


class Command(BaseCommand):
    help = "Run the Phase 2 ingestion sources and report what was stored."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source", choices=[*SOURCES, "all"], default="all",
            help="Which source to poll (default: all).",
        )

    def handle(self, *args, **options):
        chosen = list(SOURCES) if options["source"] == "all" else [options["source"]]

        for key in chosen:
            if key == "gdelt":
                self._poll_gdelt()
                continue
            label, task, unit = SOURCES[key]
            count = task()
            style = self.style.SUCCESS if count else self.style.WARNING
            self.stdout.write(style(f"{label:12}: {count} new {unit}"))

        self.stdout.write(f"\nRawArticle rows total: {RawArticle.objects.count()}")

    def _poll_gdelt(self):
        """GDELT runs one query per corridor and any single one can be throttled,
        so show the per-corridor breakdown rather than only a combined total."""
        report = poll_gdelt_by_corridor()
        total = sum(counts["stored"] for counts in report.values())

        style = self.style.SUCCESS if total else self.style.WARNING
        self.stdout.write(style(f"{'GDELT':12}: {total} new articles"))

        for corridor, counts in report.items():
            if not counts["sampled"]:
                self.stdout.write(
                    self.style.ERROR(
                        f"  {corridor:10}: NOT SAMPLED ({counts['status']}) — no answer from GDELT"
                    )
                )
            elif not counts["fetched"]:
                self.stdout.write(
                    f"  {corridor:10}: 0 articles (query answered, nothing matched)"
                )
            else:
                self.stdout.write(
                    f"  {corridor:10}: {counts['fetched']} fetched, {counts['stored']} new"
                )

        starved = [c for c, counts in report.items() if not counts["sampled"]]
        if starved:
            self.stdout.write(self.style.ERROR(
                f"\n  CORPUS IS BIASED: {', '.join(starved)} contributed nothing because the\n"
                "  query was never answered, not because those corridors are quiet.\n"
                "  Corridor risk scores are NOT comparable until every corridor is sampled.\n"
                "  Re-run `manage.py poll_sources --source gdelt` in a few minutes."
            ))

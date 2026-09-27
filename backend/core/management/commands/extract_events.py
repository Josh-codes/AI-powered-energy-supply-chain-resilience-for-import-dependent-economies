"""Extract every unprocessed RawArticle into ExtractedEvent rows.

Makes one paid LLM call per article, so --limit is worth using while testing.
--workers sends that many calls at once: the cost is identical, only the wall
time changes (measured 2.9 s/article serially).

    python manage.py extract_events --limit 10
    python manage.py extract_events                 # 4 concurrent calls
    python manage.py extract_events --workers 8     # faster; if "call failures"
                                                    # climb, drop back and re-run

Safe to interrupt (Ctrl+C): every answer is saved as it arrives, and anything
not yet answered stays unprocessed for the next run.
"""
import time

from django.core.management.base import BaseCommand, CommandError

from core.models import ExtractedEvent, RawArticle
from pipeline.tasks import extract_events

DEFAULT_WORKERS = 4


class Command(BaseCommand):
    help = "Run LLM extraction over unprocessed articles."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Maximum articles to send to the API (default: all pending).",
        )
        parser.add_argument(
            "--workers", type=int, default=DEFAULT_WORKERS,
            help=f"Concurrent LLM calls (default {DEFAULT_WORKERS}; 1 = one at a time).",
        )

    def handle(self, *args, **options):
        if options["workers"] < 1:
            raise CommandError("--workers must be at least 1")
        pending = RawArticle.objects.filter(processed=False).count()
        limit = options["limit"]
        self.stdout.write(
            f"{pending} unprocessed articles"
            + (f", extracting up to {limit}" if limit else ", extracting all")
            + f", {options['workers']} at a time"
        )

        started = time.monotonic()

        def progress(counts, total):
            done = counts["articles"]
            elapsed = time.monotonic() - started
            eta = elapsed / done * (total - done) if done else 0
            line = (
                f"  {done:>6}/{total}  events {counts['events']:>5}  "
                f"irrelevant {counts['irrelevant']:>5}  failed {counts['call_failed']:>3}"
                f"  | {elapsed / 60:5.1f} min, ~{eta / 60:.0f} min left"
            )
            self.stdout.write(self.style.WARNING(line) if counts["call_failed"] else line)

        counts = extract_events(limit=limit, workers=options["workers"], on_progress=progress)

        self.stdout.write(self.style.SUCCESS(f"\nevents created : {counts['events']}"))
        self.stdout.write(f"articles seen  : {counts['articles']}")
        self.stdout.write(f"irrelevant     : {counts['irrelevant']}")

        for label, key in (("unparseable", "unparseable"), ("call failures", "call_failed")):
            line = f"{label:14} : {counts[key]}"
            self.stdout.write(self.style.WARNING(line) if counts[key] else line)
        if counts["call_failed"]:
            self.stdout.write(self.style.WARNING(
                "  failed articles were left unprocessed - re-run this command to retry "
                "only those (consider fewer --workers if the provider is rate-limiting)"
            ))

        self.stdout.write(
            f"\nExtractedEvent rows total: {ExtractedEvent.objects.count()}"
            f"  |  still unprocessed: {RawArticle.objects.filter(processed=False).count()}"
        )

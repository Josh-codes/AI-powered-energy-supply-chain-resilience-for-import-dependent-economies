"""Extract every unprocessed RawArticle into ExtractedEvent rows.

Makes one paid OpenAI call per article, so --limit is worth using while testing.

    python manage.py extract_events --limit 10
    python manage.py extract_events
"""
from django.core.management.base import BaseCommand

from core.models import ExtractedEvent, RawArticle
from pipeline.tasks import extract_events


class Command(BaseCommand):
    help = "Run LLM extraction over unprocessed articles."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Maximum articles to send to the API (default: all pending).",
        )

    def handle(self, *args, **options):
        pending = RawArticle.objects.filter(processed=False).count()
        limit = options["limit"]
        self.stdout.write(
            f"{pending} unprocessed articles"
            + (f", extracting up to {limit}" if limit else ", extracting all")
        )

        counts = extract_events(limit=limit)

        self.stdout.write(self.style.SUCCESS(f"\nevents created : {counts['events']}"))
        self.stdout.write(f"articles seen  : {counts['articles']}")
        self.stdout.write(f"irrelevant     : {counts['irrelevant']}")

        for label, key in (("unparseable", "unparseable"), ("call failures", "call_failed")):
            line = f"{label:14} : {counts[key]}"
            self.stdout.write(self.style.WARNING(line) if counts[key] else line)

        self.stdout.write(
            f"\nExtractedEvent rows total: {ExtractedEvent.objects.count()}"
            f"  |  still unprocessed: {RawArticle.objects.filter(processed=False).count()}"
        )

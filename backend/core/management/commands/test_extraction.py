"""Run one stored article through the LLM extractor and print the result.

Reads a RawArticle already in the database (ingest it first with
`manage.py poll_sources`) — GDELT gives metadata only, so there is no page to
fetch. Makes exactly one paid API call and writes nothing: no ExtractedEvent row
is created and the article's `processed` flag is left alone.

    python manage.py test_extraction --url "https://example.com/article"
    python manage.py test_extraction          # uses the oldest unprocessed article
"""
import json

from django.core.management.base import BaseCommand, CommandError

from core.models import RawArticle
from pipeline.extract.extractor import extract_event


class Command(BaseCommand):
    help = "Extract one stored article via the LLM and print the parsed result."

    def add_arguments(self, parser):
        parser.add_argument(
            "--url", help="URL of a stored RawArticle (default: oldest unprocessed).",
        )

    def handle(self, *args, **options):
        url = options["url"]
        if url:
            article = RawArticle.objects.filter(url=url).first()
            if article is None:
                raise CommandError(
                    f"no RawArticle with url {url!r} — run `manage.py poll_sources` first"
                )
        else:
            article = RawArticle.objects.filter(processed=False).order_by("ingested_at").first()
            if article is None:
                raise CommandError("no unprocessed articles in the database")

        self.stdout.write(f"source : {article.source}")
        self.stdout.write(f"title  : {article.title}")
        self.stdout.write(f"url    : {article.url}")
        self.stdout.write("\n--- raw_text ---")
        self.stdout.write(article.raw_text)

        result = extract_event(article)
        self.stdout.write("\n--- extraction ---")
        if result is None:
            self.stdout.write(self.style.ERROR("extraction failed — see logs"))
            return

        self.stdout.write(json.dumps(result, indent=2))
        style = self.style.SUCCESS if result["is_relevant"] else self.style.WARNING
        self.stdout.write(style(f"\nrelevant: {result['is_relevant']} (nothing was saved)"))

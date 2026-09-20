"""Repoint existing ExtractedEvent.timestamp at GDELT's seendate.

Events created before the seendate fix used RawArticle.ingested_at, which is
NOT a safe proxy: GDELT returns articles it first saw days earlier (up to 4 on
the stored corpus), so those events were dated as if brand new. Under the
scorer's 0.1/day decay a 3-day error over-weights an article by ~35%.

This joins ExtractedEvent back to RawArticle on article_url to recover the
seendate. That join only works while the RawArticle rows still exist -- once
the 14-day cleanup runs, the original dates are gone for good.

RSS-sourced events are left alone: rss.py never captured a publication date,
so ingest time remains the only thing available for them.

    python manage.py backfill_event_timestamps --dry-run
    python manage.py backfill_event_timestamps
"""
from django.core.management.base import BaseCommand

from core.models import ExtractedEvent, RawArticle
from pipeline.ingest.gdelt import parse_seendate


class Command(BaseCommand):
    help = "Repoint ExtractedEvent.timestamp at GDELT seendate where available."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        w = self.stdout.write
        dry_run = options["dry_run"]

        seendates = {}
        for url, raw_text in RawArticle.objects.values_list("url", "raw_text"):
            seen = parse_seendate(raw_text)
            if seen is not None:
                seendates[url] = seen

        events = list(ExtractedEvent.objects.all())
        updates, shifts, unchanged, no_date = [], [], 0, 0

        for event in events:
            seen = seendates.get(event.article_url)
            if seen is None:
                no_date += 1
                continue
            delta_days = (event.timestamp - seen).total_seconds() / 86400
            if abs(delta_days) < 1 / 1440:  # already correct to the minute
                unchanged += 1
                continue
            event.timestamp = seen
            updates.append(event)
            shifts.append(delta_days)

        w(f"events                : {len(events)}")
        w(f"  no seendate available: {no_date}  (RSS, or article row cleaned up)")
        w(f"  already correct      : {unchanged}")
        w(f"  to repoint           : {len(updates)}")

        if shifts:
            w(
                f"\nage correction (days the event moves EARLIER):"
                f"\n  min {min(shifts):.2f} | median {sorted(shifts)[len(shifts) // 2]:.2f} "
                f"| max {max(shifts):.2f}"
            )
            stale = sum(1 for s in shifts if s >= 1)
            w(f"  events that were dated 1+ day too recent: {stale}")

        if not updates:
            w(self.style.SUCCESS("\nNothing to do."))
            return

        if dry_run:
            w(self.style.WARNING("\n--dry-run: nothing written."))
            return

        ExtractedEvent.objects.bulk_update(updates, ["timestamp"])
        w(self.style.SUCCESS(f"\nRepointed {len(updates)} event timestamps."))
        w("Re-run `manage.py score_risk` — corridor scores will change.")

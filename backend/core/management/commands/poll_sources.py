"""Manually trigger Phase 2 ingestion (GDELT + RSS + OFAC) without the orchestrator.

Hits the real external sources and writes RawArticle rows / the OFAC cache file.
Safe to re-run: articles are deduplicated by URL.

    python manage.py poll_sources
    python manage.py poll_sources --source gdelt
    python manage.py poll_sources --corridor Hormuz
    python manage.py poll_sources --source gkg

``--corridor`` polls a single GDELT corridor in one request. Use it to space
the three corridors across separate runs, minutes apart, when GDELT's per-IP
limiter is refusing a full sweep — every corridor still gets sampled, which is
what keeps their risk scores comparable.

``--source gkg`` bypasses the DOC API entirely, reading GDELT's static
15-minute bulk files. Use it when the limiter refuses even single-corridor
polling. It cannot be throttled, but a 24h window is ~96 downloads (~300 MB),
so it is excluded from ``--source all``.
"""
from django.core.management.base import BaseCommand, CommandError

from core.models import RawArticle
from pipeline.ingest.gdelt import (
    CORRIDOR_QUERIES,
    DEFAULT_LAST_MINUTES,
    DEFAULT_MAX_RECORDS,
    GDELT_RECORD_CEILING,
    LAST_MINUTES_GRANULARITY,
)
from pipeline.ingest.gdelt_gkg import (
    DEFAULT_LAST_MINUTES as GKG_DEFAULT_LAST_MINUTES,
    PUBLICATION_LAG_MINUTES as GKG_PUBLICATION_LAG,
    SOURCE_LABEL as GKG_SOURCE_LABEL,
    slice_timestamps as gkg_slice_timestamps,
)
from pipeline.tasks import (
    download_ofac,
    poll_gdelt_by_corridor,
    poll_gdelt_corridor,
    poll_gdelt_gkg,
    poll_rss,
)

SOURCES = {
    "gdelt": ("GDELT", None, "articles"),   # reported per corridor, see _poll_gdelt
    "rss": ("RSS feeds", poll_rss, "articles"),
    "ofac": ("OFAC SDN", download_ofac, "entities"),
}

# Opt-in only, never part of --source all: a 24h window is ~96 file downloads
# and roughly 300 MB, which is not something a default run should do silently.
GKG_SOURCE = "gkg"


class Command(BaseCommand):
    help = "Run the Phase 2 ingestion sources and report what was stored."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source", choices=[*SOURCES, GKG_SOURCE, "all"], default="all",
            help="Which source to poll (default: all). 'gkg' reads GDELT's bulk "
                 "15-minute files instead of the rate-limited DOC API — it cannot "
                 "be throttled, but downloads ~300 MB for a 24h window, so it is "
                 "never included in 'all'.",
        )
        parser.add_argument(
            "--corridor", choices=sorted(CORRIDOR_QUERIES),
            help="Poll ONE GDELT corridor in a single request, then stop. "
                 "Run the three separately, minutes apart, when GDELT is throttling.",
        )
        parser.add_argument(
            "--last-minutes", type=int, default=None, metavar="N",
            help=f"How far back each GDELT query looks, in minutes (default "
                 f"{DEFAULT_LAST_MINUTES} = 24h, must be a multiple of "
                 f"{LAST_MINUTES_GRANULARITY}). Widen it after a gap in polling, "
                 "e.g. 4320 for the last 3 days.",
        )
        parser.add_argument(
            "--max-records", type=int, default=None, metavar="N",
            help=f"Articles to request per GDELT corridor (default "
                 f"{DEFAULT_MAX_RECORDS}, GDELT caps at {GDELT_RECORD_CEILING}). "
                 "A smaller request may survive the rate limiter better.",
        )

    def handle(self, *args, **options):
        max_records = options["max_records"]
        if max_records is not None and not (1 <= max_records <= GDELT_RECORD_CEILING):
            raise CommandError(
                f"--max-records must be between 1 and {GDELT_RECORD_CEILING}, "
                f"got {max_records}"
            )

        last_minutes = options["last_minutes"]
        if last_minutes is not None and (
            last_minutes < LAST_MINUTES_GRANULARITY
            or last_minutes % LAST_MINUTES_GRANULARITY
        ):
            raise CommandError(
                f"--last-minutes must be a positive multiple of "
                f"{LAST_MINUTES_GRANULARITY}, got {last_minutes}"
            )

        if options["corridor"]:
            self._poll_one_corridor(options["corridor"], max_records, last_minutes)
            return

        if options["source"] == GKG_SOURCE:
            # A window inside the publication lag resolves to zero slices, which
            # would otherwise be reported as an unsampled window — technically
            # true but useless. Say what is actually wrong.
            if last_minutes is not None and last_minutes <= GKG_PUBLICATION_LAG:
                raise CommandError(
                    f"--last-minutes must exceed {GKG_PUBLICATION_LAG} for "
                    f"--source gkg: GDELT publishes its bulk files with a lag, so "
                    f"a shorter window contains no readable slices yet. Got "
                    f"{last_minutes}."
                )
            self._poll_gkg(last_minutes)
            return

        chosen = list(SOURCES) if options["source"] == "all" else [options["source"]]

        for key in chosen:
            if key == "gdelt":
                self._poll_gdelt(max_records, last_minutes)
                continue
            label, task, unit = SOURCES[key]
            count = task()
            style = self.style.SUCCESS if count else self.style.WARNING
            self.stdout.write(style(f"{label:12}: {count} new {unit}"))

        self.stdout.write(f"\nRawArticle rows total: {RawArticle.objects.count()}")

    def _poll_one_corridor(self, corridor, max_records=None, last_minutes=None):
        counts = poll_gdelt_corridor(
            corridor, max_records=max_records, last_minutes=last_minutes
        )

        if not counts["sampled"]:
            self.stdout.write(self.style.ERROR(
                f"{corridor:10}: NOT SAMPLED ({counts['status']}) - no answer from GDELT.\n"
                "  Nothing was stored, so the corpus is unchanged and still balanced.\n"
                "  Wait several minutes before retrying - each blocked request\n"
                "  extends GDELT's per-IP cooldown."
            ))
        elif not counts["fetched"]:
            self.stdout.write(self.style.WARNING(
                f"{corridor:10}: 0 articles (query answered, nothing matched) - "
                "this corridor is genuinely quiet."
            ))
        else:
            requested = max_records or DEFAULT_MAX_RECORDS
            # Hitting the cap exactly means GDELT had more to give and the
            # oldest matches in the window were silently dropped (sort=datedesc).
            truncated = " (hit the cap - there may be more)" if counts["fetched"] >= requested else ""
            self.stdout.write(self.style.SUCCESS(
                f"{corridor:10}: {counts['fetched']}/{requested} fetched, "
                f"{counts['stored']} new{truncated}"
            ))

        remaining = [c for c in sorted(CORRIDOR_QUERIES) if c != corridor]
        self.stdout.write(
            f"\nRawArticle rows total: {RawArticle.objects.count()}"
            f"\nStill to poll this cycle: {', '.join(remaining)}"
            "\n  Extracting before all three are sampled biases the corpus toward"
            "\n  whichever corridors answered - see the Phase 2.5 sampling fix."
        )

    def _poll_gkg(self, last_minutes=None):
        """Read GDELT's bulk 15-minute files instead of querying the DOC API.

        Reports per corridor in the same layout as --source gdelt, but the
        failure mode is different and the message says so: every corridor shares
        one download, so a bad run thins all three equally rather than starving
        one and biasing the ranking.
        """
        window = last_minutes or GKG_DEFAULT_LAST_MINUTES
        slices = len(gkg_slice_timestamps(window))
        self.stdout.write(
            f"Reading {slices} GKG slices ({window} min window, ~"
            f"{slices * 3} MB). This is not rate-limited but it is not quick."
        )

        report = poll_gdelt_gkg(last_minutes=last_minutes)
        total = sum(counts["stored"] for counts in report.values())

        style = self.style.SUCCESS if total else self.style.WARNING
        self.stdout.write(style(f"{'GDELT GKG':12}: {total} new articles"))

        for corridor, counts in sorted(report.items()):
            if not counts["sampled"]:
                self.stdout.write(self.style.ERROR(
                    f"  {corridor:10}: NOT SAMPLED ({counts['status']}) — too few "
                    "slices could be read"
                ))
            elif not counts["fetched"]:
                self.stdout.write(
                    f"  {corridor:10}: 0 articles (slices read, nothing matched)"
                )
            else:
                self.stdout.write(
                    f"  {corridor:10}: {counts['fetched']} matched, "
                    f"{counts['stored']} new"
                )

        if any(not counts["sampled"] for counts in report.values()):
            self.stdout.write(self.style.ERROR(
                "\n  WINDOW NOT SAMPLED: too few slices were readable, so an empty\n"
                "  corridor here is an artefact, not evidence that it is quiet.\n"
                "  Re-run, or widen --last-minutes to cover more slices."
            ))
        else:
            self.stdout.write(
                "\n  All corridors were filtered from the same slices, so their\n"
                "  risk scores are comparable with each other. They are NOT\n"
                "  directly comparable with DOC-API-sourced rows (source="
                f"'{GKG_SOURCE_LABEL}' marks the difference)."
            )

        self.stdout.write(f"\nRawArticle rows total: {RawArticle.objects.count()}")

    def _poll_gdelt(self, max_records=None, last_minutes=None):
        """GDELT runs one query per corridor and any single one can be throttled,
        so show the per-corridor breakdown rather than only a combined total."""
        report = poll_gdelt_by_corridor(
            max_records=max_records, last_minutes=last_minutes
        )
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

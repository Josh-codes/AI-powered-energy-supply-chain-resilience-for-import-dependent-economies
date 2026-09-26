"""Compare candidate risk-scoring formulas on the corpus already in the database.

    python manage.py compare_scoring
    python manage.py compare_scoring --scanned-per-day 61000
    python manage.py compare_scoring --as-of 2026-09-20T12:00:00Z

Read-only: no API calls, no writes, no network. Every candidate is scored on the
SAME clustered stories, so decay, the severity rubric and syndication dedup are
held constant and the only thing varying is the aggregation.

Why this exists
---------------
``raw_score`` is a SUM over stories, so it grows with how deeply the corpus was
sampled, not only with how dangerous the world is. Measured on this corpus: the
same crisis scored Hormuz 39.4 / Red Sea 45.1 from the GDELT DOC API alone and
Hormuz 115.6 / Red Sea 104.7 once GKG bulk ingestion was added — and the
corridor ranking inverted. Both corridors then normalized to ~0.99, leaving a
0.005 gap, which collapses Phase 4's risk-weighted capacity-loss magnitudes to
absurd values (Hormuz 0.009 mb/d on a corridor carrying 2.316 mb/d).

The three tables below are the three questions that settle the choice:

  1. does the statistic move when only the sampling depth changes?
  2. does it keep the corridors distinguishable, and leave a corridor with no
     events at its structural baseline?
  3. does it give Phase 4 back a non-degenerate capacity loss?
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand, CommandError

from core.models import Corridor, ExtractedEvent, RawArticle
from criticality.engine import compute_criticality
from graph.algorithms import baseline_max_flow, risk_weighted_max_flow
from graph.builder import build_graph
from graph.updater import update_edge_weights
from pipeline.ingest.gdelt import parse_seendate
from pipeline.score.candidates import CANDIDATES, Candidate, saturating, stat_sum
from pipeline.score.risk_scorer import (
    DECAY_LOOKBACK_DAYS,
    LAMBDA_DECAY,
    cluster_stories,
)

ALL = "ALL"


def _decayed_article_count(times, now, lambda_decay=LAMBDA_DECAY):
    """Denominator for the share candidates: the article pool weighted the same
    way the numerator's events are, so the two are on one time footing. A flat
    count would let an old, quiet week inflate today's share."""
    total = 0.0
    for t in times:
        delta_days = max(0.0, (now - t).total_seconds() / 86400.0)
        total += math.exp(-lambda_decay * delta_days)
    return total


class Command(BaseCommand):
    help = "Score the stored corpus under several candidate risk formulas and compare them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--as-of", help="ISO timestamp to decay against (default: now). The "
                            "documented figures were computed on 2026-09-20.",
        )
        parser.add_argument(
            "--scanned-per-day", type=float,
            help="Articles SCANNED per day, if known, for a true GPR-style share. "
                 "GKG reads ~61000 rows per 24h and stores only the ~113 that match; "
                 "that denominator is not persisted, so this is an estimate you supply.",
        )
        parser.add_argument(
            "--skip-criticality", action="store_true",
            help="Skip table 3, which builds a graph (persist=False, so the "
                 "GraphState singleton is left alone) and re-cuts it per candidate.",
        )

    # ------------------------------------------------------------------ data

    def _load(self, now):
        cutoff = now - timedelta(days=DECAY_LOOKBACK_DAYS)

        articles = {}   # url -> (source, article_time)
        for url, source, raw_text, ingested_at in RawArticle.objects.values_list(
            "url", "source", "raw_text", "ingested_at"
        ):
            articles[url] = (source, parse_seendate(raw_text) or ingested_at)

        # events[source][corridor] -> [(severity, confidence, timestamp, title)]
        events = defaultdict(lambda: defaultdict(list))
        unmatched = 0
        for corridor, url, sev, conf, ts, title in ExtractedEvent.objects.filter(
            corridor__isnull=False, timestamp__gte=cutoff
        ).values_list(
            "corridor__name", "article_url", "severity", "confidence", "timestamp", "title"
        ):
            entry = articles.get(url)
            if entry is None:
                # RawArticle rows are meant to be deleted at 14 days; an event
                # whose article is gone cannot be attributed to an ingestion
                # path, so it lands in ALL only.
                unmatched += 1
                source = None
            else:
                source = entry[0]
            events[source][corridor].append((sev, conf, ts, title))

        # article times per source, for the share denominators
        times = defaultdict(list)
        for source, article_time in articles.values():
            if article_time >= cutoff:
                times[source].append(article_time)

        return events, times, unmatched

    # ------------------------------------------------------------- reporting

    def handle(self, *args, **options):
        w = self.stdout.write
        ok, warn, err = self.style.SUCCESS, self.style.WARNING, self.style.ERROR

        if options["as_of"]:
            try:
                now = datetime.fromisoformat(options["as_of"].replace("Z", "+00:00"))
            except ValueError as exc:
                raise CommandError(f"--as-of is not an ISO timestamp: {exc}")
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        else:
            now = datetime.now(timezone.utc)

        corridors = list(Corridor.objects.order_by("name"))
        if not corridors:
            raise CommandError("no corridors in the database — run `manage.py seed_db`")

        events, times, unmatched = self._load(now)
        # Sources come from the ARTICLE pool, not the event pool: a source whose
        # articles were all judged irrelevant still belongs in the share
        # denominator — that is precisely what "as a share of total news" means.
        sources = sorted(set(times) | {s for s in events if s is not None})
        slices = sources + [ALL]
        # ...but events whose RawArticle is gone cannot be attributed to a
        # source, so they are folded into ALL rather than silently dropped.
        event_pool = {name: [name] for name in sources}
        event_pool[ALL] = sources + [None]

        candidates = list(CANDIDATES)
        if options["scanned_per_day"]:
            # A constant scan rate S integrated against the decay kernel is
            # S/lambda — the denominator that matches a decayed numerator.
            scanned_denominator = options["scanned_per_day"] / LAMBDA_DECAY
            candidates.append(Candidate(
                "share_scanned", "GPR share per 100 SCANNED (estimated)", "weight/100art",
                True, stat_sum, saturating(0.05),
                f"denominator = {options['scanned_per_day']:.0f} scanned/day / lambda, "
                f"held CONSTANT across slices so its table-1 ratio is uninformative; "
                f"K illustrative only",
            ))
        else:
            scanned_denominator = None

        # ---- gather stories and statistics -------------------------------
        # stories[slice][corridor] -> [Story]
        stories = defaultdict(dict)
        denominators = {}
        for name in slices:
            article_pool = sources if name == ALL else [name]
            denominators[name] = _decayed_article_count(
                [t for s in article_pool for t in times.get(s, [])], now
            )
            for corridor in corridors:
                raw_events = [
                    e for s in event_pool[name]
                    for e in events.get(s, {}).get(corridor.name, [])
                ]
                stories[name][corridor.name] = cluster_stories(raw_events, now=now)

        def statistic(candidate, slice_name, corridor_name):
            story_list = stories[slice_name][corridor_name]
            if candidate.key == "share_scanned":
                if not scanned_denominator:
                    return 0.0
                return 100.0 * stat_sum(story_list) / scanned_denominator
            denominator = denominators[slice_name] if candidate.needs_denominator else None
            return candidate.statistic(story_list, denominator)

        # ---- corpus summary ----------------------------------------------
        w(ok(f"\nCorpus (decayed as of {now.isoformat(timespec='seconds')}, "
             f"lookback {DECAY_LOOKBACK_DAYS}d, lambda={LAMBDA_DECAY})"))
        header = f"  {'slice':12} {'articles':>9} {'decayed':>9}  " + "  ".join(
            f"{c.name:>18}" for c in corridors
        )
        w(header)
        w("  " + "-" * (len(header) - 2))
        for name in slices:
            article_pool = sources if name == ALL else [name]
            n_articles = sum(len(times.get(s, [])) for s in article_pool)
            cells = []
            for c in corridors:
                n_events = sum(
                    len(events.get(s, {}).get(c.name, [])) for s in event_pool[name]
                )
                cells.append(f"{n_events:>7} ev {len(stories[name][c.name]):>5} st")
            w(f"  {name:12} {n_articles:>9} {denominators[name]:>9.1f}  " + "  ".join(cells))
        if unmatched:
            w(warn(f"  {unmatched} event(s) have no surviving RawArticle — counted in ALL only"))

        # ---- table 1: volume sensitivity ---------------------------------
        w(ok("\n[1] RAW STATISTIC BY INGESTION SLICE — does sampling depth move it?"))
        w("    ratio = ALL / widest single source. 1.00 means the statistic is")
        w("    insensitive to how much was ingested; the current formula is ~3x.")
        w("    The slices are different ingestion PATHS over one crisis, not different")
        w("    weeks, so a statistic that tracks the world should barely move across them.")
        widest = max(sources, key=lambda s: denominators[s]) if sources else None
        w(f"\n  {'candidate':34} {'corridor':10} "
          + " ".join(f"{s[:10]:>10}" for s in sources)
          + f" {'ALL':>10} {'ratio':>7}")
        for candidate in candidates:
            for i, corridor in enumerate(corridors):
                values = [statistic(candidate, s, corridor.name) for s in sources]
                total = statistic(candidate, ALL, corridor.name)
                base = statistic(candidate, widest, corridor.name) if widest else 0.0
                ratio = f"{total / base:>7.2f}" if base > 1e-9 else f"{'-':>7}"
                label = f"{candidate.key} ({candidate.unit})" if i == 0 else ""
                w(f"  {label:34} {corridor.name:10} "
                  + " ".join(f"{v:>10.3f}" for v in values)
                  + f" {total:>10.3f} {ratio}")
            w("")

        # ---- table 2: normalized scores on the full corpus ---------------
        w(ok("[2] NORMALIZED SCORE ON THE FULL CORPUS — distinguishable? baseline intact?"))
        w("    'gap' is the spread between the two scored corridors: too small and")
        w("    the ranking between them is noise. A corridor with 0 events must sit")
        w("    exactly at its baseline_risk, or the score is not absolute.")
        scored = {}
        w(f"\n  {'candidate':34} "
          + " ".join(f"{c.name:>10}" for c in corridors)
          + f" {'gap':>7}  note")
        for candidate in candidates:
            row = {}
            for corridor in corridors:
                raw = statistic(candidate, ALL, corridor.name)
                row[corridor.name] = candidate.normalize(raw, corridor.baseline_risk)
            scored[candidate.key] = row
            active = sorted(
                (v for c, v in row.items() if stories[ALL][c]), reverse=True
            )
            gap = active[0] - active[1] if len(active) > 1 else float("nan")
            line = (f"  {candidate.key:34} "
                    + " ".join(f"{row[c.name]:>10.4f}" for c in corridors)
                    + f" {gap:>7.4f}  {candidate.note}")
            w(warn(line) if gap < 0.05 else line)

        for corridor in corridors:
            if stories[ALL][corridor.name]:
                continue
            offenders = [
                k for k, row in scored.items()
                if abs(row[corridor.name] - corridor.baseline_risk) > 1e-9
            ]
            verdict = err("FAIL " + ", ".join(offenders)) if offenders else ok("all candidates OK")
            w(f"\n  baseline check — {corridor.name} has 0 events, must read "
              f"{corridor.baseline_risk:.3f}: {verdict}")

        # ---- table 3: what Phase 4 does with each ------------------------
        if options["skip_criticality"]:
            return

        w(ok("\n[3] PHASE 4 UNDER EACH CANDIDATE — is the capacity loss physical again?"))
        w("    The per-corridor loss is the ADDITIONAL flow lost by cutting that corridor")
        w("    on top of today's risk. Near-zero on a corridor carrying 2.3 mb/d means the")
        w("    score already closed it — mechanically correct, physically absurd. risk_flow")
        w("    is total deliverability against the static max-flow printed below.")
        base_graph = build_graph(persist=False)
        static_flow, _ = baseline_max_flow(base_graph)

        w(f"\n  {'candidate':20} {'risk_flow':>9} {'lost%':>6}  "
          + "  ".join(f"{c.name[:14]:>14}" for c in corridors))
        for candidate in candidates:
            G = base_graph.copy()
            update_edge_weights(G, scored[candidate.key])
            risk_flow, _ = risk_weighted_max_flow(G)
            rows = {r["corridor"]: r for r in compute_criticality(G)}
            cells = [
                f"{rows[c.name]['risk_rank']}/{rows[c.name]['rank_shift']:+d}/"
                f"{rows[c.name]['capacity_loss_mbd']:.3f}".rjust(14)
                for c in corridors
            ]
            lost = 100.0 * (static_flow - risk_flow) / static_flow
            w(f"  {candidate.key:20} {risk_flow:>9.3f} {lost:>5.1f}%  " + "  ".join(cells))
        w(f"\n  static (risk-free) max-flow = {static_flow:.3f} mb/d")
        w("  per-corridor cells: risk_rank / rank_shift / risk-weighted capacity loss (mb/d)")

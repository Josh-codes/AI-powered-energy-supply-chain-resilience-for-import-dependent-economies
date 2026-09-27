"""Backtest execution: reconstruct a historical window, score it, judge it.

Three stages, each run separately so the expensive ones are resumable and the
paid one is an explicit act:

1. **Pull** (:func:`pull_event`) — GDELT GKG slices for every day of the
   window, via ``gdelt_gkg.fetch_between``. One day at a time, stored after
   each day, recorded in a ledger (``data/backtests/<event>_pull.json``). A
   crash or a bad day costs that day, not the whole window; re-running skips
   days already sampled. Free, ~280 MB per day.
2. **Extract** — the ordinary ``manage.py extract_events``. PAID, and the only
   stage that is. Nothing here calls the LLM.
3. **Score + validate** (:func:`run_backtest`) — for each charted day, the
   PRODUCTION scoring functions evaluated point-in-time at that day's Brent
   assessment, then :func:`backtest.validator.validate`. Free, writes a report
   to ``data/backtests/<event>.json``. Writes NO ``RiskScore`` rows and never
   touches ``Corridor.live_risk_score``, so it cannot move the Thesis Snapshot.

Why backtest rows can share the live tables safely: the live score looks back
``DECAY_LOOKBACK_DAYS`` (180) from the real now. The latest window ends
2026-03-18, and 180 days on from that is 2026-09-14 — before this module
existed — so no live score computed from here on can see a backtest event.
(Moving an ``end`` past 2026-03-31 would break that for scores computed
today; check before extending a window.) ``/api/events/live/`` orders by event
timestamp, so they never surface in the dashboard feed either.
"""
import json
import logging
import os
from bisect import bisect_left, bisect_right
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from django.conf import settings

from backtest import eia
from backtest.validator import (
    SIGNAL_THRESHOLD,
    SPIKE_SESSION_PCT,
    TOLERANCE_DAYS,
    validate,
)
from core.models import Corridor, ExtractedEvent, RawArticle
from pipeline.ingest import gdelt_gkg
from pipeline.ingest.gdelt import FETCH_ERROR, parse_seendate
from pipeline.score.risk_scorer import (
    DECAY_LOOKBACK_DAYS,
    LAMBDA_DECAY,
    TOP_K_STORIES,
    StoryClusterer,
    cluster_stories,
    corridor_events,
    event_weight,
    normalize_severity,
    top_k_severity,
)

logger = logging.getLogger(__name__)

BACKTEST_DIR = Path(settings.BASE_DIR) / "data" / "backtests"

#: Each charted day is scored as of this instant (UTC). Brent spot is assessed
#: around the London close, 16:30 local, which is 16:30 UTC until BST begins
#: on the last Sunday of March. Scoring here means a day's signal uses exactly
#: the news published before that day's price was set, and no later — the
#: fairest comparison, and it cannot flatter the lead time.
SCORE_TIME_UTC = time(16, 30)

#: Days pulled before ``chart_start`` so the first charted score has history
#: behind it. A story a week old still carries e^(-0.7) = 50% of its weight;
#: without this warm-up the first charted days would read low for want of
#: data, and the "calm baseline" would be an artefact. By the time the signal
#: could plausibly cross the threshold, anything older than the warm-up has
#: decayed below e^(-2.1) = 12%.
WARMUP_DAYS = 7

#: Named events. ``end`` is inclusive. Dates come from the EIA series itself
#: (``backtest.eia.find_run_up`` / ``largest_moves``), not from memory.
BACKTEST_EVENTS = {
    "2026_hormuz_closure": {
        "name": "2026 Strait of Hormuz closure",
        "corridor": "Hormuz",
        # WIDENED on 2026-09-28, after the first result, from 2026-02-11. That
        # window was meant to open on calm and did not: Hormuz already read
        # 0.710 on its first day, and its PASSED verdict (15-day lead) rested
        # on a single day's decay dipping 0.022 below the bar. The first report
        # is archived as data/backtests/2026_hormuz_closure.window_feb11.json.
        # Jan 15 was the first idea, but Brent climbed 18% from Jan 7 to Jan
        # 30 ($61.08 -> $72.25), so the flat stretch is mid-Dec to Jan 7.
        "chart_start": date(2026, 1, 1),
        "end": date(2026, 3, 18),
        # Measured: $70.69 on 2026-02-25 -> $118.09 on 2026-03-18, 21 days.
        "run_up_usd": (69.0, 114.0),
        "note": "Onset trough 2026-02-25; first >=5% session 2026-03-02 "
                "(+8.30%); ends on the first close above $114. Peak "
                "$138.21 on 2026-04-07 is outside the window by design. "
                "Chart widened from 2026-02-11 to 2026-01-01 after the first "
                "run (see runner.py); January itself holds a gradual +18% "
                "Brent climb with no single >=5% session.",
    },
    "2025_iran_standoff": {
        "name": "2025 US-Iran standoff",
        "corridor": "Hormuz",
        "chart_start": date(2025, 5, 30),
        "end": date(2025, 6, 27),
        "run_up_usd": None,
        "note": "Largest 2025 session: 2025-06-13, +7.28% (CLAUDE.md's '+8%' "
                "was approximate). The move faded within weeks, so this "
                "window also tests whether the signal falls back.",
    },
}

DAY_OK = "ok"
DAY_NOT_SAMPLED = "not_sampled"
DAY_ERROR = "error"

VERDICT_INCOMPLETE = "INCOMPLETE"
VERDICT_NO_PRICE_DATA = "NO_PRICE_DATA"


def get_event(key):
    try:
        return BACKTEST_EVENTS[key]
    except KeyError:
        raise ValueError(
            f"unknown backtest event {key!r}; known: {', '.join(BACKTEST_EVENTS)}"
        ) from None


def pull_start(event):
    return event["chart_start"] - timedelta(days=WARMUP_DAYS)


def _days(first, last):
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def pull_days(event):
    """Every day whose GKG slices the backtest needs: warm-up + charted."""
    return _days(pull_start(event), event["end"])


def chart_days(event):
    return _days(event["chart_start"], event["end"])


def _as_of(day):
    return datetime.combine(day, SCORE_TIME_UTC, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Pull ledger
# ---------------------------------------------------------------------------

def ledger_path(key):
    return BACKTEST_DIR / f"{key}_pull.json"


def report_path(key):
    return BACKTEST_DIR / f"{key}.json"


def load_ledger(key):
    """``{"event", "days": {iso_date: entry}}``. A missing or corrupt ledger
    reads as empty, which only means days get re-pulled — never lost, since
    storage dedupes by URL."""
    path = ledger_path(key)
    if path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                ledger = json.load(fh)
            ledger.setdefault("days", {})
            return ledger
        except (OSError, ValueError) as exc:
            logger.warning("pull ledger %s unreadable, starting fresh: %s", path, exc)
    return {"event": key, "days": {}}


def _write_json(path, payload):
    """Write via a temp file + replace, so an interrupted write never leaves a
    truncated ledger or report behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    os.replace(tmp, path)


def save_ledger(key, ledger):
    _write_json(ledger_path(key), ledger)


# ---------------------------------------------------------------------------
# Stage 1: pull
# ---------------------------------------------------------------------------

def pull_day(day, workers=4, timeout=60):
    """Fetch and store one UTC day of GKG slices. Returns a ledger entry.

    Never raises: an unexpected failure becomes a ``DAY_ERROR`` entry so the
    rest of the window still runs (CLAUDE.md rule 5).
    """
    start = datetime.combine(day, time(0, 0), tzinfo=timezone.utc)
    entry = {"pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        window = gdelt_gkg.fetch_between(
            start, start + timedelta(days=1), timeout=timeout, workers=workers
        )
        entry["slices_read"] = window.slices_read
        entry["slices_total"] = window.slices_total
        if any(f.status == FETCH_ERROR for f in window.corridors.values()):
            entry["status"] = DAY_NOT_SAMPLED
            entry["matched"] = {}
            entry["stored"] = 0
            return entry
        articles = [a for f in window.corridors.values() for a in f.articles]
        entry["matched"] = {c: len(f.articles) for c, f in window.corridors.items()}
        entry["stored"] = gdelt_gkg.store_gkg_articles(articles)
        entry["status"] = DAY_OK
    except Exception as exc:  # noqa: BLE001 - one bad day must not end the pull
        logger.exception("backtest pull failed for %s", day)
        entry["status"] = DAY_ERROR
        entry["error"] = str(exc)
    return entry


def pull_event(key, days=None, force=False, workers=4, timeout=60, on_day=None):
    """Pull every day of *key*'s window not already sampled.

    ``days`` restricts to specific dates (must lie inside the window);
    ``force`` re-pulls days the ledger marks ok. ``on_day(day, entry)`` is
    called after each day, so a command can print progress. The ledger is
    saved after EVERY day, which is what makes the pull resumable.
    """
    event = get_event(key)
    window = pull_days(event)
    if days:
        outside = [d for d in days if d not in window]
        if outside:
            raise ValueError(
                f"{', '.join(map(str, outside))} outside {key}'s pull window "
                f"{window[0]}..{window[-1]}"
            )
        todo = sorted(set(days))
    else:
        todo = window

    ledger = load_ledger(key)
    summary = {"pulled": 0, "skipped": 0, "ok": 0, "not_sampled": 0, "error": 0}
    for day in todo:
        iso = day.isoformat()
        if not force and ledger["days"].get(iso, {}).get("status") == DAY_OK:
            summary["skipped"] += 1
            continue
        entry = pull_day(day, workers=workers, timeout=timeout)
        ledger["days"][iso] = entry
        save_ledger(key, ledger)
        summary["pulled"] += 1
        summary[entry["status"]] += 1
        if on_day:
            on_day(day, entry)
    return summary


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def pending_in_scope(event):
    """Unextracted RawArticle rows that could still change this event's series.

    Precise rather than global: a row matters only if its seendate falls where
    some charted day's lookback can see it — between the first charted day's
    lookback cutoff and the window's end. A pending live article from
    September cannot move a March score, so it must not block the verdict.
    """
    lo = _as_of(event["chart_start"]) - timedelta(days=DECAY_LOOKBACK_DAYS)
    hi = _as_of(event["end"])
    count = 0
    for raw_text in RawArticle.objects.filter(processed=False).values_list(
        "raw_text", flat=True
    ):
        seen = parse_seendate(raw_text)
        if seen is not None and lo <= seen <= hi:
            count += 1
    return count


def coverage(key):
    """How much of the window the ledger says was actually sampled."""
    event = get_event(key)
    ledger = load_ledger(key)["days"]
    expected = [d.isoformat() for d in pull_days(event)]
    ok = [d for d in expected if ledger.get(d, {}).get("status") == DAY_OK]
    missing = [d for d in expected if d not in ok]
    return {
        "days_expected": len(expected),
        "days_ok": len(ok),
        "days_missing": missing,
        "slices_read": sum(ledger.get(d, {}).get("slices_read", 0) for d in ok),
        "slices_total": sum(ledger.get(d, {}).get("slices_total", 0) for d in ok),
        "articles_stored": sum(ledger.get(d, {}).get("stored", 0) for d in ok),
        "complete": not missing,
    }


def event_counts(event):
    """ExtractedEvents per corridor dated inside the pull window."""
    lo = datetime.combine(pull_start(event), time(0, 0), tzinfo=timezone.utc)
    hi = _as_of(event["end"])
    counts = {c: 0 for c in Corridor.objects.values_list("name", flat=True)}
    for name in ExtractedEvent.objects.filter(
        timestamp__gte=lo, timestamp__lte=hi, corridor__isnull=False
    ).values_list("corridor__name", flat=True):
        counts[name] = counts.get(name, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Stage 3: score + validate
# ---------------------------------------------------------------------------

def _story_row(story):
    return {
        "title": story.title,
        "weight": round(story.weight, 3),
        "severity": story.severity,
        "confidence": story.confidence,
        "timestamp": story.timestamp.isoformat(),
        "members": story.members,
    }


def _summarize(stories, as_of, baseline, n_events, with_top):
    """One corridor-day from its stories, weighted as of *as_of*."""
    weighted = [
        s._replace(weight=event_weight(s.severity, s.confidence, s.timestamp, as_of,
                                       LAMBDA_DECAY))
        for s in stories
    ]
    raw = top_k_severity(weighted, k=TOP_K_STORIES)
    out = {
        "raw": round(raw, 4),
        "score": round(normalize_severity(raw, baseline_risk=baseline), 4),
        "events": n_events,
        "stories": len(weighted),
    }
    if with_top:
        top = sorted(weighted, key=lambda s: s.weight, reverse=True)[:TOP_K_STORIES]
        out["top_stories"] = [_story_row(s) for s in top]
    return out


def _row(day, per_corridor, focus):
    row = {"date": day, "as_of": _as_of(day).isoformat(),
           "scores": {}, "raw": {}, "stories": {}, "events": {}}
    for name, cell in per_corridor.items():
        row["scores"][name] = cell["score"]
        row["raw"][name] = cell["raw"]
        row["stories"][name] = cell["stories"]
        row["events"][name] = cell["events"]
        if name == focus:
            row["top_stories"] = cell["top_stories"]
    return row


def score_day(day, corridors, focus=None):
    """Point-in-time scores for every corridor on *day*, computed from scratch.

    Composes exactly what ``compute_corridor_severity`` does (a test pins
    equality). The runner does not call this per day any more — see
    :func:`corridor_series` — but it is the independent reference the
    incremental series is tested against, and the simplest statement of what
    each charted day means.
    """
    as_of = _as_of(day)
    cells = {}
    for name, baseline in corridors:
        events = corridor_events(name, now=as_of, exclude_future=True)
        stories = cluster_stories(events, now=as_of, lambda_decay=LAMBDA_DECAY)
        cells[name] = _summarize(stories, as_of, baseline, len(events), name == focus)
    return _row(day, cells, focus)


def corridor_series(name, baseline, days, with_top=False):
    """Point-in-time scores for one corridor on every one of *days*, in one
    incremental pass instead of one clustering per day.

    Exact, not an approximation. Production scores day D from the events in
    ``[as_of - 180 days, as_of]``, in date order (``corridor_events``), and
    clustering a date-ordered list is the same as clustering its prefix and
    carrying on. So while the lookback's START stays put, day D+1's clusters
    are day D's plus that day's events. The start moves only when the 180-day
    edge passes an event, so days are grouped by where their window starts and
    each group gets one pass — in practice one or a handful, not 36.

    One pass matters: re-clustering the Hormuz window from scratch per day was
    measured at 112 s for 2026-03-05 alone (2,652 events), growing roughly
    quadratically to 14,483 events by 03-18.

    Each cluster's strongest member is chosen with weights as of the group's
    last day; for events not after it, that choice is the same as-of any
    earlier day (see ``StoryClusterer``). Weights are then recomputed as of
    each day before the top-3 is taken.
    """
    if not days:
        return []
    as_ofs = [_as_of(d) for d in days]
    span = (as_ofs[-1] - as_ofs[0]).days
    events = corridor_events(
        name, now=as_ofs[-1], lookback_days=DECAY_LOOKBACK_DAYS + span,
        exclude_future=True,
    )
    times = [e[2] for e in events]
    lookback = timedelta(days=DECAY_LOOKBACK_DAYS)
    starts = [bisect_left(times, a - lookback) for a in as_ofs]
    ends = [bisect_right(times, a) for a in as_ofs]

    cells = [None] * len(days)
    for start in sorted(set(starts)):
        group = [k for k in range(len(days)) if starts[k] == start]
        clusterer = StoryClusterer(now=as_ofs[group[-1]], lambda_decay=LAMBDA_DECAY)
        i = start
        for k in group:
            while i < ends[k]:
                clusterer.add(*events[i])
                i += 1
            cells[k] = _summarize(clusterer.stories(), as_ofs[k], baseline,
                                  ends[k] - start, with_top)
    return cells


def score_series(key, on_day=None):
    event = get_event(key)
    corridors = list(Corridor.objects.order_by("name").values_list("name", "baseline_risk"))
    if not corridors:
        raise ValueError("no corridors in the database - run `manage.py seed_db`")
    days = chart_days(event)
    focus = event["corridor"]
    by_corridor = {
        name: corridor_series(name, baseline, days, with_top=(name == focus))
        for name, baseline in corridors
    }
    series = []
    for k, day in enumerate(days):
        row = _row(day, {name: cells[k] for name, cells in by_corridor.items()}, focus)
        series.append(row)
        if on_day:
            on_day(row)
    return series


def _price_window(event):
    """Brent from a week before the pull starts, so the first charted day's
    session return is computable."""
    return pull_start(event) - timedelta(days=7), event["end"]


def run_backtest(key, write=True, on_day=None):
    """Score the window, validate it, and (by default) write the report.

    The verdict is downgraded to INCOMPLETE — whatever the series says — if
    any pull day is missing or any in-scope article is still unextracted:
    a missing day scores as quiet, and quiet days are exactly what would
    fake a clean "calm, then rise" shape.
    """
    event = get_event(key)
    series = score_series(key, on_day=on_day)

    first, last = _price_window(event)
    try:
        prices = eia.fetch_brent_prices(first, last)
    except eia.EIAConfigurationError as exc:
        logger.error("no Brent data: %s", exc)
        prices = []
    for row in series:
        session = next((p for p in prices if p["date"] == row["date"]), None)
        row["brent_usd"] = session["price_usd"] if session else None

    cov = coverage(key)
    pending = pending_in_scope(event)

    if prices:
        verdict = validate(
            series, prices, event["corridor"], event["chart_start"], event["end"],
        )
    else:
        verdict = {"validation": VERDICT_NO_PRICE_DATA,
                   "reason": "no Brent series (no EIA key and no cache)"}

    raw_verdict = verdict["validation"]
    problems = []
    if not cov["complete"]:
        problems.append(f"{len(cov['days_missing'])} pull day(s) not sampled")
    if pending:
        problems.append(f"{pending} in-scope article(s) not yet extracted")
    if problems and raw_verdict != VERDICT_NO_PRICE_DATA:
        verdict["validation_if_complete"] = raw_verdict
        verdict["validation"] = VERDICT_INCOMPLETE
        verdict["reason"] = "; ".join(problems) + f" (series alone says {raw_verdict})"

    run_up = None
    if prices and event.get("run_up_usd"):
        lo_usd, hi_usd = event["run_up_usd"]
        run_up = eia.find_run_up(
            [p for p in prices if pull_start(event) <= p["date"] <= event["end"]],
            lo_usd, hi_usd,
        )

    report = {
        "event": key,
        "name": event["name"],
        "note": event["note"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": {
            "score_time_utc": SCORE_TIME_UTC.isoformat(),
            "point_in_time": True,
            "top_k_stories": TOP_K_STORIES,
            "lambda_decay": LAMBDA_DECAY,
            "lookback_days": DECAY_LOOKBACK_DAYS,
            "warmup_days": WARMUP_DAYS,
            "signal_threshold": SIGNAL_THRESHOLD,
            "tolerance_days": TOLERANCE_DAYS,
            "spike_session_pct": SPIKE_SESSION_PCT,
            "price_series": f"EIA {eia.BRENT_SERIES} (Europe Brent Spot FOB, daily)",
        },
        "window": {
            "pull_start": pull_start(event),
            "chart_start": event["chart_start"],
            "end": event["end"],
        },
        "coverage": cov,
        "pending_extraction": pending,
        "events_in_window": event_counts(event),
        "run_up": run_up,
        **verdict,
        "signal_elevated_days_before": verdict.get("lead_time_days"),
        "series": series,
    }
    if write:
        _write_json(report_path(key), report)
    return report


def load_report(key):
    """The last written report for *key*, or None if it has not been run."""
    get_event(key)
    path = report_path(key)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)

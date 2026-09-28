"""Phase 7 backtest: did the risk signal rise before Brent did?

Three steps, run in this order. Only step 2 costs money.

    # 1. PULL historical GKG slices, one UTC day at a time (free, ~280 MB/day).
    #    Resumable: re-running skips days already sampled.
    python manage.py run_backtest --event 2026_hormuz_closure --pull
    python manage.py run_backtest --event 2026_hormuz_closure --pull --day 2026-02-20

    # 2. EXTRACT the pulled articles (PAID; the ordinary command).
    python manage.py extract_events

    # 3. SCORE + VALIDATE (free). Writes data/backtests/<event>.json, and NO
    #    RiskScore rows: the Thesis Snapshot cannot move.
    python manage.py run_backtest --event 2026_hormuz_closure

    python manage.py run_backtest --list
    python manage.py run_backtest --event 2026_hormuz_closure --status
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from backtest import runner
from backtest.validator import (
    FAILED,
    INCONCLUSIVE,
    NO_PRICE_EVENT,
    PASSED,
    SIGNAL_THRESHOLD,
)


class Command(BaseCommand):
    help = "Backtest the risk signal against historical Brent prices (Phase 7)."

    def add_arguments(self, parser):
        parser.add_argument("--event", help=f"One of: {', '.join(runner.BACKTEST_EVENTS)}.")
        parser.add_argument("--list", action="store_true", help="List the defined events.")
        parser.add_argument("--status", action="store_true",
                            help="Show pull coverage and extraction progress; changes nothing.")
        parser.add_argument("--pull", action="store_true",
                            help="Download GKG slices for the window (free, ~280 MB/day).")
        parser.add_argument("--day", action="append", default=[], metavar="YYYY-MM-DD",
                            help="With --pull: only this day (repeatable).")
        parser.add_argument("--force", action="store_true",
                            help="With --pull: re-pull days already marked ok.")
        parser.add_argument("--workers", type=int, default=4,
                            help="With --pull: concurrent slice downloads (default 4).")
        parser.add_argument("--no-write", action="store_true",
                            help="Score and print, but do not write the report file.")

    def handle(self, *args, **options):
        if options["list"]:
            return self._list()
        key = options["event"]
        if not key:
            raise CommandError("--event is required (or use --list)")
        try:
            event = runner.get_event(key)
        except ValueError as exc:
            raise CommandError(str(exc))

        if options["status"]:
            return self._status(key, event)
        if options["pull"]:
            return self._pull(key, event, options)
        if options["day"] or options["force"]:
            raise CommandError("--day / --force only apply with --pull")
        return self._score(key, event, write=not options["no_write"])

    # ------------------------------------------------------------------ list

    def _list(self):
        w = self.stdout.write
        for key, ev in runner.BACKTEST_EVENTS.items():
            report = runner.load_report(key)
            verdict = report["validation"] if report else "not run"
            w(f"{key}")
            w(f"    {ev['name']} ({ev['corridor']})")
            w(f"    pull {runner.pull_start(ev)} .. chart {ev['chart_start']} .. {ev['end']}"
              f"   [{len(runner.pull_days(ev))} days]   verdict: {verdict}")

    # ---------------------------------------------------------------- status

    def _status(self, key, event):
        w = self.stdout.write
        ok, warn = self.style.SUCCESS, self.style.WARNING
        cov = runner.coverage(key)
        w(self.style.MIGRATE_HEADING(f"{event['name']} - status"))
        w(f"  pull window   {runner.pull_start(event)} .. {event['end']}")
        style = ok if cov["complete"] else warn
        w(style(f"  days pulled   {cov['days_ok']} / {cov['days_expected']}"))
        if cov["slices_total"]:
            w(f"  slices read   {cov['slices_read']} / {cov['slices_total']}")
        w(f"  articles      {cov['articles_stored']} stored")
        if cov["days_missing"]:
            shown = ", ".join(cov["days_missing"][:8])
            more = f" (+{len(cov['days_missing']) - 8} more)" if len(cov["days_missing"]) > 8 else ""
            w(warn(f"  missing       {shown}{more}"))
        pending = runner.pending_in_scope(event)
        w((warn if pending else ok)(f"  to extract    {pending} article(s)"))
        counts = runner.event_counts(event)
        w("  events        " + ", ".join(f"{c} {n}" for c, n in sorted(counts.items())))
        w("")
        if not cov["complete"]:
            w(f"  next: python manage.py run_backtest --event {key} --pull")
        elif pending:
            w("  next: python manage.py extract_events        (PAID)")
        else:
            w(f"  next: python manage.py run_backtest --event {key}")

    # ------------------------------------------------------------------ pull

    def _pull(self, key, event, options):
        w = self.stdout.write
        ok, warn, err = self.style.SUCCESS, self.style.WARNING, self.style.ERROR
        try:
            days = [date.fromisoformat(d) for d in options["day"]]
        except ValueError as exc:
            raise CommandError(f"--day must be YYYY-MM-DD: {exc}")
        if options["workers"] < 1:
            raise CommandError("--workers must be at least 1")

        n = len(days) if days else len(runner.pull_days(event))
        w(self.style.MIGRATE_HEADING(
            f"Pulling GKG for {event['name']}: up to {n} day(s), "
            f"~{n * 280 / 1024:.1f} GB. Safe to interrupt; re-run to resume."
        ))

        def progress(day, entry):
            status = entry["status"]
            if status == runner.DAY_OK:
                matched = ", ".join(f"{c} {k}" for c, k in sorted(entry["matched"].items()))
                w(ok(f"  {day}  ok   {entry['slices_read']:>3}/{entry['slices_total']} slices"
                     f"   stored {entry['stored']:>4}   ({matched})"))
            elif status == runner.DAY_NOT_SAMPLED:
                w(warn(f"  {day}  NOT SAMPLED  only {entry['slices_read']}/"
                       f"{entry['slices_total']} slices readable - re-run later"))
            else:
                w(err(f"  {day}  ERROR  {entry.get('error', '')}"))

        try:
            summary = runner.pull_event(
                key, days=days or None, force=options["force"],
                workers=options["workers"], on_day=progress,
            )
        except ValueError as exc:
            raise CommandError(str(exc))

        w("")
        w(f"  pulled {summary['pulled']}, skipped {summary['skipped']} already ok"
          f"  |  ok {summary['ok']}, not sampled {summary['not_sampled']}, error {summary['error']}")
        cov = runner.coverage(key)
        if cov["complete"]:
            w(ok(f"  window complete: {cov['days_ok']}/{cov['days_expected']} days."))
            w("  next: python manage.py extract_events        (PAID)")
        else:
            w(warn(f"  {len(cov['days_missing'])} day(s) still missing - re-run the same "
                   f"command to retry only those."))

    # ----------------------------------------------------------------- score

    def _score(self, key, event, write):
        w = self.stdout.write
        ok, warn, err = self.style.SUCCESS, self.style.WARNING, self.style.ERROR
        focus = event["corridor"]
        w(self.style.MIGRATE_HEADING(
            f"{event['name']}: point-in-time scores at {runner.SCORE_TIME_UTC:%H:%M} UTC"
        ))
        w(f"  {'date':<10}  {'Brent':>7}  {'Cape':>6}  {'Hormuz':>6}  {'RedSea':>6}"
          f"  {focus + ' stories':>15}")

        report = runner.run_backtest(key, write=write)
        for row in report["series"]:
            brent = f"{row['brent_usd']:7.2f}" if row.get("brent_usd") is not None else f"{'-':>7}"
            s = row["scores"]
            marks = []
            if row["date"] == report.get("price_spiked_at"):
                marks.append("<- PRICE EVENT")
            if row["date"] == report.get("signal_elevated_at"):
                marks.append(f"<- SIGNAL > {SIGNAL_THRESHOLD}")
            if not row.get("day_sampled", True):
                marks.append("[no data: decay only]")
            elif row.get("unsampled_days_in_lookback"):
                marks.append("[lower bound]")
            w(f"  {row['date']}  {brent}  {s.get('Cape', 0):6.3f}  {s.get('Hormuz', 0):6.3f}"
              f"  {s.get('Red Sea', 0):6.3f}  {row['stories'].get(focus, 0):>15}  {' '.join(marks)}")

        w("")
        verdict = report["validation"]
        style = {PASSED: ok, FAILED: err, INCONCLUSIVE: warn, NO_PRICE_EVENT: warn}.get(verdict, warn)
        w(style(f"  VERDICT: {verdict}"))
        w(f"  {report.get('reason')}")
        if report.get("price_spiked_at"):
            w(f"  price event   {report['price_spiked_at']}  Brent {report['brent_before_usd']:.2f}"
              f" -> {report['brent_after_usd']:.2f}  ({report['brent_spike_pct']:+.2f}%)")
        if report.get("lead_time_days") is not None:
            bound = " (lower bound)" if report.get("lead_time_is_lower_bound") else ""
            w(f"  lead time     {report['lead_time_days']} day(s){bound}")
        if report.get("max_risk_score") is not None:
            w(f"  peak {focus}   {report['max_risk_score']:.3f} on {report['max_risk_at']}")
        for fa in report.get("false_alarms", []):
            w(warn(f"  false alarm   {fa['start']}..{fa['end']} (peak {fa['peak']:.3f})"
                   f" - above {SIGNAL_THRESHOLD}, then fell back before the price moved"))
        if report.get("run_up"):
            ru = report["run_up"]
            w(f"  run-up        {ru['start']} ${ru['start_usd']:.2f} -> {ru['peak']}"
              f" ${ru['peak_usd']:.2f}  ({ru['pct_change']:+.1f}% in {ru['days']} days)")
        for gap in report.get("gaps", []):
            w(warn(f"  source gap    {gap['start']}..{gap['end']} ({gap['days']} day(s) unsampled)"
                   f" - scores there are decay only; later scores are lower bounds"))
        if verdict == runner.VERDICT_INCOMPLETE:
            w(warn(f"  run `python manage.py run_backtest --event {key} --status` for what is missing"))
        if write:
            w(f"  report        {runner.report_path(key)}")

"""Tests for backtest/validator.py, backtest/runner.py, the GKG range fetch,
and ``manage.py run_backtest``.

Mocking: GKG slices are mocked at ``pipeline.ingest.gdelt_gkg.fetch_slice``
(one level above requests, since these tests are about windowing and order,
not HTTP); the runner's pull is mocked at ``gdelt_gkg.fetch_between``; Brent
at ``backtest.runner.eia.fetch_brent_prices``. ``BACKTEST_DIR`` is always a
temp dir. No test touches the network, the LLM, or data/backtests/.

The load-bearing tests are the ones about HONESTY, not arithmetic:
``test_backtest_never_writes_risk_scores_or_moves_live_risk`` (the Thesis
Snapshot cannot move), ``test_future_event_does_not_leak_into_a_past_day``
(no lookahead), ``test_early_blip_is_a_false_alarm_not_a_lead`` and
``test_already_elevated_at_start_is_inconclusive`` (the verdict cannot flatter).
"""
import json
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.gis.geos import LineString
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from backtest import runner, validator
from core.models import Corridor, ExtractedEvent, RawArticle, RiskScore
from pipeline.ingest import gdelt_gkg
from pipeline.ingest.gdelt import FETCH_EMPTY, FETCH_ERROR, FETCH_OK, CorridorFetch
from pipeline.score.risk_scorer import (
    cluster_stories,
    compute_corridor_severity,
    corridor_events,
    normalize_severity,
    top_k_severity,
)

UTC = timezone.utc
BASELINES = {"Hormuz": 0.20, "Red Sea": 0.15, "Cape": 0.05}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _series(start, scores, corridor="Hormuz"):
    """Consecutive daily rows from *start* with the given focus scores."""
    return [
        {"date": start + timedelta(days=i), "scores": {corridor: s}}
        for i, s in enumerate(scores)
    ]


def _prices(start, values):
    """One session per calendar day — enough for validator arithmetic."""
    return [{"date": start + timedelta(days=i), "price_usd": v} for i, v in enumerate(values)]


def _flat_then_jump(start, n_days, jump_day, base=70.0, jumped=77.0):
    """Flat Brent with a single +10% session on ``start + jump_day``."""
    values = [base] * n_days
    for i in range(jump_day, n_days):
        values[i] = jumped
    return _prices(start, values)


def _make_corridors():
    line = LineString((56.0, 26.0), (57.0, 27.0))
    for name, baseline in BASELINES.items():
        Corridor.objects.create(
            name=name, geometry=line, capacity_mbd=10.0, transit_days=10,
            baseline_risk=baseline, live_risk_score=0.5,
        )


def _unique_title(seed):
    import random
    import string
    rng = random.Random(seed)
    return " ".join("".join(rng.choices(string.ascii_lowercase, k=7)) for _ in range(5))


def _event(corridor, when, severity=5, confidence=1.0, seed=0, title=None):
    return ExtractedEvent.objects.create(
        corridor=Corridor.objects.get(name=corridor), actor="Iran",
        event_type="military", severity=severity, confidence=confidence,
        timestamp=when, article_url=f"https://example.com/{seed}",
        title=_unique_title(seed) if title is None else title,
    )


class _TempBacktestDir:
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(runner, "BACKTEST_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# validator — pure
# ---------------------------------------------------------------------------

class FindPriceEventTests(SimpleTestCase):
    START = date(2026, 2, 1)

    def test_first_qualifying_session_in_window(self):
        prices = _prices(self.START, [70, 70, 74, 70, 80])   # +5.7% day 2, +14% day 4
        move = validator.find_price_event(prices, self.START, self.START + timedelta(days=4))
        self.assertEqual(move["date"], self.START + timedelta(days=2))

    def test_sessions_outside_the_window_are_ignored(self):
        prices = _prices(self.START, [70, 80, 80, 80])       # +14% on day 1
        move = validator.find_price_event(
            prices, self.START + timedelta(days=2), self.START + timedelta(days=3)
        )
        self.assertIsNone(move)

    def test_below_threshold_is_not_an_event(self):
        prices = _prices(self.START, [70, 73.4])               # +4.9%
        self.assertIsNone(validator.find_price_event(prices, self.START, self.START + timedelta(days=1)))


class ExcursionTests(SimpleTestCase):
    START = date(2026, 2, 1)

    def test_runs_and_peaks(self):
        runs = validator.excursions(
            _series(self.START, [0.3, 0.7, 0.8, 0.5, 0.65, 0.3]), "Hormuz"
        )
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["start"], self.START + timedelta(days=1))
        self.assertEqual(runs[0]["days"], 2)
        self.assertEqual(runs[0]["peak"], 0.8)
        self.assertEqual(runs[1]["days"], 1)

    def test_threshold_is_strictly_above(self):
        # CLAUDE.md: "> 0.6". Exactly 0.6 is not elevated.
        self.assertEqual(validator.excursions(_series(self.START, [0.6, 0.6]), "Hormuz"), [])

    def test_run_still_open_at_the_end_is_closed(self):
        runs = validator.excursions(_series(self.START, [0.3, 0.7, 0.7]), "Hormuz")
        self.assertEqual(runs[0]["end"], self.START + timedelta(days=2))


class ValidateTests(SimpleTestCase):
    START = date(2026, 2, 11)
    DAYS = 20
    SPIKE = 10          # price jumps on START + 10

    def _run(self, scores):
        series = _series(self.START, scores)
        prices = _flat_then_jump(self.START - timedelta(days=1), self.DAYS + 1, self.SPIKE + 1)
        return validator.validate(
            series, prices, "Hormuz", self.START, self.START + timedelta(days=self.DAYS - 1)
        )

    def _scores(self, cross_on=None, from_=0.3, to=0.8):
        return [to if cross_on is not None and i >= cross_on else from_ for i in range(self.DAYS)]

    def test_signal_before_price_passes_with_positive_lead(self):
        r = self._run(self._scores(cross_on=7))
        self.assertEqual(r["validation"], validator.PASSED)
        self.assertEqual(r["price_spiked_at"], self.START + timedelta(days=self.SPIKE))
        self.assertEqual(r["signal_elevated_at"], self.START + timedelta(days=7))
        self.assertEqual(r["lead_time_days"], 3)
        self.assertAlmostEqual(r["brent_spike_pct"], 10.0)

    def test_signal_within_tolerance_after_price_passes_but_says_so(self):
        r = self._run(self._scores(cross_on=self.SPIKE + 2))
        self.assertEqual(r["validation"], validator.PASSED)
        self.assertEqual(r["lead_time_days"], -2)
        self.assertIn("AFTER", r["reason"])

    def test_signal_after_tolerance_fails(self):
        r = self._run(self._scores(cross_on=self.SPIKE + 3))
        self.assertEqual(r["validation"], validator.FAILED)
        self.assertIsNone(r["lead_time_days"])

    def test_signal_that_never_crosses_fails(self):
        self.assertEqual(self._run(self._scores())["validation"], validator.FAILED)

    def test_already_elevated_at_start_is_inconclusive(self):
        r = self._run(self._scores(cross_on=0))
        self.assertEqual(r["validation"], validator.INCONCLUSIVE)
        self.assertTrue(r["lead_time_is_lower_bound"])
        self.assertEqual(r["lead_time_days"], self.SPIKE)

    def test_early_blip_is_a_false_alarm_not_a_lead(self):
        scores = self._scores(cross_on=8)
        scores[2] = scores[3] = 0.9          # up days 2-3, back down by day 4
        r = self._run(scores)
        self.assertEqual(r["validation"], validator.PASSED)
        self.assertEqual(r["signal_elevated_at"], self.START + timedelta(days=8))
        self.assertEqual(r["lead_time_days"], 2)                 # not 8
        self.assertEqual(len(r["false_alarms"]), 1)
        self.assertEqual(r["false_alarms"][0]["start"], self.START + timedelta(days=2))

    def test_blip_alone_fails(self):
        scores = self._scores()
        scores[2] = 0.9
        r = self._run(scores)
        self.assertEqual(r["validation"], validator.FAILED)
        self.assertEqual(len(r["false_alarms"]), 1)

    def test_no_price_event(self):
        series = _series(self.START, self._scores(cross_on=5))
        prices = _prices(self.START - timedelta(days=1), [70.0] * (self.DAYS + 1))
        r = validator.validate(series, prices, "Hormuz", self.START,
                               self.START + timedelta(days=self.DAYS - 1))
        self.assertEqual(r["validation"], validator.NO_PRICE_EVENT)

    def test_documented_keys_present(self):
        r = self._run(self._scores(cross_on=7))
        for key in ("signal_elevated_at", "price_spiked_at", "lead_time_days",
                    "max_risk_score", "brent_spike_pct", "validation"):
            self.assertIn(key, r)
        self.assertEqual(r["max_risk_score"], 0.8)


CACHE = Path(settings.BASE_DIR) / "data" / "brent_cache.json"


@skipUnless(CACHE.exists(), "committed Brent cache not present")
class GapTests(SimpleTestCase):
    def test_gap_ranges_merge_consecutive_days_only(self):
        ranges = runner.gap_ranges(["2025-06-16", "2025-06-15", "2025-06-18"])
        self.assertEqual(ranges, [
            {"start": date(2025, 6, 15), "end": date(2025, 6, 16), "days": 2},
            {"start": date(2025, 6, 18), "end": date(2025, 6, 18), "days": 1},
        ])

    def test_no_missing_days_means_no_gaps_and_no_flags(self):
        series = [{"date": date(2025, 6, 1)}, {"date": date(2025, 6, 2)}]
        runner.flag_gaps(series, [])
        self.assertEqual(runner.gap_ranges([]), [])
        self.assertTrue(all(r["day_sampled"] for r in series))
        self.assertTrue(all(r["unsampled_days_in_lookback"] == 0 for r in series))

    def test_a_day_after_the_gap_is_sampled_but_a_lower_bound(self):
        series = [{"date": date(2025, 6, d)} for d in (14, 15, 16)]
        runner.flag_gaps(series, ["2025-06-15"])
        self.assertEqual([r["day_sampled"] for r in series], [True, False, True])
        self.assertEqual([r["unsampled_days_in_lookback"] for r in series], [0, 1, 1])


class DocumentedDatesTests(SimpleTestCase):
    """The event dates in runner.BACKTEST_EVENTS and the docstrings are read
    off the committed EIA series; this pins them to it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(CACHE, encoding="utf-8") as fh:
            cls.prices = [
                {"date": date.fromisoformat(r["date"]), "price_usd": r["price_usd"]}
                for r in json.load(fh)["prices"]
            ]

    def _event_date(self, key):
        ev = runner.BACKTEST_EVENTS[key]
        return validator.find_price_event(self.prices, ev["chart_start"], ev["end"])

    def test_hormuz_window_selects_march_2(self):
        move = self._event_date("2026_hormuz_closure")
        self.assertEqual(move["date"], date(2026, 3, 2))
        self.assertAlmostEqual(move["pct_change"], 8.30, places=2)

    def test_2025_window_selects_june_13(self):
        move = self._event_date("2025_iran_standoff")
        self.assertEqual(move["date"], date(2025, 6, 13))
        self.assertAlmostEqual(move["pct_change"], 7.28, places=2)

    def test_2025_window_leaves_room_for_the_signal_to_fall_back(self):
        # The fall-back half of the 2025 test needs the window to outlast the
        # ceasefire crash by more than the ~6.4 days pure decay takes to bring
        # a ~0.95 score under 0.6 (see runner.BACKTEST_EVENTS).
        ev = runner.BACKTEST_EVENTS["2025_iran_standoff"]
        ceasefire = date(2025, 6, 24)
        crash = next(r for r in self.prices if r["date"] == ceasefire)
        self.assertAlmostEqual(crash["price_usd"], 69.13, places=2)
        self.assertGreaterEqual((ev["end"] - ceasefire).days, 14)


# ---------------------------------------------------------------------------
# GKG range fetch
# ---------------------------------------------------------------------------

def _record(url, title="Tankers divert from Strait of Hormuz"):
    return {"url": url, "title": title, "domain": "x.com", "themes": "",
            "seen_at": datetime(2026, 2, 11, tzinfo=UTC), "published_at": None}


class FetchBetweenTests(SimpleTestCase):
    DAY = datetime(2026, 2, 11, tzinfo=UTC)

    def test_a_day_is_96_half_open_slices(self):
        stamps = gdelt_gkg.slices_between(self.DAY, self.DAY + timedelta(days=1))
        self.assertEqual(len(stamps), 96)
        self.assertEqual(stamps[0], self.DAY)
        self.assertEqual(stamps[-1], self.DAY + timedelta(hours=23, minutes=45))

    def test_unaligned_start_rounds_up_not_down(self):
        stamps = gdelt_gkg.slices_between(self.DAY + timedelta(minutes=5),
                                          self.DAY + timedelta(minutes=31))
        self.assertEqual(stamps, [self.DAY + timedelta(minutes=15), self.DAY + timedelta(minutes=30)])

    def _fake_slices(self, stamp, timeout=60):
        i = int((stamp - self.DAY).total_seconds() // 900)
        # every slice repeats the previous slice's URL, so dedup order matters
        return [_record(f"https://x.com/{i}"), _record(f"https://x.com/{max(i - 1, 0)}")]

    def test_parallel_matches_serial(self):
        end = self.DAY + timedelta(hours=3)
        with patch.object(gdelt_gkg, "fetch_slice", side_effect=self._fake_slices):
            serial = gdelt_gkg.fetch_between(self.DAY, end, workers=1)
        with patch.object(gdelt_gkg, "fetch_slice", side_effect=self._fake_slices):
            parallel = gdelt_gkg.fetch_between(self.DAY, end, workers=4)
        urls = lambda wf: [a["url"] for a in wf.corridors["Hormuz"].articles]
        self.assertEqual(urls(serial), urls(parallel))
        self.assertEqual(len(urls(serial)), 12)
        self.assertEqual((serial.slices_read, serial.slices_total), (12, 12))

    def test_mostly_unreadable_window_is_not_sampled(self):
        calls = iter(range(1000))
        def flaky(stamp, timeout=60):
            return None if next(calls) % 3 else [_record(f"https://x.com/{stamp}")]
        with patch.object(gdelt_gkg, "fetch_slice", side_effect=flaky):
            wf = gdelt_gkg.fetch_between(self.DAY, self.DAY + timedelta(hours=3))
        self.assertTrue(all(f.status == FETCH_ERROR for f in wf.corridors.values()))
        self.assertEqual(wf.slices_total, 12)
        self.assertEqual(wf.slices_read, 4)

    def test_empty_range_is_not_sampled(self):
        wf = gdelt_gkg.fetch_between(self.DAY, self.DAY)
        self.assertEqual(wf.slices_total, 0)
        self.assertTrue(all(f.status == FETCH_ERROR for f in wf.corridors.values()))


# ---------------------------------------------------------------------------
# runner — windows and definitions
# ---------------------------------------------------------------------------

class EventDefinitionTests(SimpleTestCase):

    def test_hormuz_window_includes_the_warmup(self):
        ev = runner.BACKTEST_EVENTS["2026_hormuz_closure"]
        days = runner.pull_days(ev)
        self.assertEqual(days[0], date(2025, 12, 25))
        self.assertEqual(days[-1], date(2026, 3, 18))
        self.assertEqual(len(days), 84)
        self.assertEqual(len(runner.chart_days(ev)), 77)

    def test_no_window_reaches_the_live_lookback(self):
        # runner's docstring: an end after 2026-03-31 would put backtest events
        # inside the 180-day lookback of a live score computed on 2026-09-27.
        for key, ev in runner.BACKTEST_EVENTS.items():
            self.assertLessEqual(ev["end"], date(2026, 3, 31), key)

    def test_unknown_event_raises(self):
        with self.assertRaises(ValueError):
            runner.get_event("suez_1956")

    def test_score_time_is_before_bst(self):
        # 16:30 UTC == London close only while the UK is on GMT; BST began
        # 2026-03-29, after every charted day.
        for ev in runner.BACKTEST_EVENTS.values():
            if ev["end"].year == 2026:
                self.assertLess(ev["end"], date(2026, 3, 29))


# ---------------------------------------------------------------------------
# runner — pull
# ---------------------------------------------------------------------------

_window_seq = iter(range(10 ** 6))


def _window(status=FETCH_OK, n=2, read=96, total=96):
    """A fake day: ``n`` Hormuz articles with fresh URLs, or a not-sampled day
    (every corridor FETCH_ERROR, nothing to store) when status is FETCH_ERROR."""
    if status == FETCH_ERROR:
        corridors = {c: CorridorFetch(c, [], FETCH_ERROR) for c in BASELINES}
    else:
        batch = next(_window_seq)
        articles = [{"url": f"https://x.com/{batch}-{i}", "source": "gdelt_gkg",
                     "title": f"t{i}", "raw_text": "t\nseendate: 20260211T120000Z"}
                    for i in range(n)]
        corridors = {
            "Hormuz": CorridorFetch("Hormuz", articles, FETCH_OK),
            "Red Sea": CorridorFetch("Red Sea", [], FETCH_EMPTY),
            "Cape": CorridorFetch("Cape", [], FETCH_EMPTY),
        }
    return gdelt_gkg.WindowFetch(corridors, read, total)


class PullTests(_TempBacktestDir, TestCase):
    KEY = "2026_hormuz_closure"

    @patch.object(gdelt_gkg, "fetch_between")
    def test_pull_stores_and_records_every_day(self, fetch):
        fetch.side_effect = lambda *a, **k: _window()
        days = [date(2026, 2, 4), date(2026, 2, 5)]
        summary = runner.pull_event(self.KEY, days=days)
        self.assertEqual(summary["ok"], 2)
        self.assertEqual(RawArticle.objects.count(), 4)
        ledger = runner.load_ledger(self.KEY)["days"]
        self.assertEqual(ledger["2026-02-04"]["status"], runner.DAY_OK)
        self.assertEqual(ledger["2026-02-04"]["matched"]["Hormuz"], 2)
        # each day asked for exactly its own UTC day
        start, end = fetch.call_args_list[0].args[:2]
        self.assertEqual(start, datetime(2026, 2, 4, tzinfo=UTC))
        self.assertEqual(end - start, timedelta(days=1))

    @patch.object(gdelt_gkg, "fetch_between")
    def test_rerun_skips_ok_days_and_retries_the_rest(self, fetch):
        fetch.side_effect = [_window(), _window(status=FETCH_ERROR, read=10)]
        runner.pull_event(self.KEY, days=[date(2026, 2, 4), date(2026, 2, 5)])
        ledger = runner.load_ledger(self.KEY)["days"]
        self.assertEqual(ledger["2026-02-05"]["status"], runner.DAY_NOT_SAMPLED)
        self.assertEqual(ledger["2026-02-05"]["stored"], 0)

        fetch.side_effect = [_window()]
        summary = runner.pull_event(self.KEY, days=[date(2026, 2, 4), date(2026, 2, 5)])
        self.assertEqual((summary["skipped"], summary["pulled"]), (1, 1))
        self.assertEqual(runner.load_ledger(self.KEY)["days"]["2026-02-05"]["status"], runner.DAY_OK)

    @patch.object(gdelt_gkg, "fetch_between")
    def test_force_repulls_ok_days(self, fetch):
        fetch.side_effect = lambda *a, **k: _window()
        runner.pull_event(self.KEY, days=[date(2026, 2, 4)])
        summary = runner.pull_event(self.KEY, days=[date(2026, 2, 4)], force=True)
        self.assertEqual(summary["pulled"], 1)

    @patch.object(gdelt_gkg, "fetch_between", side_effect=RuntimeError("disk full"))
    def test_a_crashing_day_is_recorded_not_raised(self, _fetch):
        seen = []
        summary = runner.pull_event(self.KEY, days=[date(2026, 2, 4), date(2026, 2, 5)],
                                    on_day=lambda d, e: seen.append(e["status"]))
        self.assertEqual(summary["error"], 2)
        self.assertEqual(seen, [runner.DAY_ERROR, runner.DAY_ERROR])
        self.assertIn("disk full", runner.load_ledger(self.KEY)["days"]["2026-02-04"]["error"])

    def test_days_outside_the_window_are_rejected(self):
        with self.assertRaises(ValueError):
            runner.pull_event(self.KEY, days=[date(2026, 9, 1)])

    def test_corrupt_ledger_reads_as_empty(self):
        runner.ledger_path(self.KEY).parent.mkdir(parents=True, exist_ok=True)
        runner.ledger_path(self.KEY).write_text("{not json")
        self.assertEqual(runner.load_ledger(self.KEY)["days"], {})


# ---------------------------------------------------------------------------
# runner — scoring and the report
# ---------------------------------------------------------------------------

class ScoreDayTests(TestCase):
    DAY = date(2026, 2, 20)

    @classmethod
    def setUpTestData(cls):
        _make_corridors()

    def _corridors(self):
        return list(Corridor.objects.order_by("name").values_list("name", "baseline_risk"))

    def _at(self, days=0, hh=12, mm=0):
        return datetime.combine(self.DAY + timedelta(days=days), time(hh, mm), tzinfo=UTC)

    def test_matches_the_production_function(self):
        for i in range(5):
            _event("Hormuz", self._at(days=-i), severity=2 + (i % 4), seed=i)
        _event("Red Sea", self._at(days=-1), severity=4, seed=99)
        row = runner.score_day(self.DAY, self._corridors(), focus="Hormuz")
        as_of = runner._as_of(self.DAY)
        for name, baseline in BASELINES.items():
            raw = compute_corridor_severity(name, now=as_of, exclude_future=True)
            self.assertAlmostEqual(row["scores"][name], round(normalize_severity(raw, baseline), 4))
        self.assertEqual(len(row["top_stories"]), 3)

    def test_future_event_does_not_leak_into_a_past_day(self):
        _event("Hormuz", self._at(days=10), severity=5, seed=1)
        row = runner.score_day(self.DAY, self._corridors())
        self.assertEqual(row["scores"]["Hormuz"], BASELINES["Hormuz"])
        self.assertEqual(row["events"]["Hormuz"], 0)

    def test_incremental_series_equals_scoring_each_day_from_scratch(self):
        # The runner scores every day from ONE pass; score_day re-clusters from
        # scratch. Includes syndicated repeats, and old events that the 180-day
        # lookback edge passes DURING the window, so days fall into more than
        # one group and the grouping logic is exercised.
        import random as _r
        rng = _r.Random(11)
        days = [self.DAY + timedelta(days=i) for i in range(10)]
        stems = ["tanker seized strait", "navy convoy escorts ships", "iran sets deadline talks",
                 "insurance premiums soar gulf", "missile hits oil terminal"]
        seed = 0
        for d in range(-6, 10):
            for _ in range(rng.randint(2, 7)):
                seed += 1
                title = rng.choice(stems) + rng.choice(["", " again", " report", " update"])
                _event("Hormuz", self._at(days=d, hh=rng.randint(0, 23)),
                       severity=rng.randint(2, 5), seed=seed, title=title)
        # old events near the lookback edge of the first and last days
        edge = runner._as_of(days[0]) - timedelta(days=180)
        for i, off in enumerate((-1, 2, 5)):
            _event("Hormuz", edge + timedelta(days=off), severity=5, seed=900 + i)

        series = runner.corridor_series("Hormuz", BASELINES["Hormuz"], days, with_top=True)
        # the fixture must really exercise what it claims to: stories merge, and
        # the lookback edge passes old events mid-window (-> several passes)
        self.assertTrue(any(c["events"] > c["stories"] for c in series))
        first_cut = runner._as_of(days[0]) - timedelta(days=180)
        last_cut = runner._as_of(days[-1]) - timedelta(days=180)
        self.assertGreaterEqual(ExtractedEvent.objects.filter(
            timestamp__gte=first_cut, timestamp__lt=last_cut).count(), 2)
        for k, day in enumerate(days):
            ref = runner.score_day(day, [("Hormuz", BASELINES["Hormuz"])], focus="Hormuz")
            cell = series[k]
            self.assertEqual(cell["score"], ref["scores"]["Hormuz"], day)
            self.assertEqual(cell["events"], ref["events"]["Hormuz"], day)
            self.assertEqual(cell["stories"], ref["stories"]["Hormuz"], day)
            self.assertEqual(cell["top_stories"], ref["top_stories"], day)

    def test_top_k_matches_the_production_statistic_at_that_k(self):
        for i in range(6):
            _event("Hormuz", self._at(days=-i), severity=5 - (i % 3), seed=i)
        as_of = runner._as_of(self.DAY)
        for k in (1, 3, 5):
            row = runner.score_day(self.DAY, self._corridors(), focus="Hormuz", top_k=k)
            events = corridor_events("Hormuz", now=as_of, exclude_future=True)
            stories = cluster_stories(events, now=as_of)
            raw = top_k_severity(stories, k=k)
            self.assertAlmostEqual(row["scores"]["Hormuz"],
                                   round(normalize_severity(raw, BASELINES["Hormuz"]), 4), msg=k)
            self.assertEqual(len(row["top_stories"]), min(k, len(stories)))

    def test_incremental_series_equals_from_scratch_at_another_k(self):
        for d in range(-3, 4):
            _event("Hormuz", self._at(days=d), severity=2 + (d % 4), seed=50 + d)
        days = [self.DAY + timedelta(days=i) for i in range(4)]
        series = runner.corridor_series("Hormuz", BASELINES["Hormuz"], days, top_k=5)
        for k, day in enumerate(days):
            ref = runner.score_day(day, [("Hormuz", BASELINES["Hormuz"])], top_k=5)
            self.assertEqual(series[k]["score"], ref["scores"]["Hormuz"], day)

    def test_scored_at_the_brent_assessment_not_end_of_day(self):
        _event("Hormuz", self._at(hh=16, mm=0), seed=1)     # before 16:30 -> in
        _event("Hormuz", self._at(hh=17, mm=0), seed=2)     # after 16:30  -> out
        row = runner.score_day(self.DAY, self._corridors())
        self.assertEqual(row["events"]["Hormuz"], 1)


class PendingInScopeTests(TestCase):

    def _raw(self, url, stamp, processed=False):
        RawArticle.objects.create(url=url, source="gdelt_gkg", title="t",
                                  raw_text=f"t\nseendate: {stamp}", processed=processed)

    def test_only_unextracted_rows_that_can_move_the_series_count(self):
        ev = runner.BACKTEST_EVENTS["2026_hormuz_closure"]
        self._raw("https://x.com/in", "20260220T120000Z")
        self._raw("https://x.com/done", "20260220T120000Z", processed=True)
        self._raw("https://x.com/live", "20260926T120000Z")      # after the window
        self._raw("https://x.com/old", "20250101T120000Z")       # before any lookback
        self.assertEqual(runner.pending_in_scope(ev), 1)


class RunBacktestTests(_TempBacktestDir, TestCase):
    """End to end on a synthetic crisis: calm, a Hormuz crossing on 02-27, a
    +10% Brent session on 03-02."""
    KEY = "2026_hormuz_closure"

    @classmethod
    def setUpTestData(cls):
        _make_corridors()
        ev = runner.BACKTEST_EVENTS[cls.KEY]
        # three severity-5 stories from 02-27 -> Hormuz near 1.0 from then on
        for i in range(3):
            _event("Hormuz", datetime(2026, 2, 27, 9 + i, tzinfo=UTC), seed=i)
        # one mild story during the calm period
        _event("Hormuz", datetime(2026, 2, 12, 9, tzinfo=UTC), severity=2, seed=50)
        # a September event that must NOT leak into February
        _event("Hormuz", datetime(2026, 9, 26, 9, tzinfo=UTC), seed=77)
        cls.ev = ev

    def _complete_ledger(self):
        runner.save_ledger(self.KEY, {"event": self.KEY, "days": {
            d.isoformat(): {"status": runner.DAY_OK, "slices_read": 96,
                            "slices_total": 96, "stored": 1, "matched": {}}
            for d in runner.pull_days(self.ev)
        }})

    def _prices(self):
        start = runner.pull_start(self.ev) - timedelta(days=7)
        n = (self.ev["end"] - start).days + 1
        return _flat_then_jump(start, n, (date(2026, 3, 2) - start).days)

    def test_synthetic_crisis_passes_with_the_right_lead(self):
        self._complete_ledger()
        with patch.object(runner.eia, "fetch_brent_prices", return_value=self._prices()):
            report = runner.run_backtest(self.KEY)
        self.assertEqual(report["validation"], validator.PASSED, report["reason"])
        self.assertEqual(report["signal_elevated_at"], date(2026, 2, 27))
        self.assertEqual(report["price_spiked_at"], date(2026, 3, 2))
        self.assertEqual(report["lead_time_days"], 3)
        self.assertEqual(report["signal_elevated_days_before"], 3)
        self.assertEqual(len(report["series"]), 77)
        first = report["series"][0]["scores"]["Hormuz"]
        self.assertLess(first, validator.SIGNAL_THRESHOLD)       # Sept event did not leak
        self.assertTrue(runner.report_path(self.KEY).exists())

    def test_backtest_never_writes_risk_scores_or_moves_live_risk(self):
        self._complete_ledger()
        before = dict(Corridor.objects.values_list("name", "live_risk_score"))
        with patch.object(runner.eia, "fetch_brent_prices", return_value=self._prices()):
            runner.run_backtest(self.KEY)
        self.assertEqual(RiskScore.objects.count(), 0)
        self.assertEqual(dict(Corridor.objects.values_list("name", "live_risk_score")), before)

    def test_missing_days_downgrade_to_incomplete(self):
        self._complete_ledger()
        ledger = runner.load_ledger(self.KEY)
        ledger["days"]["2026-02-20"]["status"] = runner.DAY_NOT_SAMPLED
        runner.save_ledger(self.KEY, ledger)
        with patch.object(runner.eia, "fetch_brent_prices", return_value=self._prices()):
            report = runner.run_backtest(self.KEY)
        self.assertEqual(report["validation"], runner.VERDICT_INCOMPLETE)
        self.assertEqual(report["validation_if_complete"], validator.PASSED)
        self.assertIn("2026-02-20", report["coverage"]["days_missing"])

    def test_rows_are_flagged_for_the_gap_they_sit_in_or_after(self):
        self._complete_ledger()
        ledger = runner.load_ledger(self.KEY)
        for d in ("2026-02-20", "2026-02-21"):
            ledger["days"][d]["status"] = runner.DAY_NOT_SAMPLED
        runner.save_ledger(self.KEY, ledger)
        with patch.object(runner.eia, "fetch_brent_prices", return_value=self._prices()):
            report = runner.run_backtest(self.KEY)
        rows = {r["date"]: r for r in report["series"]}
        self.assertTrue(rows[date(2026, 2, 19)]["day_sampled"])
        self.assertEqual(rows[date(2026, 2, 19)]["unsampled_days_in_lookback"], 0)
        self.assertFalse(rows[date(2026, 2, 21)]["day_sampled"])
        self.assertTrue(rows[date(2026, 3, 1)]["day_sampled"])
        self.assertEqual(rows[date(2026, 3, 1)]["unsampled_days_in_lookback"], 2)
        self.assertEqual(report["gaps"], [
            {"start": date(2026, 2, 20), "end": date(2026, 2, 21), "days": 2},
        ])

    def test_top_k_run_writes_its_own_file_and_never_the_cited_report(self):
        self._complete_ledger()
        with patch.object(runner.eia, "fetch_brent_prices", return_value=self._prices()):
            cited = runner.run_backtest(self.KEY)
            sens = runner.run_backtest(self.KEY, top_k=5)
        self.assertNotEqual(runner.report_path(self.KEY, 5), runner.report_path(self.KEY))
        self.assertTrue(runner.report_path(self.KEY, 5).exists())
        loaded = runner.load_report(self.KEY)          # what /api/backtest/ serves
        self.assertEqual(loaded["method"]["top_k_stories"], runner.TOP_K_STORIES)
        self.assertTrue(loaded["method"]["top_k_is_production"])
        self.assertEqual(sens["method"]["top_k_stories"], 5)
        self.assertFalse(sens["method"]["top_k_is_production"])
        # three severity-5 stories on 02-27: diluted over 5, so lower than over 3
        day = {r["date"]: r for r in sens["series"]}[date(2026, 2, 27)]
        base = {r["date"]: r for r in cited["series"]}[date(2026, 2, 27)]
        self.assertLess(day["scores"]["Hormuz"], base["scores"]["Hormuz"])

    def test_top_k_below_one_is_rejected(self):
        with self.assertRaises(ValueError):
            runner.run_backtest(self.KEY, write=False, top_k=0)

    def test_pending_extraction_downgrades_to_incomplete(self):
        self._complete_ledger()
        RawArticle.objects.create(url="https://x.com/p", source="gdelt_gkg", title="t",
                                  raw_text="t\nseendate: 20260301T120000Z")
        with patch.object(runner.eia, "fetch_brent_prices", return_value=self._prices()):
            report = runner.run_backtest(self.KEY)
        self.assertEqual(report["validation"], runner.VERDICT_INCOMPLETE)
        self.assertEqual(report["pending_extraction"], 1)

    def test_no_price_data_is_reported_not_raised(self):
        self._complete_ledger()
        with patch.object(runner.eia, "fetch_brent_prices",
                          side_effect=runner.eia.EIAConfigurationError("no key")):
            report = runner.run_backtest(self.KEY, write=False)
        self.assertEqual(report["validation"], runner.VERDICT_NO_PRICE_DATA)
        self.assertFalse(runner.report_path(self.KEY).exists())

    def test_written_report_round_trips_as_json(self):
        self._complete_ledger()
        with patch.object(runner.eia, "fetch_brent_prices", return_value=self._prices()):
            runner.run_backtest(self.KEY)
        loaded = runner.load_report(self.KEY)
        self.assertEqual(loaded["validation"], validator.PASSED)
        self.assertEqual(loaded["price_spiked_at"], "2026-03-02")


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------

class CommandTests(_TempBacktestDir, TestCase):

    def _call(self, *args):
        out = StringIO()
        call_command("run_backtest", *args, stdout=out)
        return out.getvalue()

    def test_list(self):
        out = self._call("--list")
        self.assertIn("2026_hormuz_closure", out)
        self.assertIn("not run", out)

    def test_event_required(self):
        with self.assertRaises(CommandError):
            self._call()

    def test_unknown_event(self):
        with self.assertRaises(CommandError):
            self._call("--event", "suez_1956")

    def test_day_without_pull_is_rejected(self):
        with self.assertRaises(CommandError):
            self._call("--event", "2026_hormuz_closure", "--day", "2026-02-11")

    def test_bad_day_format(self):
        with self.assertRaises(CommandError):
            self._call("--event", "2026_hormuz_closure", "--pull", "--day", "11/02/2026")

    def test_top_k_below_one_is_rejected_by_the_command(self):
        with self.assertRaises(CommandError):
            self._call("--event", "2026_hormuz_closure", "--top-k", "0")

    def test_top_k_run_is_labelled_and_written_beside_the_cited_report(self):
        _make_corridors()
        with patch.object(runner.eia, "fetch_brent_prices", return_value=[]):
            out = self._call("--event", "2026_hormuz_closure", "--top-k", "5")
        self.assertIn("SENSITIVITY RUN: top-5", out)
        self.assertIn("2026_hormuz_closure.top5.json", out)
        self.assertFalse(runner.report_path("2026_hormuz_closure").exists())

    def test_status_on_a_fresh_window_points_at_pull(self):
        out = self._call("--event", "2026_hormuz_closure", "--status")
        self.assertIn("0 / 84", out)
        self.assertIn("--pull", out)

    @patch.object(gdelt_gkg, "fetch_between", side_effect=lambda *a, **k: _window())
    def test_pull_one_day_reports_progress(self, _fetch):
        out = self._call("--event", "2026_hormuz_closure", "--pull", "--day", "2026-02-11")
        self.assertIn("2026-02-11  ok", out)
        self.assertIn("83 day(s) still missing", out)

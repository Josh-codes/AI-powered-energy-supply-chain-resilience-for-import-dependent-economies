"""Tests for backtest/eia.py.

Convention (same as test_ingestion.py): HTTP is mocked at the import site,
``backtest.eia.requests.get``, and ``BRENT_CACHE_PATH`` is pointed at a temp
directory so no test touches the network or the real cache.

No database is needed, so these are SimpleTestCase.
"""
import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings

from backtest import eia


def _row(day, value):
    return {"period": day, "series": "RBRTE", "value": value}


def _response(rows, status=200):
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = {"response": {"total": len(rows), "data": rows}}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status}")
    else:
        resp.raise_for_status.return_value = None
    return resp


def _prices(*pairs):
    return [{"date": date.fromisoformat(d), "price_usd": p} for d, p in pairs]


class _TempCacheMixin:
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self._tmp.name) / "brent_cache.json"
        patcher = patch.object(eia, "BRENT_CACHE_PATH", self.cache_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        # Retries would otherwise sleep 2s + 4s per failing test.
        sleeper = patch.object(eia.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)


@override_settings(EIA_API_KEY="test-key")
class FetchBrentPricesTests(_TempCacheMixin, SimpleTestCase):

    @patch("backtest.eia.requests.get")
    def test_parses_and_sorts_ascending(self, mock_get):
        mock_get.return_value = _response([
            _row("2026-03-03", "80.10"),
            _row("2026-03-02", 77.24),
        ])
        prices = eia.fetch_brent_prices("2026-03-01", "2026-03-05")
        self.assertEqual([p["date"] for p in prices],
                         [date(2026, 3, 2), date(2026, 3, 3)])
        self.assertEqual(prices[1]["price_usd"], 80.10)

    @patch("backtest.eia.requests.get")
    def test_null_values_are_dropped_not_zeroed(self, mock_get):
        # EIA publishes holidays as value: null. A 0.0 would register as a
        # -100% session and dominate every return calculation.
        mock_get.return_value = _response([
            _row("2026-03-02", 77.24),
            _row("2026-03-03", None),
            _row("2026-03-04", 79.0),
        ])
        prices = eia.fetch_brent_prices("2026-03-01", "2026-03-05")
        self.assertEqual(len(prices), 2)
        self.assertNotIn(0.0, [p["price_usd"] for p in prices])

    @patch("backtest.eia.requests.get")
    def test_series_and_key_are_sent(self, mock_get):
        mock_get.return_value = _response([_row("2026-03-02", 77.24)])
        eia.fetch_brent_prices("2026-03-01", "2026-03-05")
        params = dict(mock_get.call_args.kwargs["params"])
        self.assertEqual(params["facets[series][]"], "RBRTE")
        self.assertEqual(params["api_key"], "test-key")
        self.assertEqual(params["frequency"], "daily")

    @patch("backtest.eia.requests.get")
    def test_paginates_until_a_short_page(self, mock_get):
        full = [_row(f"2020-01-{(i % 28) + 1:02d}", 50.0 + i)
                for i in range(eia.MAX_PAGE_LENGTH)]
        mock_get.side_effect = [
            _response(full),
            _response([_row("2026-01-02", 61.0)]),
        ]
        eia.fetch_brent_prices("2020-01-01", "2026-01-05", use_cache=False)
        self.assertEqual(mock_get.call_count, 2)
        offsets = [dict(c.kwargs["params"])["offset"] for c in mock_get.call_args_list]
        self.assertEqual(offsets, ["0", str(eia.MAX_PAGE_LENGTH)])

    @patch("backtest.eia.requests.get")
    def test_network_failure_returns_empty_never_raises(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("down")
        self.assertEqual(eia.fetch_brent_prices("2026-03-01", "2026-03-05"), [])
        self.assertEqual(mock_get.call_count, eia._RETRY_ATTEMPTS)

    @patch("backtest.eia.requests.get")
    def test_http_error_returns_empty(self, mock_get):
        mock_get.return_value = _response([], status=403)
        self.assertEqual(eia.fetch_brent_prices("2026-03-01", "2026-03-05"), [])

    @patch("backtest.eia.requests.get")
    def test_eia_error_body_returns_empty(self, mock_get):
        resp = _response([])
        resp.json.return_value = {"error": "invalid api_key"}
        mock_get.return_value = resp
        self.assertEqual(eia.fetch_brent_prices("2026-03-01", "2026-03-05"), [])

    @patch("backtest.eia.requests.get")
    def test_failed_second_page_discards_the_first(self, mock_get):
        # A truncated series would make the lead-time measurement quietly
        # wrong, so a partial fetch must not be returned as if complete.
        full = [_row(f"2020-01-{(i % 28) + 1:02d}", 50.0)
                for i in range(eia.MAX_PAGE_LENGTH)]
        mock_get.side_effect = [_response(full)] + [
            requests.ConnectionError("down")
        ] * eia._RETRY_ATTEMPTS
        self.assertEqual(
            eia.fetch_brent_prices("2020-01-01", "2026-01-05", use_cache=False), []
        )

    def test_start_after_end_is_rejected(self):
        with self.assertRaises(ValueError):
            eia.fetch_brent_prices("2026-03-05", "2026-03-01")


@override_settings(EIA_API_KEY="")
class MissingKeyTests(_TempCacheMixin, SimpleTestCase):

    @patch("backtest.eia.requests.get")
    def test_missing_key_raises_instead_of_returning_empty(self, mock_get):
        # The one failure that is NOT swallowed: silence here would surface
        # much later as an unexplained flat price line.
        with self.assertRaises(eia.EIAConfigurationError):
            eia.fetch_brent_prices("2026-03-01", "2026-03-05")
        mock_get.assert_not_called()

    @patch("backtest.eia.requests.get")
    def test_cache_hit_needs_no_key(self, mock_get):
        # Committed cache => the backtest reproduces on a machine with no key.
        eia._write_cache(_prices(("2026-03-02", 77.24), ("2026-03-06", 90.0)),
                         date(2026, 3, 1))
        prices = eia.fetch_brent_prices("2026-03-01", "2026-03-06")
        self.assertEqual(len(prices), 2)
        mock_get.assert_not_called()


@override_settings(EIA_API_KEY="test-key")
class CacheTests(_TempCacheMixin, SimpleTestCase):

    @patch("backtest.eia.requests.get")
    def test_identical_request_hits_cache_despite_holiday_edges(self, mock_get):
        # Regression: coverage used to be inferred from the first/last
        # observation. Jan 1 has no row and the last ~5 days are unpublished,
        # so the same request missed the cache every single time.
        mock_get.return_value = _response([
            _row("2025-01-02", 76.0), _row("2025-01-03", 76.5),
        ])
        eia.fetch_brent_prices("2025-01-01", "2025-01-03")
        eia.fetch_brent_prices("2025-01-01", "2025-01-03")
        self.assertEqual(mock_get.call_count, 1)

    @patch("backtest.eia.requests.get")
    def test_request_past_last_observation_refetches(self, mock_get):
        # Days after the last observation may not be published yet, so they
        # must not be recorded as covered or the tail would freeze.
        mock_get.return_value = _response([_row("2026-09-22", 114.89)])
        eia.fetch_brent_prices("2026-09-20", "2026-09-27")
        eia.fetch_brent_prices("2026-09-20", "2026-09-27")
        self.assertEqual(mock_get.call_count, 2)

    @patch("backtest.eia.requests.get")
    def test_subrange_is_served_from_cache(self, mock_get):
        mock_get.return_value = _response([
            _row("2026-02-25", 70.69), _row("2026-03-02", 77.24),
            _row("2026-03-18", 118.09),
        ])
        eia.fetch_brent_prices("2026-02-01", "2026-03-18")
        sub = eia.fetch_brent_prices("2026-03-01", "2026-03-10")
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual([p["date"] for p in sub], [date(2026, 3, 2)])

    def test_disjoint_spans_are_not_unioned(self):
        # Unioning [Jan] and [Mar] would claim February is covered and serve
        # an empty February as though Brent had not traded.
        eia._write_cache(_prices(("2026-01-05", 61.0), ("2026-01-30", 66.0)),
                         date(2026, 1, 1))
        eia._write_cache(_prices(("2026-03-02", 77.0), ("2026-03-30", 110.0)),
                         date(2026, 3, 1))
        self.assertIsNone(eia._read_cache(date(2026, 1, 1), date(2026, 3, 30)))
        self.assertIsNotNone(eia._read_cache(date(2026, 3, 1), date(2026, 3, 30)))

    def test_contiguous_spans_are_unioned(self):
        eia._write_cache(_prices(("2026-01-05", 61.0), ("2026-01-31", 66.0)),
                         date(2026, 1, 1))
        eia._write_cache(_prices(("2026-02-02", 67.0), ("2026-02-27", 71.0)),
                         date(2026, 2, 1))
        rows = eia._read_cache(date(2026, 1, 1), date(2026, 2, 27))
        self.assertEqual(len(rows), 4)

    def test_legacy_cache_without_coverage_is_treated_as_a_miss(self):
        self.cache_path.write_text(json.dumps(
            {"prices": [{"date": "2026-03-02", "price_usd": 77.0}]}
        ))
        self.assertIsNone(eia._read_cache(date(2026, 3, 2), date(2026, 3, 2)))

    def test_corrupt_cache_is_a_miss_not_a_crash(self):
        self.cache_path.write_text("{not json")
        self.assertIsNone(eia._read_cache(date(2026, 3, 2), date(2026, 3, 2)))

    def test_cache_file_records_provenance(self):
        eia._write_cache(_prices(("2026-03-02", 77.24)), date(2026, 3, 1))
        payload = json.loads(self.cache_path.read_text())
        self.assertEqual(payload["series"], "RBRTE")
        self.assertIn("eia", payload["source"].lower())
        self.assertEqual(payload["covered_start"], "2026-03-01")
        self.assertEqual(payload["covered_end"], "2026-03-02")


class EventLocationTests(SimpleTestCase):
    """The helpers that date the price events from the price series alone."""

    SERIES = _prices(
        ("2026-02-20", 71.50),
        ("2026-02-23", 72.10),
        ("2026-02-25", 70.69),   # onset trough
        ("2026-03-02", 77.24),
        ("2026-03-05", 88.59),
        ("2026-03-12", 102.38),
        ("2026-03-18", 118.09),  # first print over the target
        ("2026-04-07", 138.21),
    )

    def test_daily_returns_measure_against_previous_published_session(self):
        rets = eia.daily_returns(self.SERIES)
        self.assertEqual(len(rets), len(self.SERIES) - 1)
        first = rets[0]
        self.assertEqual(first["date"], date(2026, 2, 23))
        self.assertAlmostEqual(first["pct_change"], (72.10 - 71.50) / 71.50 * 100)

    def test_zero_price_is_skipped_not_divided_by(self):
        rets = eia.daily_returns(_prices(("2026-01-01", 0.0), ("2026-01-02", 60.0)))
        self.assertEqual(rets, [])

    def test_largest_moves_up_are_ordered_and_positive(self):
        moves = eia.largest_moves(self.SERIES, n=3)
        pcts = [m["pct_change"] for m in moves]
        self.assertEqual(pcts, sorted(pcts, reverse=True))
        self.assertTrue(all(p > 0 for p in pcts))
        # 118.09 -> 138.21 is +17.0%, beating 102.38 -> 118.09 at +15.3%.
        # (The fixture skips the sessions between 03-18 and 04-07.)
        self.assertEqual(moves[0]["date"], date(2026, 4, 7))

    def test_largest_moves_down(self):
        moves = eia.largest_moves(self.SERIES, n=5, direction="down")
        self.assertTrue(all(m["pct_change"] < 0 for m in moves))
        self.assertEqual(moves[0]["date"], date(2026, 2, 25))

    def test_run_up_finds_the_onset_trough_and_first_crossing(self):
        run = eia.find_run_up(self.SERIES, 69.0, 114.0)
        self.assertEqual(run["start"], date(2026, 2, 25))
        self.assertEqual(run["peak"], date(2026, 3, 18))
        self.assertEqual(run["days"], 21)

    def test_run_up_ignores_a_start_that_fell_back_below_the_band(self):
        series = _prices(
            ("2026-01-01", 70.0),   # early trough...
            ("2026-01-05", 80.0),
            ("2026-01-09", 69.5),   # ...fell back: the climb restarts here
            ("2026-01-20", 100.0),
            ("2026-01-30", 115.0),
        )
        run = eia.find_run_up(series, 69.0, 114.0)
        self.assertEqual(run["start"], date(2026, 1, 9))

    def test_run_up_absent_returns_none(self):
        flat = _prices(("2025-10-01", 64.0), ("2025-10-02", 65.0))
        self.assertIsNone(eia.find_run_up(flat, 69.0, 114.0))

    def test_price_on_uses_most_recent_prior_session(self):
        # A Sunday resolves to the preceding Friday's settlement.
        row = eia.price_on(self.SERIES, "2026-03-15")
        self.assertEqual(row["date"], date(2026, 3, 12))

    def test_price_on_before_series_is_none(self):
        self.assertIsNone(eia.price_on(self.SERIES, "2025-01-01"))

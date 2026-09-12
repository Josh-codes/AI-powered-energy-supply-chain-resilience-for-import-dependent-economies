"""Tests for pipeline/ingest/* and pipeline/tasks.py.

Convention: every outbound HTTP/feed call is mocked with unittest.mock.patch at
the import site (pipeline.ingest.gdelt.requests.get, pipeline.ingest.rss.feedparser.parse).
No test in this module may touch the network.

Because CLAUDE.md requires the pipeline to never crash on external failure, most
of these assert graceful degradation, not just the happy path.
"""
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from django.core.management import call_command
from django.test import TestCase

from core.models import RawArticle
from pipeline import tasks
from pipeline.ingest import gdelt, ofac, rss

GDELT_PAYLOAD = {
    "articles": [
        {
            "url": "https://example.com/hormuz-tanker",
            "title": "Tanker traffic slows in Strait of Hormuz",
            "domain": "example.com",
            "sourcecountry": "India",
            "seendate": "20260911T120000Z",
        },
        {
            "url": "https://example.com/red-sea-convoy",
            "title": "Convoy escorts resume in the Red Sea",
            "domain": "example.com",
            "sourcecountry": "Egypt",
            "seendate": "20260911T130000Z",
        },
    ]
}


def _mock_response(payload):
    response = Mock()
    response.raise_for_status = Mock()
    response.json = Mock(return_value=payload)
    return response


def _mock_feed(entries, bozo=0):
    feed = Mock()
    feed.bozo = bozo
    feed.bozo_exception = "malformed xml"
    feed.entries = entries
    return feed


class GdeltIngestTests(TestCase):
    @patch("pipeline.ingest.gdelt.requests.get")
    def test_fetch_returns_normalized_articles(self, mock_get):
        mock_get.return_value = _mock_response(GDELT_PAYLOAD)

        articles = gdelt.fetch_gdelt_articles(gdelt.CORRIDOR_QUERIES["Hormuz"], corridor_hint="Hormuz")

        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["url"], "https://example.com/hormuz-tanker")
        self.assertEqual(articles[0]["source"], "gdelt")
        self.assertIn("Hormuz", articles[0]["title"])
        self.assertIn("example.com", articles[0]["raw_text"])
        self.assertIn("matched_corridor_query: Hormuz", articles[0]["raw_text"])

    @patch("pipeline.ingest.gdelt.time.sleep")
    @patch("pipeline.ingest.gdelt.requests.get")
    def test_fetch_survives_request_exception(self, mock_get, mock_sleep):
        mock_get.side_effect = requests.ConnectionError("boom")

        self.assertEqual(gdelt.fetch_gdelt_articles("oil"), [])
        self.assertEqual(mock_get.call_count, gdelt._RETRY_ATTEMPTS)

    @patch("pipeline.ingest.gdelt.time.sleep")
    @patch("pipeline.ingest.gdelt.requests.get")
    def test_fetch_survives_malformed_json(self, mock_get, mock_sleep):
        response = Mock()
        response.raise_for_status = Mock()
        response.json = Mock(side_effect=ValueError("not json"))
        mock_get.return_value = response

        self.assertEqual(gdelt.fetch_gdelt_articles("oil"), [])

    @patch("pipeline.ingest.gdelt.time.sleep")
    @patch("pipeline.ingest.gdelt.requests.get")
    def test_fetch_retries_past_a_throttled_response(self, mock_get, mock_sleep):
        """GDELT answers 429 aggressively; one bad attempt must not lose the cycle."""
        mock_get.side_effect = [
            requests.HTTPError("429 Too Many Requests"),
            _mock_response(GDELT_PAYLOAD),
        ]

        self.assertEqual(len(gdelt.fetch_gdelt_articles("oil")), 2)

    @patch("pipeline.ingest.gdelt.requests.get")
    def test_fetch_skips_records_missing_url_or_title(self, mock_get):
        mock_get.return_value = _mock_response(
            {"articles": [{"url": "https://example.com/a"}, {"title": "no url"}]}
        )

        self.assertEqual(gdelt.fetch_gdelt_articles("oil"), [])

    @patch("pipeline.ingest.gdelt.time.sleep")
    @patch("pipeline.ingest.gdelt.fetch_gdelt_articles")
    def test_fetch_all_corridors_merges_and_dedupes(self, mock_fetch, mock_sleep):
        shared_url = "https://example.com/red-sea-and-cape"

        def fake_fetch(query, corridor_hint=None, **kwargs):
            if corridor_hint == "Hormuz":
                return [{"url": "https://example.com/hormuz-only", "source": "gdelt", "title": "H", "raw_text": "h"}]
            if corridor_hint == "Red Sea":
                return [{"url": shared_url, "source": "gdelt", "title": "R", "raw_text": "r"}]
            if corridor_hint == "Cape":
                return [{"url": shared_url, "source": "gdelt", "title": "C", "raw_text": "c"}]
            return []

        mock_fetch.side_effect = fake_fetch

        articles = gdelt.fetch_all_corridors()

        self.assertEqual(mock_fetch.call_count, 3)
        self.assertEqual({a["url"] for a in articles}, {"https://example.com/hormuz-only", shared_url})
        self.assertEqual(len(articles), 2)  # shared_url counted once despite matching two corridors

    @patch("pipeline.ingest.gdelt.time.sleep")
    @patch("pipeline.ingest.gdelt.fetch_gdelt_articles", return_value=[])
    def test_fetch_all_corridors_queries_every_named_corridor(self, mock_fetch, mock_sleep):
        gdelt.fetch_all_corridors()

        queried_corridors = {call.kwargs["corridor_hint"] for call in mock_fetch.call_args_list}
        self.assertEqual(queried_corridors, {"Hormuz", "Red Sea", "Cape"})

    @patch("pipeline.ingest.gdelt.time.sleep")
    @patch("pipeline.ingest.gdelt.fetch_gdelt_articles", return_value=[])
    def test_fetch_all_corridors_sleeps_between_queries(self, mock_fetch, mock_sleep):
        gdelt.fetch_all_corridors()

        # 3 corridors -> 2 gaps between them, not before the first or after the last
        self.assertEqual(mock_sleep.call_count, 2)

    def test_store_dedupes_by_url(self):
        articles = [
            {"url": "https://example.com/a", "source": "gdelt", "title": "A", "raw_text": "a"},
            {"url": "https://example.com/b", "source": "gdelt", "title": "B", "raw_text": "b"},
        ]

        self.assertEqual(gdelt.store_gdelt_articles(articles), 2)
        self.assertEqual(gdelt.store_gdelt_articles(articles), 0)
        self.assertEqual(RawArticle.objects.count(), 2)

    def test_store_does_not_reset_processed_flag(self):
        article = {"url": "https://example.com/a", "source": "gdelt", "title": "A", "raw_text": "a"}
        gdelt.store_gdelt_articles([article])
        RawArticle.objects.filter(url=article["url"]).update(processed=True)

        gdelt.store_gdelt_articles([article])

        self.assertTrue(RawArticle.objects.get(url=article["url"]).processed)


class RssIngestTests(TestCase):
    @patch("pipeline.ingest.rss.feedparser.parse")
    def test_fetch_returns_normalized_articles(self, mock_parse):
        mock_parse.return_value = _mock_feed(
            [
                {"link": "https://news.example/1", "title": "Oil up", "summary": "prices rose"},
                {"link": "https://news.example/2", "title": "Oil down", "summary": "prices fell"},
            ]
        )

        articles = rss.fetch_rss_feed("https://feed.example/rss", "oilprice")

        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["source"], "oilprice")
        self.assertEqual(articles[0]["raw_text"], "prices rose")

    @patch("pipeline.ingest.rss.feedparser.parse")
    def test_bozo_feed_keeps_entries_without_warning(self, mock_parse):
        mock_parse.return_value = _mock_feed(
            [{"link": "https://news.example/1", "title": "Partial", "summary": "s"}], bozo=1
        )

        with self.assertLogs("pipeline.ingest.rss", level="DEBUG") as logs:
            articles = rss.fetch_rss_feed("https://feed.example/rss", "gcaptain")

        self.assertEqual(len(articles), 1)
        self.assertFalse([line for line in logs.output if line.startswith("WARNING")])

    @patch("pipeline.ingest.rss.feedparser.parse")
    def test_bozo_feed_with_no_entries_warns(self, mock_parse):
        mock_parse.return_value = _mock_feed([], bozo=1)

        with self.assertLogs("pipeline.ingest.rss", level="WARNING"):
            articles = rss.fetch_rss_feed("https://feed.example/rss", "gcaptain")

        self.assertEqual(articles, [])

    @patch("pipeline.ingest.rss.feedparser.parse")
    def test_fetch_survives_parser_exception(self, mock_parse):
        mock_parse.side_effect = OSError("unreachable")

        self.assertEqual(rss.fetch_rss_feed("https://feed.example/rss", "oilprice"), [])

    @patch("pipeline.ingest.rss.feedparser.parse")
    def test_title_truncated_to_field_limit(self, mock_parse):
        mock_parse.return_value = _mock_feed(
            [{"link": "https://news.example/1", "title": "x" * 600, "summary": "s"}]
        )

        articles = rss.fetch_rss_feed("https://feed.example/rss", "oilprice")

        self.assertEqual(len(articles[0]["title"]), 500)

    @patch("pipeline.ingest.rss.feedparser.parse")
    def test_falls_back_to_title_when_no_summary(self, mock_parse):
        mock_parse.return_value = _mock_feed([{"link": "https://news.example/1", "title": "Only title"}])

        articles = rss.fetch_rss_feed("https://feed.example/rss", "oilprice")

        self.assertEqual(articles[0]["raw_text"], "Only title")

    @patch("pipeline.ingest.rss.fetch_rss_feed")
    def test_named_wrappers_pass_correct_source_labels(self, mock_fetch):
        mock_fetch.return_value = []

        rss.fetch_energy_news()
        self.assertEqual(mock_fetch.call_args.args[1], "oilprice")

        rss.fetch_shipping_news()
        self.assertEqual(mock_fetch.call_args.args[1], "gcaptain")

    def test_store_dedupes_by_url(self):
        articles = [
            {"url": "https://news.example/1", "source": "oilprice", "title": "A", "raw_text": "a"}
        ]

        self.assertEqual(rss.store_rss_articles(articles), 1)
        self.assertEqual(rss.store_rss_articles(articles), 0)
        self.assertEqual(RawArticle.objects.count(), 1)


SDN_CSV = (
    '36,"AEROCARIBBEAN AIRLINES","-0- ","CUBA","-0- ","-0- ","-0- ","-0- ","-0- ","-0- ","-0- ","-0- "\n'
    '173,"NATIONAL IRANIAN TANKER COMPANY","-0- ","IRAN","-0- ","-0- ","-0- ","-0- ","-0- ","-0- ","-0- ","-0- "\n'
)


class OfacIngestTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self._tmp.name) / "ofac_sdn_cache.json"
        # OFAC_CACHE_PATH is a module-level constant, so patch it directly
        # rather than via override_settings.
        patcher = patch.object(ofac, "OFAC_CACHE_PATH", self.cache_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    @patch("pipeline.ingest.ofac.requests.get")
    def test_download_parses_entities(self, mock_get):
        response = Mock()
        response.raise_for_status = Mock()
        response.text = SDN_CSV
        mock_get.return_value = response

        entities = ofac.download_ofac_sdn()

        self.assertEqual(len(entities), 2)
        self.assertEqual(entities[1]["name"], "NATIONAL IRANIAN TANKER COMPANY")
        self.assertEqual(entities[1]["program"], "IRAN")
        self.assertEqual(entities[1]["entity_type"], "")  # "-0-" normalized to empty

    @patch("pipeline.ingest.ofac.requests.get")
    def test_download_writes_cache(self, mock_get):
        response = Mock()
        response.raise_for_status = Mock()
        response.text = SDN_CSV
        mock_get.return_value = response

        ofac.download_ofac_sdn()

        payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertIn("fetched_at", payload)
        self.assertEqual(len(payload["entities"]), 2)

    @patch("pipeline.ingest.ofac.requests.get")
    def test_download_survives_request_exception(self, mock_get):
        mock_get.side_effect = requests.Timeout("slow")

        self.assertEqual(ofac.download_ofac_sdn(), [])

    @patch("pipeline.ingest.ofac.requests.get")
    def test_download_skips_malformed_rows(self, mock_get):
        response = Mock()
        response.raise_for_status = Mock()
        response.text = "1,short\n" + SDN_CSV.splitlines()[1] + "\n"
        mock_get.return_value = response

        self.assertEqual(len(ofac.download_ofac_sdn()), 1)

    def test_cache_roundtrip(self):
        ofac.save_ofac_cache([{"name": "ACME", "program": "IRAN"}])

        cache = ofac.load_ofac_cache()

        self.assertEqual(cache["entities"][0]["name"], "ACME")

    def test_load_cache_returns_none_when_missing(self):
        self.assertIsNone(ofac.load_ofac_cache())

    def test_is_sanctioned_matches_case_insensitively(self):
        cache = {"entities": [{"name": "NATIONAL IRANIAN TANKER COMPANY"}]}

        self.assertTrue(ofac.is_sanctioned("national iranian tanker company", cache=cache))
        self.assertTrue(ofac.is_sanctioned("Iranian Tanker", cache=cache))
        self.assertFalse(ofac.is_sanctioned("Saudi Aramco", cache=cache))
        self.assertFalse(ofac.is_sanctioned("", cache=cache))


class TasksTests(TestCase):
    @patch("pipeline.tasks.store_gdelt_articles", return_value=2)
    @patch("pipeline.tasks.fetch_by_corridor")
    def test_poll_gdelt_returns_stored_count(self, mock_fetch, mock_store):
        mock_fetch.return_value = {
            "Hormuz": [{"url": "https://example.com/a"}, {"url": "https://example.com/b"}],
            "Red Sea": [],
            "Cape": [],
        }

        self.assertEqual(tasks.poll_gdelt(), 6)  # store mocked at 2 per corridor call

    @patch("pipeline.tasks.fetch_by_corridor", side_effect=RuntimeError("unexpected"))
    def test_poll_gdelt_never_raises(self, mock_fetch):
        self.assertEqual(tasks.poll_gdelt(), 0)

    @patch("pipeline.tasks.fetch_by_corridor", side_effect=RuntimeError("unexpected"))
    def test_poll_gdelt_by_corridor_reports_zeros_on_total_failure(self, mock_fetch):
        report = tasks.poll_gdelt_by_corridor()

        self.assertEqual(set(report), set(gdelt.CORRIDOR_QUERIES))
        self.assertTrue(all(c["fetched"] == 0 and c["stored"] == 0 for c in report.values()))

    @patch("pipeline.tasks.store_gdelt_articles", return_value=0)
    @patch("pipeline.tasks.fetch_by_corridor")
    def test_poll_gdelt_by_corridor_distinguishes_throttled_from_nothing_new(
        self, mock_fetch, mock_store
    ):
        """fetched=0 means the query failed; stored=0 alone just means no new URLs."""
        mock_fetch.return_value = {
            "Hormuz": [{"url": "https://example.com/a"}],  # fetched but already known
            "Red Sea": [],                                  # throttled / no match
            "Cape": [],
        }

        report = tasks.poll_gdelt_by_corridor()

        self.assertEqual(report["Hormuz"], {"fetched": 1, "stored": 0})
        self.assertEqual(report["Red Sea"], {"fetched": 0, "stored": 0})

    @patch("pipeline.tasks.store_gdelt_articles", return_value=0)
    @patch("pipeline.tasks.fetch_by_corridor")
    def test_poll_gdelt_by_corridor_warns_about_starved_corridors(self, mock_fetch, mock_store):
        mock_fetch.return_value = {"Hormuz": [{"url": "u"}], "Red Sea": [], "Cape": []}

        with self.assertLogs("pipeline.tasks", level="WARNING") as logs:
            tasks.poll_gdelt_by_corridor()

        self.assertIn("Red Sea", logs.output[0])
        self.assertIn("Cape", logs.output[0])

    @patch("pipeline.tasks.store_gdelt_articles", return_value=1)
    @patch("pipeline.tasks.fetch_by_corridor")
    def test_poll_gdelt_stores_a_shared_article_only_once(self, mock_fetch, mock_store):
        shared = {"url": "https://example.com/shared"}
        mock_fetch.return_value = {"Hormuz": [shared], "Red Sea": [shared], "Cape": []}

        tasks.poll_gdelt_by_corridor()

        # Second corridor must hand the store layer an empty list, not the duplicate.
        self.assertEqual(mock_store.call_args_list[0].args[0], [shared])
        self.assertEqual(mock_store.call_args_list[1].args[0], [])

    @patch("pipeline.tasks.store_rss_articles", return_value=3)
    @patch("pipeline.tasks.fetch_shipping_news", return_value=[])
    @patch("pipeline.tasks.fetch_energy_news", return_value=[])
    def test_poll_rss_sums_both_feeds(self, mock_energy, mock_shipping, mock_store):
        self.assertEqual(tasks.poll_rss(), 6)

    @patch("pipeline.tasks.store_rss_articles", return_value=3)
    @patch("pipeline.tasks.fetch_shipping_news", return_value=[])
    @patch("pipeline.tasks.fetch_energy_news", side_effect=RuntimeError("dead feed"))
    def test_poll_rss_continues_when_one_feed_fails(self, mock_energy, mock_shipping, mock_store):
        self.assertEqual(tasks.poll_rss(), 3)

    @patch("pipeline.tasks.download_ofac_sdn", return_value=[{}, {}, {}])
    def test_download_ofac_returns_entity_count(self, mock_download):
        self.assertEqual(tasks.download_ofac(), 3)

    @patch("pipeline.tasks.download_ofac_sdn", side_effect=RuntimeError("unexpected"))
    def test_download_ofac_never_raises(self, mock_download):
        self.assertEqual(tasks.download_ofac(), 0)

    def test_no_celery_coupling(self):
        """Celery is out of scope for this build — keep tasks.py a plain module."""
        source = Path(tasks.__file__).read_text(encoding="utf-8").lower()
        # The module docstring explains why Celery is absent, so only check code.
        code = source.split('"""', 2)[-1]
        for forbidden in ("import celery", "shared_task", ".delay(", "apply_async"):
            self.assertNotIn(forbidden, code, forbidden)


class PollSourcesCommandTests(TestCase):
    @patch("pipeline.tasks.download_ofac_sdn", return_value=[{}])
    @patch("pipeline.tasks.store_rss_articles", return_value=0)
    @patch("pipeline.tasks.fetch_shipping_news", return_value=[])
    @patch("pipeline.tasks.fetch_energy_news", return_value=[])
    @patch("pipeline.tasks.store_gdelt_articles", return_value=1)
    @patch("pipeline.tasks.fetch_by_corridor", return_value={"Hormuz": [{"url": "u"}]})
    def test_command_runs_all_sources(self, *mocks):
        call_command("poll_sources", stdout=StringIO())

    @patch("pipeline.tasks.store_gdelt_articles", return_value=1)
    @patch("pipeline.tasks.fetch_by_corridor", return_value={"Hormuz": [{"url": "u"}]})
    def test_command_runs_single_source(self, mock_fetch, mock_store):
        out = StringIO()

        call_command("poll_sources", source="gdelt", stdout=out)

        mock_fetch.assert_called_once()
        self.assertIn("GDELT", out.getvalue())

    @patch("pipeline.tasks.store_gdelt_articles", return_value=0)
    @patch("pipeline.tasks.fetch_by_corridor")
    def test_command_flags_a_starved_corridor(self, mock_fetch, mock_store):
        mock_fetch.return_value = {"Hormuz": [{"url": "u"}], "Red Sea": [], "Cape": []}
        out = StringIO()

        call_command("poll_sources", source="gdelt", stdout=out)
        output = out.getvalue()

        self.assertIn("Hormuz", output)
        self.assertIn("1 fetched", output)
        self.assertIn("no articles returned", output)

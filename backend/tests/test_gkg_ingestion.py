"""Tests for pipeline/ingest/gdelt_gkg.py — the un-throttled bulk-file path.

Convention follows tests/test_ingestion.py: every outbound call is mocked at the
import site (``pipeline.ingest.gdelt_gkg.requests.get``). No test here touches
the network, and none of them download a real 3 MB slice.

The column layout and field positions asserted below were taken from a real
slice (20260920130000.gkg.csv), not from GDELT's documentation — the docs omit
that the title lives inside V2EXTRASXML rather than in a column of its own.
"""
import io
import zipfile
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.models import RawArticle
from pipeline import tasks
from pipeline.ingest import gdelt, gdelt_gkg

# A real GKG row has 27 tab-separated columns. Only a handful carry anything we
# use, so the helper fills the rest with empties at the right positions — using
# the module's own column constants, so a layout correction can't silently
# desynchronize the fixtures from the parser.
def _gkg_row(url, title, themes="", date="20260920130000", domain="example.com",
             translation="", pubstamp=None, columns=gdelt_gkg.GKG_COLUMNS):
    fields = [""] * gdelt_gkg.GKG_COLUMNS
    fields[gdelt_gkg.COL_DATE] = date
    fields[gdelt_gkg.COL_DOMAIN] = domain
    fields[gdelt_gkg.COL_URL] = url
    fields[gdelt_gkg.COL_THEMES] = themes
    fields[gdelt_gkg.COL_TRANSLATION] = translation
    extras = f"<PAGE_TITLE>{title}</PAGE_TITLE>" if title else ""
    if pubstamp:
        extras = (
            f"<PAGE_PRECISEPUBTIMESTAMP>{pubstamp}</PAGE_PRECISEPUBTIMESTAMP>"
            + extras
        )
    fields[gdelt_gkg.COL_EXTRAS] = extras
    # `columns` below the real width simulates a truncated row.
    return "\t".join(fields[:columns])


def _zip_bytes(*rows):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("20260920130000.gkg.csv", "\n".join(rows))
    return buffer.getvalue()


def _response(payload, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.content = payload
    response.raise_for_status = Mock()
    return response


HORMUZ_ROW = _gkg_row(
    "https://example.com/hormuz-tanker",
    "Iran says struck oil tanker in Strait of Hormuz",
)
RED_SEA_ROW = _gkg_row(
    "https://example.com/houthi-port",
    "Houthis eye oil-rich east, week after seizing Red Sea city",
)


class ParseGkgCsvTests(TestCase):
    def test_extracts_the_fields_we_use(self):
        (record,) = list(gdelt_gkg.parse_gkg_csv(HORMUZ_ROW))

        self.assertEqual(record["url"], "https://example.com/hormuz-tanker")
        self.assertEqual(
            record["title"], "Iran says struck oil tanker in Strait of Hormuz"
        )
        self.assertEqual(record["domain"], "example.com")
        self.assertEqual(
            record["seen_at"], datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc)
        )

    def test_title_comes_from_page_title_not_a_column(self):
        """GKG has no title column — it is buried in V2EXTRASXML. If this ever
        regresses, Phase 3's headline-similarity dedup loses its input."""
        row = _gkg_row("https://example.com/a", "A Headline About Oil Tankers")
        (record,) = list(gdelt_gkg.parse_gkg_csv(row))
        self.assertEqual(record["title"], "A Headline About Oil Tankers")

    def test_rows_without_a_title_are_skipped(self):
        row = _gkg_row("https://example.com/a", "")
        self.assertEqual(list(gdelt_gkg.parse_gkg_csv(row)), [])

    def test_rows_without_a_url_are_skipped(self):
        row = _gkg_row("", "Some Oil Headline")
        self.assertEqual(list(gdelt_gkg.parse_gkg_csv(row)), [])

    def test_translated_rows_are_skipped(self):
        """The DOC path pins sourcelang:eng in every query, so admitting
        translated coverage here would make the two corpora differ for a reason
        unrelated to the corridor filter."""
        row = _gkg_row(
            "https://example.com/a", "Oil Tanker Headline",
            translation="srclc:fra;eng:Moses",
        )
        self.assertEqual(list(gdelt_gkg.parse_gkg_csv(row)), [])

    def test_truncated_rows_are_skipped_not_crashed_on(self):
        short = _gkg_row("https://example.com/a", "Oil", columns=10)
        self.assertEqual(list(gdelt_gkg.parse_gkg_csv(short)), [])

    def test_blank_lines_are_ignored(self):
        text = f"{HORMUZ_ROW}\n\n{RED_SEA_ROW}\n"
        self.assertEqual(len(list(gdelt_gkg.parse_gkg_csv(text))), 2)

    def test_precise_pub_timestamp_is_captured_when_present(self):
        row = _gkg_row(
            "https://example.com/a", "Oil Tanker Headline",
            date="20260920130000", pubstamp="20260918112700",
        )
        (record,) = list(gdelt_gkg.parse_gkg_csv(row))
        self.assertEqual(
            record["published_at"], datetime(2026, 9, 18, 11, 27, tzinfo=timezone.utc)
        )
        self.assertEqual(
            record["seen_at"], datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc)
        )

    def test_html_entities_in_titles_are_decoded(self):
        """GDELT writes PAGE_TITLE without decoding entities. A raw "&#xA0;"
        reaches both the extraction prompt and Phase 3's headline-similarity
        dedup, where it depresses the ratio between two copies of one story."""
        row = _gkg_row(
            "https://example.com/a",
            "Turkiye backs Saudi oil security&#xA0; &amp; shipping",
        )
        (record,) = list(gdelt_gkg.parse_gkg_csv(row))
        self.assertNotIn("&#xA0;", record["title"])
        self.assertIn("&", record["title"])
        self.assertNotIn("&amp;", record["title"])

    def test_unparsable_date_becomes_none_rather_than_raising(self):
        row = _gkg_row("https://example.com/a", "Oil Headline", date="not-a-date")
        (record,) = list(gdelt_gkg.parse_gkg_csv(row))
        self.assertIsNone(record["seen_at"])


class MatchCorridorsTests(TestCase):
    def _record(self, title, url="https://example.com/a", themes=""):
        return {"title": title, "url": url, "themes": themes}

    def test_matches_the_expected_corridor(self):
        self.assertEqual(
            gdelt_gkg.match_corridors(
                self._record("Tanker traffic slows in the Strait of Hormuz")
            ),
            ["Hormuz"],
        )

    def test_corridor_term_alone_is_not_enough(self):
        """Mirrors the second clause of each DOC query — "Red Sea" on its own
        matches tourism and marine biology."""
        self.assertEqual(
            gdelt_gkg.match_corridors(self._record("Red Sea coral reefs recovering")),
            [],
        )

    def test_a_gkg_theme_can_satisfy_the_topic_gate(self):
        """A headline can be about an oil disruption without using any of our
        topic words; GDELT's own codes catch those. The title here deliberately
        contains no TOPIC_KEYWORD, so only the theme route can pass it."""
        record = self._record("What Hormuz means for Delhi", themes="ENV_OIL")
        self.assertEqual(gdelt_gkg.match_corridors(record), ["Hormuz"])

        without_themes = self._record("What Hormuz means for Delhi")
        self.assertEqual(gdelt_gkg.match_corridors(without_themes), [])

    def test_plural_topic_words_still_match(self):
        """Word-boundary matching must not cost plurals: "Tankers divert from
        Hormuz" has to match the singular term list."""
        self.assertEqual(
            gdelt_gkg.match_corridors(self._record("Tankers divert from Hormuz")),
            ["Hormuz"],
        )

    def test_a_topic_word_embedded_in_another_word_does_not_count(self):
        """Measured on the first live run: with substring matching, "port" fired
        on "Riyadh airport" and "supports measures", which let Houthi land-war
        stories in as Red Sea shipping risk."""
        for title in (
            "Flames seen near Riyadh airport, Houthis claim attacks",
            "Yemen government condemns Houthi strikes, supports Riyadh measures",
        ):
            self.assertEqual(gdelt_gkg.match_corridors(self._record(title)), [],
                             msg=title)

    def test_an_implied_corridor_needs_a_maritime_signal_not_just_conflict(self):
        """The two-tier gate. "Houthi" implies Red Sea, but a ballistic missile
        fired at Riyadh is a land war — 24 of the first live run's 27 Red Sea
        articles were this, which is intake, not a hint."""
        land_war = self._record("Saudi-led coalition says Houthis fired missile at Riyadh")
        self.assertEqual(gdelt_gkg.match_corridors(land_war), [])

        maritime = self._record("Houthis hit Saudi Aramco oil facilities in Yanbu")
        self.assertEqual(gdelt_gkg.match_corridors(maritime), ["Red Sea"])

    def test_a_geographic_corridor_name_passes_on_the_looser_gate(self):
        """Closure vocabulary counts for a named corridor: dropping this lost
        "Hormuz to stay closed until US meets conditions", the most on-topic
        headline in the live sample."""
        self.assertEqual(
            gdelt_gkg.match_corridors(
                self._record("Hormuz to stay closed until US meets conditions")
            ),
            ["Hormuz"],
        )

    def test_topic_alone_matches_no_corridor(self):
        self.assertEqual(
            gdelt_gkg.match_corridors(self._record("OPEC raises crude output")), []
        )

    def test_an_article_can_match_two_corridors(self):
        matched = gdelt_gkg.match_corridors(
            self._record("Tankers divert from Hormuz and the Red Sea to the Cape route")
        )
        self.assertEqual(sorted(matched), ["Cape", "Hormuz", "Red Sea"])

    def test_the_url_is_searched_as_well_as_the_title(self):
        matched = gdelt_gkg.match_corridors(
            self._record("Shipping update", url="https://example.com/hormuz-oil-latest")
        )
        self.assertEqual(matched, ["Hormuz"])

    def test_matching_is_case_insensitive(self):
        self.assertEqual(
            gdelt_gkg.match_corridors(self._record("HORMUZ TANKER SEIZED")), ["Hormuz"]
        )


class SliceWindowTests(TestCase):
    NOW = datetime(2026, 9, 20, 14, 5, tzinfo=timezone.utc)

    def test_timestamps_are_aligned_to_quarter_hours(self):
        stamps = gdelt_gkg.slice_timestamps(last_minutes=120, now=self.NOW)
        self.assertTrue(all(s.minute % gdelt_gkg.SLICE_MINUTES == 0 for s in stamps))
        self.assertTrue(all(s.second == 0 for s in stamps))

    def test_recent_slices_are_excluded_because_gdelt_lags(self):
        """Measured: at 14:05 UTC the 13:30 slice was still unpublished even
        though lastupdate.txt already named 14:00. Requesting it buys only 404s."""
        stamps = gdelt_gkg.slice_timestamps(last_minutes=240, now=self.NOW)
        newest = max(stamps)
        cutoff = self.NOW.replace(minute=0, second=0, microsecond=0)
        self.assertLess(newest, cutoff)

    def test_window_is_ordered_oldest_first(self):
        stamps = gdelt_gkg.slice_timestamps(last_minutes=240, now=self.NOW)
        self.assertEqual(stamps, sorted(stamps))

    def test_a_24h_window_is_91_slices_not_96(self):
        """A day holds 96 slices, but the publication lag makes the newest 6
        unavailable, so a 24h window reads 91. Pinned because it is the number
        the command multiplies to warn about download size."""
        stamps = gdelt_gkg.slice_timestamps(last_minutes=1440, now=self.NOW)
        self.assertEqual(len(stamps), 91)

    def test_slice_url_matches_gdelts_naming(self):
        stamp = datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc)
        self.assertEqual(
            gdelt_gkg.slice_url(stamp),
            "https://data.gdeltproject.org/gdeltv2/20260920130000.gkg.csv.zip",
        )


class FetchSliceTests(TestCase):
    STAMP = datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc)

    @patch("pipeline.ingest.gdelt_gkg.requests.get")
    def test_reads_rows_out_of_a_zipped_slice(self, mock_get):
        mock_get.return_value = _response(_zip_bytes(HORMUZ_ROW, RED_SEA_ROW))
        records = gdelt_gkg.fetch_slice(self.STAMP)
        self.assertEqual(len(records), 2)

    @patch("pipeline.ingest.gdelt_gkg.requests.get")
    def test_missing_slice_returns_none_without_retrying(self, mock_get):
        """404 is expected, not exceptional: GDELT lags behind its own pointer
        file and sometimes skips a slice. Retrying it wastes a minute per gap."""
        mock_get.return_value = _response(b"", status_code=404)
        self.assertIsNone(gdelt_gkg.fetch_slice(self.STAMP))
        self.assertEqual(mock_get.call_count, 1)

    @patch("pipeline.ingest.gdelt_gkg.time.sleep")
    @patch("pipeline.ingest.gdelt_gkg.requests.get")
    def test_network_failure_retries_then_gives_up_quietly(self, mock_get, _sleep):
        mock_get.side_effect = requests.ConnectionError("boom")
        self.assertIsNone(gdelt_gkg.fetch_slice(self.STAMP))
        self.assertEqual(mock_get.call_count, gdelt_gkg._RETRY_ATTEMPTS)

    @patch("pipeline.ingest.gdelt_gkg.requests.get")
    def test_corrupt_archive_returns_none_rather_than_raising(self, mock_get):
        mock_get.return_value = _response(b"not a zip file at all")
        self.assertIsNone(gdelt_gkg.fetch_slice(self.STAMP))


class FetchByCorridorTests(TestCase):
    NOW = datetime(2026, 9, 20, 14, 5, tzinfo=timezone.utc)

    # Must exceed PUBLICATION_LAG_MINUTES, or the window resolves to zero
    # slices and fetch_by_corridor short-circuits before reading anything.
    def _run(self, per_slice, last_minutes=240):
        """Drive fetch_by_corridor with a canned per-slice reader."""
        with patch.object(gdelt_gkg, "fetch_slice", side_effect=per_slice):
            return gdelt_gkg.fetch_by_corridor(
                last_minutes=last_minutes, now=self.NOW
            )

    def test_sorts_matches_into_their_corridors(self):
        records = list(gdelt_gkg.parse_gkg_csv(f"{HORMUZ_ROW}\n{RED_SEA_ROW}"))
        result = self._run(lambda stamp, timeout=60: records)

        self.assertEqual(len(result["Hormuz"].articles), 1)
        self.assertEqual(len(result["Red Sea"].articles), 1)
        self.assertEqual(result["Hormuz"].status, gdelt.FETCH_OK)

    def test_a_corridor_with_no_matches_is_empty_not_unsampled(self):
        """The Phase 2.5 distinction: Cape matching nothing here is real evidence
        it is quiet, because its slices were read just like the others'."""
        records = list(gdelt_gkg.parse_gkg_csv(HORMUZ_ROW))
        result = self._run(lambda stamp, timeout=60: records)

        self.assertEqual(result["Cape"].status, gdelt.FETCH_EMPTY)
        self.assertTrue(result["Cape"].sampled)

    def test_urls_are_deduplicated_across_slices(self):
        records = list(gdelt_gkg.parse_gkg_csv(HORMUZ_ROW))
        result = self._run(lambda stamp, timeout=60: records)
        self.assertEqual(len(result["Hormuz"].articles), 1)

    def test_a_multi_corridor_article_is_stored_once(self):
        row = _gkg_row(
            "https://example.com/both",
            "Tankers avoid Hormuz and the Red Sea, taking the Cape route",
        )
        records = list(gdelt_gkg.parse_gkg_csv(row))
        result = self._run(lambda stamp, timeout=60: records)

        total = sum(len(fetch.articles) for fetch in result.values())
        self.assertEqual(total, 1)

    def test_too_few_readable_slices_marks_every_corridor_unsampled(self):
        """The honesty property. If most of the window could not be read, an
        empty corridor is an artefact — reporting it as EMPTY would let a failed
        download masquerade as a quiet corridor, which is exactly the class of
        bug Phase 2.5 existed to kill."""
        result = self._run(lambda stamp, timeout=60: None)

        for corridor, fetch in result.items():
            self.assertFalse(fetch.sampled, corridor)
            self.assertEqual(fetch.status, gdelt.FETCH_ERROR)

    def test_partial_coverage_still_counts_as_sampled(self):
        """Above the coverage floor, a thin window is usable: every corridor was
        filtered from the same slices, so they stay comparable with each other."""
        records = list(gdelt_gkg.parse_gkg_csv(HORMUZ_ROW))
        calls = {"n": 0}

        def reader(stamp, timeout=60):
            calls["n"] += 1
            return records if calls["n"] % 4 else None

        result = self._run(reader, last_minutes=1440)
        self.assertTrue(result["Hormuz"].sampled)

    def test_a_window_shorter_than_the_publication_lag_is_an_error(self):
        """Asking for the last 15 minutes resolves to zero readable slices. That
        has to report as unsampled, not as three quiet corridors."""
        result = gdelt_gkg.fetch_by_corridor(last_minutes=15, now=self.NOW)
        for fetch in result.values():
            self.assertFalse(fetch.sampled)


class RawTextContractTests(TestCase):
    """raw_text must stay interchangeable with the DOC API path's, because
    Phase 3 reads it and does not know which source produced the row."""

    def test_seendate_round_trips_through_the_doc_paths_parser(self):
        """The integration that matters most: gdelt.parse_seendate dates every
        ExtractedEvent. A different key here would silently fall back to ingest
        time and reintroduce the timestamp error fixed on 2026-09-20."""
        record = {
            "title": "Oil tanker struck near Hormuz",
            "url": "https://example.com/a",
            "domain": "example.com",
            "seen_at": datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc),
            "published_at": None,
        }
        raw_text = gdelt_gkg._build_raw_text(record, "Hormuz")

        self.assertEqual(
            gdelt.parse_seendate(raw_text),
            datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc),
        )

    def test_publication_time_is_preferred_over_when_gdelt_saw_it(self):
        record = {
            "title": "Oil tanker struck near Hormuz",
            "url": "https://example.com/a",
            "domain": "example.com",
            "seen_at": datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc),
            "published_at": datetime(2026, 9, 18, 11, 27, tzinfo=timezone.utc),
        }
        raw_text = gdelt_gkg._build_raw_text(record, "Hormuz")

        self.assertEqual(
            gdelt.parse_seendate(raw_text),
            datetime(2026, 9, 18, 11, 27, tzinfo=timezone.utc),
        )

    def test_the_corridor_hint_line_matches_the_doc_paths_wording(self):
        """pipeline/extract/prompt.py names this exact line and tells the model
        to treat it as a weak hint."""
        record = {
            "title": "t", "url": "u", "domain": "d",
            "seen_at": None, "published_at": None,
        }
        self.assertIn(
            "matched_corridor_query: Hormuz",
            gdelt_gkg._build_raw_text(record, "Hormuz"),
        )

    def test_gkg_themes_are_not_leaked_into_the_prompt(self):
        """Deliberate: richer prompts for GKG rows would confound any comparison
        of extraction quality between the two sources."""
        records = list(
            gdelt_gkg.parse_gkg_csv(
                _gkg_row("https://example.com/a", "Hormuz oil tanker",
                         themes="ENV_OIL;MARITIME")
            )
        )
        raw_text = gdelt_gkg._build_raw_text(records[0], "Hormuz")
        self.assertNotIn("ENV_OIL", raw_text)

    def test_source_label_is_distinct_from_the_doc_api_path(self):
        record = {
            "title": "t", "url": "https://example.com/a", "domain": "d",
            "seen_at": None, "published_at": None,
        }
        article = gdelt_gkg._to_article(record, "Hormuz")
        self.assertEqual(article["source"], "gdelt_gkg")
        self.assertNotEqual(article["source"], gdelt.SOURCE_LABEL)


class ExtrasXmlTests(TestCase):
    def test_reads_a_tag_out_of_gdelts_pseudo_xml(self):
        extras = (
            "<PAGE_LINKS>https://a.example/x</PAGE_LINKS>"
            "<PAGE_TITLE>A Headline</PAGE_TITLE>"
        )
        self.assertEqual(gdelt_gkg._extract_tag(extras, "PAGE_TITLE"), "A Headline")

    def test_missing_tag_returns_empty_string(self):
        self.assertEqual(gdelt_gkg._extract_tag("<PAGE_LINKS>x</PAGE_LINKS>",
                                                "PAGE_TITLE"), "")

    def test_empty_extras_is_handled(self):
        self.assertEqual(gdelt_gkg._extract_tag("", "PAGE_TITLE"), "")

    def test_a_title_containing_angle_brackets_does_not_break_the_regex(self):
        """V2EXTRASXML is not real XML — unescaped content appears in practice,
        so a strict parser would reject rows a regex handles."""
        extras = "<PAGE_TITLE>Oil < $60 as Hormuz reopens</PAGE_TITLE>"
        self.assertEqual(
            gdelt_gkg._extract_tag(extras, "PAGE_TITLE"),
            "Oil < $60 as Hormuz reopens",
        )


class StoreAndTaskTests(TestCase):
    def _fetch(self, hormuz=1):
        articles = [
            {
                "url": f"https://example.com/h{i}",
                "source": gdelt_gkg.SOURCE_LABEL,
                "title": f"Hormuz story {i}",
                "raw_text": "Hormuz story\nmatched_corridor_query: Hormuz",
            }
            for i in range(hormuz)
        ]
        return {
            "Hormuz": gdelt.CorridorFetch("Hormuz", articles, gdelt.FETCH_OK),
            "Red Sea": gdelt.CorridorFetch("Red Sea", [], gdelt.FETCH_EMPTY),
            "Cape": gdelt.CorridorFetch("Cape", [], gdelt.FETCH_EMPTY),
        }

    def test_stored_rows_carry_the_gkg_source_label(self):
        gdelt_gkg.store_gkg_articles(self._fetch()["Hormuz"].articles)
        self.assertEqual(
            RawArticle.objects.filter(source="gdelt_gkg").count(), 1
        )

    def test_restoring_the_same_url_creates_nothing(self):
        articles = self._fetch()["Hormuz"].articles
        gdelt_gkg.store_gkg_articles(articles)
        self.assertEqual(gdelt_gkg.store_gkg_articles(articles), 0)

    @patch("pipeline.tasks.fetch_gkg_by_corridor")
    def test_task_reports_the_same_shape_as_the_doc_path(self, mock_fetch):
        mock_fetch.return_value = self._fetch(hormuz=2)
        report = tasks.poll_gdelt_gkg()

        self.assertEqual(set(report), {"Hormuz", "Red Sea", "Cape"})
        self.assertEqual(
            set(report["Hormuz"]), {"fetched", "stored", "status", "sampled"}
        )
        self.assertEqual(report["Hormuz"]["fetched"], 2)
        self.assertEqual(report["Hormuz"]["stored"], 2)
        self.assertTrue(report["Cape"]["sampled"])

    @patch("pipeline.tasks.fetch_gkg_by_corridor")
    def test_task_never_raises(self, mock_fetch):
        mock_fetch.side_effect = RuntimeError("boom")
        report = tasks.poll_gdelt_gkg()

        self.assertTrue(all(not c["sampled"] for c in report.values()))
        self.assertEqual(sum(c["stored"] for c in report.values()), 0)


class CommandTests(TestCase):
    @patch("pipeline.tasks.fetch_gkg_by_corridor")
    def test_source_gkg_runs_the_bulk_path(self, mock_fetch):
        mock_fetch.return_value = {
            c: gdelt.CorridorFetch(c, [], gdelt.FETCH_EMPTY)
            for c in gdelt_gkg.CORRIDOR_KEYWORDS
        }
        call_command("poll_sources", "--source", "gkg")
        self.assertEqual(mock_fetch.call_count, 1)

    @patch("pipeline.tasks.fetch_gkg_by_corridor")
    @patch("pipeline.tasks.download_ofac_sdn", return_value=[])
    @patch("pipeline.tasks.fetch_by_corridor")
    @patch("pipeline.tasks.fetch_shipping_news", return_value=[])
    @patch("pipeline.tasks.fetch_energy_news", return_value=[])
    def test_source_all_does_not_trigger_a_300mb_download(
        self, _energy, _shipping, mock_doc, _ofac, mock_gkg
    ):
        """--source all must stay cheap. GKG is opt-in precisely because a
        24h window is ~96 downloads."""
        mock_doc.return_value = {
            c: gdelt.CorridorFetch(c, [], gdelt.FETCH_EMPTY)
            for c in gdelt.CORRIDOR_QUERIES
        }
        call_command("poll_sources", "--source", "all")
        mock_gkg.assert_not_called()

    @patch("pipeline.tasks.fetch_gkg_by_corridor")
    def test_last_minutes_is_passed_through(self, mock_fetch):
        mock_fetch.return_value = {
            c: gdelt.CorridorFetch(c, [], gdelt.FETCH_EMPTY)
            for c in gdelt_gkg.CORRIDOR_KEYWORDS
        }
        call_command("poll_sources", "--source", "gkg", "--last-minutes", "240")
        self.assertEqual(mock_fetch.call_args.kwargs["last_minutes"], 240)

    def test_a_window_inside_the_publication_lag_is_rejected_with_a_reason(self):
        """Better than running and reporting an unsampled window: the operator
        gets told the request itself was impossible."""
        with self.assertRaises(CommandError) as caught:
            call_command("poll_sources", "--source", "gkg", "--last-minutes", "60")
        self.assertIn("publishes its bulk files with a lag", str(caught.exception))

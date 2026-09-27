"""Tests for pipeline/extract/*.

Convention: no test here may reach the network or spend an API call. Phase 2
patched the HTTP library at its import site; the LLM client is built lazily
inside ``extractor._get_client`` (so the package is only needed when extraction
actually runs), so the seam is that function — and ``extractor._call_llm`` for
tests that only care about what happens to a response once it arrives.

Calls go to OpenRouter via the openai SDK (OpenAI-compatible wire protocol), so
the mocks are shaped like an OpenAI chat-completions response either way.
"""
import json
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.gis.geos import LineString
from django.test import TestCase

from core.models import Corridor, ExtractedEvent, RawArticle
from pipeline.extract import extractor
from pipeline.extract.prompt import build_prompt


def _response(**overrides):
    payload = {
        "corridor": "Hormuz",
        "actor": "Iran",
        "event_type": "military",
        "severity": 4,
        "confidence": 0.9,
        "is_relevant": True,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _article(url="https://example.com/a", text="Tankers rerouted around Hormuz"):
    return RawArticle.objects.create(
        url=url, source="gdelt", title="headline", raw_text=text,
    )


class PromptTests(TestCase):
    def test_article_text_is_inserted(self):
        self.assertIn("Tanker seized", build_prompt("Tanker seized"))

    def test_json_schema_braces_survive(self):
        # str.format would raise on these; build_prompt must use str.replace.
        prompt = build_prompt("text with {braces} in it")
        self.assertIn('"corridor"', prompt)
        self.assertIn("{braces}", prompt)

    def test_suez_is_not_an_allowed_answer(self):
        prompt = build_prompt("x")
        self.assertIn('answer "Suez"', prompt)
        self.assertIn('"corridor": "Hormuz" | "Red Sea" | "Cape" | "None"', prompt)


class ParseExtractionTests(TestCase):
    def test_valid_json(self):
        result = extractor.parse_extraction(_response())
        self.assertEqual(result["corridor"], "Hormuz")
        self.assertEqual(result["event_type"], "military")
        self.assertEqual(result["severity"], 4)
        self.assertTrue(result["is_relevant"])

    def test_markdown_fenced_json(self):
        text = f"```json\n{_response()}\n```"
        self.assertEqual(extractor.parse_extraction(text)["corridor"], "Hormuz")

    def test_json_with_prose_around_it(self):
        text = f"Here is the extraction:\n{_response()}\nHope that helps."
        self.assertEqual(extractor.parse_extraction(text)["actor"], "Iran")

    def test_unrecoverable_text_returns_none(self):
        self.assertIsNone(extractor.parse_extraction("I cannot help with that."))

    def test_empty_response_returns_none(self):
        self.assertIsNone(extractor.parse_extraction(""))
        self.assertIsNone(extractor.parse_extraction(None))

    def test_json_array_returns_none(self):
        self.assertIsNone(extractor.parse_extraction('[{"corridor": "Hormuz"}]'))

    def test_suez_maps_to_red_sea(self):
        self.assertEqual(
            extractor.parse_extraction(_response(corridor="Suez"))["corridor"], "Red Sea"
        )

    def test_corridor_synonyms_map_to_model_names(self):
        for answer, expected in [
            ("Strait of Hormuz", "Hormuz"),
            ("Bab-el-Mandeb", "Red Sea"),
            ("Cape of Good Hope", "Cape"),
        ]:
            with self.subTest(answer=answer):
                parsed = extractor.parse_extraction(_response(corridor=answer))
                self.assertEqual(parsed["corridor"], expected)

    def test_unknown_corridor_becomes_none(self):
        self.assertEqual(
            extractor.parse_extraction(_response(corridor="Panama Canal"))["corridor"], "None"
        )

    def test_unknown_event_type_becomes_other(self):
        self.assertEqual(
            extractor.parse_extraction(_response(event_type="cyberattack"))["event_type"],
            "other",
        )

    def test_out_of_range_values_are_clamped(self):
        parsed = extractor.parse_extraction(_response(severity=9, confidence=1.7))
        self.assertEqual(parsed["severity"], 5)
        self.assertEqual(parsed["confidence"], 1.0)

        parsed = extractor.parse_extraction(_response(severity=0, confidence=-0.3))
        self.assertEqual(parsed["severity"], 1)
        self.assertEqual(parsed["confidence"], 0.0)

    def test_non_numeric_values_fall_back(self):
        parsed = extractor.parse_extraction(_response(severity="high", confidence="very"))
        self.assertEqual(parsed["severity"], 1)
        self.assertEqual(parsed["confidence"], 0.1)

    def test_missing_actor_becomes_unknown(self):
        parsed = extractor.parse_extraction(_response(actor=None))
        self.assertEqual(parsed["actor"], "unknown")

    def test_long_actor_is_truncated_to_field_width(self):
        parsed = extractor.parse_extraction(_response(actor="x" * 400))
        self.assertEqual(len(parsed["actor"]), 200)


class CallLlmTests(TestCase):
    @patch("pipeline.extract.extractor._get_client")
    def test_returns_message_content(self, get_client):
        message = Mock()
        message.content = _response()
        get_client.return_value.chat.completions.create.return_value = Mock(
            choices=[Mock(message=message)]
        )
        self.assertEqual(extractor._call_llm("prompt"), _response())

    @patch("pipeline.extract.extractor._get_client")
    def test_api_failure_returns_none(self, get_client):
        get_client.side_effect = RuntimeError("no API key")
        self.assertIsNone(extractor._call_llm("prompt"))

    @patch("pipeline.extract.extractor._get_client")
    def test_requests_json_object_mode(self, get_client):
        message = Mock()
        message.content = _response()
        create = get_client.return_value.chat.completions.create
        create.return_value = Mock(choices=[Mock(message=message)])

        extractor._call_llm("prompt")

        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(kwargs["max_tokens"], settings.LLM_MAX_TOKENS)
        self.assertEqual(kwargs["model"], settings.LLM_MODEL)
        self.assertEqual(kwargs["temperature"], 0)

    @patch("pipeline.extract.extractor._get_client")
    def test_reasoning_is_disabled_so_it_cannot_eat_the_token_budget(self, get_client):
        message = Mock()
        message.content = _response()
        create = get_client.return_value.chat.completions.create
        create.return_value = Mock(choices=[Mock(message=message)])

        extractor._call_llm("prompt")

        self.assertEqual(
            create.call_args.kwargs["extra_body"], {"reasoning": {"enabled": False}}
        )

    def test_client_targets_openrouter(self):
        with patch("openai.OpenAI") as openai_cls:
            with self.settings(LLM_API_KEY="sk-or-test"):
                extractor._get_client()
        self.assertEqual(
            openai_cls.call_args.kwargs["base_url"], settings.LLM_BASE_URL
        )
        self.assertEqual(openai_cls.call_args.kwargs["api_key"], "sk-or-test")

    def test_missing_key_is_reported_clearly(self):
        with self.settings(LLM_API_KEY=""):
            with self.assertRaisesMessage(RuntimeError, "OPENROUTER_API_KEY"):
                extractor._get_client()


class ExtractPendingEventsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        line = LineString((56.0, 26.0), (57.0, 27.0))
        for name, baseline in (("Hormuz", 0.20), ("Red Sea", 0.15), ("Cape", 0.05)):
            Corridor.objects.create(
                name=name, geometry=line, capacity_mbd=10.0,
                transit_days=10, baseline_risk=baseline,
            )

    @patch("pipeline.extract.extractor._call_llm")
    def test_event_timestamp_uses_gdelt_seendate_not_ingest_time(self, call_llm):
        """GDELT returns articles it first saw days earlier, so dating an event
        by ingest time over-weights it under the 0.1/day decay."""
        call_llm.return_value = _response()
        _article(text="Tanker seized\nseendate: 20260913T064500Z")

        extractor.extract_pending_events()

        event = ExtractedEvent.objects.get()
        self.assertEqual(event.timestamp.year, 2026)
        self.assertEqual(event.timestamp.month, 9)
        self.assertEqual(event.timestamp.day, 13)
        self.assertEqual(event.timestamp.hour, 6)

    @patch("pipeline.extract.extractor._call_llm")
    def test_event_timestamp_falls_back_to_ingest_time_without_a_seendate(self, call_llm):
        """RSS rows carry no publication date, so they must still get one."""
        call_llm.return_value = _response()
        article = _article(text="Tanker seized, no date here")

        extractor.extract_pending_events()

        self.assertEqual(ExtractedEvent.objects.get().timestamp, article.ingested_at)

    @patch("pipeline.extract.extractor._call_llm")
    def test_relevant_article_creates_event_and_marks_processed(self, call_llm):
        call_llm.return_value = _response()
        article = _article()

        counts = extractor.extract_pending_events()

        self.assertEqual(counts, {
            "articles": 1, "events": 1, "irrelevant": 0,
            "unparseable": 0, "call_failed": 0,
        })
        event = ExtractedEvent.objects.get()
        self.assertEqual(event.corridor.name, "Hormuz")
        self.assertEqual(event.actor, "Iran")
        self.assertEqual(event.article_url, article.url)
        article.refresh_from_db()
        self.assertTrue(article.processed)

    @patch("pipeline.extract.extractor._call_llm")
    def test_event_timestamp_comes_from_ingest_time(self, call_llm):
        call_llm.return_value = _response()
        article = _article()

        extractor.extract_pending_events()

        self.assertEqual(ExtractedEvent.objects.get().timestamp, article.ingested_at)

    @patch("pipeline.extract.extractor._call_llm")
    def test_irrelevant_article_stores_no_event_but_is_processed(self, call_llm):
        call_llm.return_value = _response(is_relevant=False)
        article = _article()

        counts = extractor.extract_pending_events()

        self.assertEqual(counts["irrelevant"], 1)
        self.assertEqual(ExtractedEvent.objects.count(), 0)
        article.refresh_from_db()
        self.assertTrue(article.processed)

    @patch("pipeline.extract.extractor._call_llm")
    def test_call_failure_leaves_article_for_retry(self, call_llm):
        call_llm.return_value = None
        article = _article()

        counts = extractor.extract_pending_events()

        self.assertEqual(counts["call_failed"], 1)
        self.assertEqual(ExtractedEvent.objects.count(), 0)
        article.refresh_from_db()
        self.assertFalse(article.processed)

    @patch("pipeline.extract.extractor._call_llm")
    def test_unparseable_answer_is_not_retried(self, call_llm):
        call_llm.return_value = "I cannot help with that."
        article = _article()

        counts = extractor.extract_pending_events()

        self.assertEqual(counts["unparseable"], 1)
        self.assertEqual(ExtractedEvent.objects.count(), 0)
        article.refresh_from_db()
        self.assertTrue(article.processed)

    @patch("pipeline.extract.extractor._call_llm")
    def test_crash_mid_batch_does_not_abort_the_run(self, call_llm):
        call_llm.side_effect = [RuntimeError("boom"), _response()]
        _article(url="https://example.com/1")
        _article(url="https://example.com/2")

        counts = extractor.extract_pending_events()

        self.assertEqual(counts["articles"], 2)
        self.assertEqual(counts["events"], 1)
        self.assertEqual(counts["call_failed"], 1)

    @patch("pipeline.extract.extractor._call_llm")
    def test_corridorless_event_stores_null_fk(self, call_llm):
        call_llm.return_value = _response(corridor="None")
        _article()

        extractor.extract_pending_events()

        self.assertIsNone(ExtractedEvent.objects.get().corridor)

    @patch("pipeline.extract.extractor._call_llm")
    def test_limit_caps_api_calls(self, call_llm):
        call_llm.return_value = _response()
        for i in range(5):
            _article(url=f"https://example.com/{i}")

        counts = extractor.extract_pending_events(limit=2)

        self.assertEqual(counts["articles"], 2)
        self.assertEqual(call_llm.call_count, 2)
        self.assertEqual(RawArticle.objects.filter(processed=False).count(), 3)

    @patch("pipeline.extract.extractor._call_llm")
    def test_already_processed_articles_are_skipped(self, call_llm):
        call_llm.return_value = _response()
        RawArticle.objects.create(
            url="https://example.com/done", source="gdelt", title="t",
            raw_text="x", processed=True,
        )

        counts = extractor.extract_pending_events()

        self.assertEqual(counts["articles"], 0)
        call_llm.assert_not_called()

    @patch("pipeline.extract.extractor._call_llm")
    def test_no_articles_returns_zero_counts(self, call_llm):
        counts = extractor.extract_pending_events()
        self.assertEqual(counts["articles"], 0)
        call_llm.assert_not_called()


def _answer_by_content(prompt):
    """Answer keyed on the article text, since concurrent calls complete in
    no fixed order and an ordered side_effect list would be meaningless."""
    if "IRRELEVANT" in prompt:
        return _response(is_relevant=False)
    if "FAILS" in prompt:
        return None
    if "GARBAGE" in prompt:
        return "I cannot help with that."
    if "CRASHES" in prompt:
        raise RuntimeError("boom")
    return _response()


class ConcurrentExtractionTests(TestCase):
    """``workers > 1``: same outcomes as serial, calls genuinely overlap, and
    every database write still happens on the main thread."""

    @classmethod
    def setUpTestData(cls):
        line = LineString((56.0, 26.0), (57.0, 27.0))
        for name, baseline in (("Hormuz", 0.20), ("Red Sea", 0.15), ("Cape", 0.05)):
            Corridor.objects.create(
                name=name, geometry=line, capacity_mbd=10.0,
                transit_days=10, baseline_risk=baseline,
            )

    def _mixed_batch(self):
        texts = {"ok1": "Hormuz ok", "ok2": "Hormuz ok again", "irr": "IRRELEVANT story",
                 "fail": "FAILS to answer", "junk": "GARBAGE answer", "boom": "CRASHES"}
        return {key: _article(url=f"https://example.com/{key}", text=t)
                for key, t in texts.items()}

    @patch("pipeline.extract.extractor._call_llm", side_effect=_answer_by_content)
    def test_outcomes_match_the_serial_contract(self, _call):
        articles = self._mixed_batch()
        counts = extractor.extract_pending_events(workers=4)
        self.assertEqual(counts, {
            "articles": 6, "events": 2, "irrelevant": 1,
            "unparseable": 1, "call_failed": 2,
        })
        processed = {k: RawArticle.objects.get(pk=a.pk).processed for k, a in articles.items()}
        # transient failures stay pending for a retry; answered ones are done
        self.assertEqual(processed, {"ok1": True, "ok2": True, "irr": True,
                                     "junk": True, "fail": False, "boom": False})

    @patch("pipeline.extract.extractor._call_llm", side_effect=lambda p: _response())
    def test_every_article_is_called_exactly_once(self, call_llm):
        for i in range(23):
            _article(url=f"https://example.com/{i}")
        counts = extractor.extract_pending_events(workers=4)
        self.assertEqual(call_llm.call_count, 23)
        self.assertEqual(counts["events"], 23)
        self.assertEqual(ExtractedEvent.objects.count(), 23)
        self.assertEqual(len(set(ExtractedEvent.objects.values_list("article_url", flat=True))), 23)

    def test_calls_actually_run_concurrently(self):
        # Each call blocks until 4 are waiting at once. Serial execution can
        # never get there: the barrier times out, the call "fails", and the
        # assertions below catch it.
        import threading
        barrier = threading.Barrier(4, timeout=5)

        def gated(prompt):
            barrier.wait()
            return _response()

        for i in range(8):
            _article(url=f"https://example.com/{i}")
        with patch("pipeline.extract.extractor._call_llm", side_effect=gated):
            counts = extractor.extract_pending_events(workers=4)
        self.assertEqual(counts["call_failed"], 0)
        self.assertEqual(counts["events"], 8)

    @patch("pipeline.extract.extractor._call_llm", side_effect=lambda p: _response())
    def test_progress_reports_periodically_and_at_the_end(self, _call):
        for i in range(5):
            _article(url=f"https://example.com/{i}")
        seen = []
        extractor.extract_pending_events(
            workers=2, progress_every=2,
            on_progress=lambda counts, total: seen.append((counts["articles"], total)),
        )
        self.assertEqual(seen, [(2, 5), (4, 5), (5, 5)])

    @patch("pipeline.extract.extractor._call_llm", side_effect=lambda p: _response())
    def test_progress_is_not_repeated_when_total_is_a_multiple(self, _call):
        for i in range(4):
            _article(url=f"https://example.com/{i}")
        seen = []
        extractor.extract_pending_events(
            progress_every=2, on_progress=lambda counts, total: seen.append(counts["articles"]),
        )
        self.assertEqual(seen, [2, 4])


class ExtractEventsCommandTests(TestCase):

    @patch("core.management.commands.extract_events.extract_events")
    def test_workers_reach_the_task(self, task):
        from io import StringIO
        from django.core.management import call_command
        task.return_value = {"articles": 0, "events": 0, "irrelevant": 0,
                             "unparseable": 0, "call_failed": 0}
        call_command("extract_events", "--workers", "6", stdout=StringIO())
        self.assertEqual(task.call_args.kwargs["workers"], 6)

    def test_zero_workers_is_rejected(self):
        from io import StringIO
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("extract_events", "--workers", "0", stdout=StringIO())

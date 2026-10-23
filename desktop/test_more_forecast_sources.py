"""Small fixtures matching public schemas observed on 2026-10-23.

Dates, IDs and edge-case announcements below are test data, not live forecasts.
No test requests a website, a reset-card endpoint or any user's account.
"""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from forecast_sources import (MAX_EVENTS_PER_SOURCE, POLL_SECONDS, SOURCES,
                              _source_result, parse_source)

NOW = datetime(2026, 10, 23, 6, 50, tzinfo=timezone.utc)
FUTURE = (NOW + timedelta(hours=2)).isoformat()
ORIGINAL = "https://x.com/thsottiaux/status/1234567890"


def source(adapter):
    return next(s for s in SOURCES if s["adapter"] == adapter)


def dev_item(**changes):
    item = {"id": "archive-fixture", "type": "global_reset", "status": "recorded",
            "title": "Codex global quota reset recorded", "announced_at": NOW.isoformat(),
            "effective_at": FUTURE, "time_precision": "minute", "time_basis": "announcement",
            "source_url": ORIGINAL, "source_type": "community", "origin": "codex_resets",
            "evidence_text": "Codex usage limits have been reset.",
            "notes": "Archived through the Codex Resets public API."}
    item.update(changes)
    return item


def dev_payload(items=None, **changes):
    data = {"status": "no_verified_reset", "events": items if items is not None else [dev_item()],
            "lastChecked": NOW.isoformat(), "generatedAt": NOW.isoformat(),
            "resetArchiveSyncedAt": (NOW - timedelta(minutes=5)).isoformat(),
            "sources": [{"id": "codex-resets", "status": "online"}],
            "forecast": {"probability_24h": None, "probability_48h": None, "confidence": "insufficient"}}
    data.update(changes)
    return data


def tibo_item(**changes):
    item = {"id": "x:fixture", "publishedAt": NOW.isoformat(), "firstObservedAt": FUTURE,
            "title": "公开预告 / 完成 / 观察记录", "quote": "Landing tomorrow before 10am PST together with a small ship.",
            "textType": "relayed-quote", "kind": "regular-record", "contextReset": True,
            "originalUrl": ORIGINAL, "sourceUrl": "https://codex-resets.com/api/v1/status",
            "sourceIds": ["watch", "replies", "direct"], "upstreamStage": "planned", "stage": "planned"}
    item.update(changes)
    return item


def tibo_payload(items=None, **changes):
    data = {"schemaVersion": 2, "snapshotAt": NOW.isoformat(), "servedAt": FUTURE, "stale": False,
            "current": {"state": "public-signals-only", "confirmedAt": None, "nextResetAt": FUTURE},
            "forecast": {"windowEnd": FUTURE, "probability": 0.99},
            "events": items if items is not None else [tibo_item()]}
    data.update(changes)
    return data


def app_row(title="Codex quota reset update", badge="Reset announced", text="Codex Resets tracks an upcoming reset awaiting execution evidence.", original=ORIGINAL):
    return ('<article class="event-row"><time class="event-date" dateTime="' + NOW.isoformat() + '"></time>'
            '<div class="event-content"><div class="event-title"><a href="/events/fixture"><h3>' + title + '</h3></a>'
            '<span class="badge badge-amber">' + badge + '</span></div><p>' + text + '</p>'
            '<div class="event-meta"><a class="text-link" href="' + original + '">View source</a>'
            '<span class="calibration-label">Auto-published · Not manually checked</span></div></div></article>')


def app_page(rows=None):
    return ('<html><body><article class="hero"><time class="reset-hero-time" datetime="' + FUTURE + '">estimate</time></article>'
            + (app_row() if rows is None else rows)
            + '<div class="reset-overview-footer"><time dateTime="' + NOW.isoformat() + '">Last synced</time></div>'
            '<a href="/?page=2">Next</a><script>{"nextResetAt":"' + FUTURE + '"}</script></body></html>')


class MoreForecastSourceTests(unittest.TestCase):
    def parse(self, adapter, payload):
        if not isinstance(payload, str):
            payload = json.dumps(payload)
        return parse_source(source(adapter), payload, now=NOW)

    def test_four_new_sites_have_distinct_https_hosts_and_dependency_labels(self):
        additions = SOURCES[4:]
        self.assertEqual(len(additions), 4)
        self.assertEqual(len({s["requestUrl"].split("/")[2] for s in additions}), 4)
        self.assertTrue(all(s.get("sourceGroupReason") for s in additions))

    def test_dev_real_archive_schema_does_not_count_effective_time(self):
        rows, meta = self.parse("codexreset-dev", dev_payload())
        self.assertEqual(rows[0]["status"], "completed")
        self.assertIsNone(rows[0]["resetAt"])
        self.assertEqual(rows[0]["confidence"], "medium")
        self.assertEqual(rows[0]["sourceGroup"], "codex-resets")
        self.assertEqual(meta["snapshotAt"], (NOW - timedelta(minutes=5)).isoformat())

    def test_dev_archive_with_old_future_wording_remains_history(self):
        rows, _ = self.parse("codexreset-dev", dev_payload([dev_item(evidence_text="Codex will reset at " + FUTURE)]))
        self.assertEqual(rows[0]["status"], "recorded")
        self.assertIsNone(rows[0]["resetAt"])

    def test_dev_confirmed_date_only_launch_never_uses_effective_at(self):
        rows, _ = self.parse("codexreset-dev", dev_payload([dev_item(type="banked_reset", status="confirmed", evidence_text="Banked resets are now available.", time_precision="date", effective_at="2026-06-11T00:00:00Z")]))
        self.assertEqual(rows[0]["eventKind"], "grant")
        self.assertEqual(rows[0]["status"], "completed")
        self.assertIsNone(rows[0]["resetAt"])

    def test_dev_observation_template_does_not_become_confirmed_completion(self):
        for changes in ({"time_basis": "observation"}, {"source_record_id": "observed-fixture"}, {"source_author": "Codex Resets observation"}):
            with self.subTest(changes=changes):
                rows, _ = self.parse("codexreset-dev", dev_payload([dev_item(**changes)]))
                self.assertEqual(rows[0]["status"], "recorded")
                self.assertEqual(rows[0]["confidence"], "low")
                self.assertIsNone(rows[0]["resetAt"])

    def test_dev_prediction_probability_does_not_create_an_event_or_time(self):
        rows, _ = self.parse("codexreset-dev", dev_payload([], forecast={"probability_24h": 1.0, "confidence": "high", "next_reset_at": FUTURE}))
        self.assertEqual(rows, [])
        rows, _ = self.parse("codexreset-dev", dev_payload([dev_item(status="unknown", evidence_text="Codex reset signal", effective_at=FUTURE)], forecast={"probability_24h": 1.0}))
        self.assertIsNone(rows[0]["resetAt"])
        self.assertEqual(rows[0]["status"], "unknown")

    def test_dev_primary_text_outranks_conflicting_structured_estimate(self):
        rows, _ = self.parse("codexreset-dev", dev_payload([dev_item(status="planned", evidence_text="Codex will reset at " + FUTURE, effective_at=(NOW + timedelta(days=1)).isoformat())]))
        self.assertEqual(rows[0]["resetAt"], FUTURE)
        self.assertEqual(rows[0]["status"], "upcoming")
        self.assertNotEqual(rows[0]["confidence"], "high")

    def test_dev_unknown_and_failed_dependency_are_stale_despite_new_generated_time(self):
        for changes in ({"status": "unknown"}, {"sources": [{"id": "codex-resets", "status": "error"}]}):
            with self.subTest(changes=changes):
                _, meta = self.parse("codexreset-dev", dev_payload(**changes))
                self.assertTrue(meta["upstreamStale"])

    def test_dev_schema_change_fails_visibly(self):
        for value in ({"events": []}, {"status": "unknown", "events": {}}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse("codexreset-dev", value)

    def test_tibo_real_planned_pst_quote_stays_ambiguous(self):
        rows, meta = self.parse("tibo-watch", tibo_payload())
        self.assertEqual(rows[0]["status"], "upcoming")
        self.assertIsNone(rows[0]["resetAt"])
        self.assertEqual(rows[0]["timeKind"], "window")
        self.assertEqual(rows[0]["confidence"], "low")
        self.assertEqual(meta["snapshotAt"], NOW.isoformat())

    def test_tibo_forecast_model_observation_and_served_times_are_ignored(self):
        rows, _ = self.parse("tibo-watch", tibo_payload([tibo_item(quote="Codex reset signal", stage="hint")]))
        self.assertEqual(rows[0]["status"], "unknown")
        self.assertIsNone(rows[0]["resetAt"])

    def test_tibo_explicit_quote_can_support_future_but_not_high_confidence(self):
        rows, _ = self.parse("tibo-watch", tibo_payload([tibo_item(quote="Codex will reset at " + FUTURE, confidence="high", probability=1.0)]))
        self.assertEqual(rows[0]["status"], "upcoming")
        self.assertEqual(rows[0]["resetAt"], FUTURE)
        self.assertEqual(rows[0]["confidence"], "medium")
        self.assertEqual(rows[0]["originalUrl"], ORIGINAL)

    def test_tibo_completed_and_cancelled_win_over_quoted_plan(self):
        for stage, suffix, expected in (("completed", "", "completed"), ("planned", " The reset is cancelled.", "cancelled"), ("planned", " It is done.", "completed")):
            with self.subTest(stage=stage, suffix=suffix):
                rows, _ = self.parse("tibo-watch", tibo_payload([tibo_item(quote="Codex will reset at " + FUTURE + suffix, stage=stage)]))
                self.assertEqual(rows[0]["status"], expected)

    def test_tibo_cancelled_wins_over_completed(self):
        rows, _ = self.parse("tibo-watch", tibo_payload([tibo_item(quote="Codex reset cancelled.", stage="completed")]))
        self.assertEqual(rows[0]["status"], "cancelled")

    def test_tibo_future_completion_is_not_an_already_completed_activity(self):
        for text in ("Codex will reset and will be completed at " + FUTURE, "Codex reset will be done by " + FUTURE,
                     "Codex reset upcoming, not yet completed", "The feature is done but Codex reset will land at " + FUTURE,
                     "Codex will reset at " + FUTURE + "; not cancelled", "Codex will reset at " + FUTURE + "; has not been cancelled"):
            with self.subTest(text=text):
                rows, _ = self.parse("tibo-watch", tibo_payload([tibo_item(quote=text)]))
                self.assertEqual(rows[0]["status"], "upcoming")

    def test_tibo_old_ambiguous_and_elapsed_plans_are_history(self):
        for quote in ("Landing tomorrow before 10am PST", "Codex will reset at 2026-08-05T01:00:00Z", "Reset landing within the next 2 hours"):
            with self.subTest(quote=quote):
                rows, _ = self.parse("tibo-watch", tibo_payload([tibo_item(quote=quote, publishedAt="2026-08-04T01:00:00Z")]))
                self.assertEqual(rows[0]["status"], "recorded")
                self.assertIsNone(rows[0]["resetAt"])

    def test_tibo_service_and_non_activity_items_are_ignored(self):
        rows, _ = self.parse("tibo-watch", tibo_payload([tibo_item(kind="auxiliary"), tibo_item(stage="auxiliary"), tibo_item(contextReset=False, quote="Product news")]))
        self.assertEqual(rows, [])

    def test_tibo_no_primary_link_is_low_and_uses_safe_source_detail(self):
        rows, _ = self.parse("tibo-watch", tibo_payload([tibo_item(originalUrl=None, sourceUrl="https://whennextreset.com/resets/fixture")]))
        self.assertEqual(rows[0]["confidence"], "low")
        self.assertEqual(rows[0]["originalUrl"], "https://whennextreset.com/resets/fixture")

    def test_tibo_stale_and_unsupported_schema_are_visible(self):
        _, meta = self.parse("tibo-watch", tibo_payload(stale=True))
        self.assertTrue(meta["upstreamStale"])
        with self.assertRaises(ValueError):
            self.parse("tibo-watch", tibo_payload(schemaVersion=3))

    def test_when_next_reset_real_rss_schema_is_history_without_invented_primary(self):
        feed = '<rss version="2.0"><channel><title>When Next Reset</title><item><title>Codex reset recorded · 22 Oct 2026</title><link>https://whennextreset.com/resets/observed-fixture</link><guid>history-fixture</guid><pubDate>Thu, 22 Oct 2026 17:00:00 +0000</pubDate><description>Codex usage limits have been reset. Check your limits.</description></item></channel></rss>'
        rows, _ = self.parse("whennextreset", feed)
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["publishedAt"], "2026-10-22T17:00:00+00:00")
        self.assertEqual(rows[0]["confidence"], "low")
        self.assertIsNone(rows[0]["resetAt"])
        self.assertEqual(rows[0]["originalUrl"], "https://whennextreset.com/resets/observed-fixture")

    def test_when_next_reset_does_not_promote_an_archived_future_quote(self):
        feed = '<rss><channel><item><title>Codex reset recorded</title><link>https://whennextreset.com/resets/fixture</link><description>Codex will reset at ' + FUTURE + '</description></item></channel></rss>'
        rows, _ = self.parse("whennextreset", feed)
        self.assertEqual(rows[0]["status"], "recorded")
        self.assertIsNone(rows[0]["resetAt"])

    def test_when_next_reset_cancelled_precedes_history_and_feed_root_is_checked(self):
        rows, _ = self.parse("whennextreset", '<rss><channel><item><title>Codex reset cancelled</title></item></channel></rss>')
        self.assertEqual(rows[0]["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.parse("whennextreset", '<html><item><title>Codex reset</title></item></html>')

    def test_app_real_html_shape_stays_window_not_hero_time(self):
        rows, meta = self.parse("codex-reset-app", app_page())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "upcoming")
        self.assertEqual(rows[0]["timeKind"], "window")
        self.assertEqual(rows[0]["confidence"], "low")
        self.assertIsNone(rows[0]["resetAt"])
        self.assertEqual(rows[0]["publishedAt"], NOW.isoformat())
        self.assertEqual(rows[0]["originalUrl"], ORIGINAL)
        self.assertEqual(meta["snapshotAt"], NOW.isoformat())

    def test_app_unconfirmed_observations_and_unavailable_source_stay_low(self):
        for badge, expected in (("Community observation · unconfirmed", "recorded"), ("Source unavailable", "unknown")):
            with self.subTest(badge=badge):
                rows, _ = self.parse("codex-reset-app", app_page(app_row(badge=badge, text="A community observation without an explicit official completion announcement.")))
                self.assertEqual(rows[0]["status"], expected)
                self.assertEqual(rows[0]["confidence"], "low")
                self.assertIsNone(rows[0]["resetAt"])

    def test_app_completed_cancelled_states_and_no_hidden_script_quotes(self):
        rows, _ = self.parse("codex-reset-app", app_page(app_row(badge="Third-party reports completion", text="Codex Resets classifies this record as a completed reset.") + app_row(badge="Reset cancelled", text="Codex reset cancelled.")))
        self.assertEqual([r["status"] for r in rows], ["completed", "cancelled"])
        self.assertTrue(all(r["resetAt"] is None for r in rows))

    def test_app_layout_change_does_not_silently_erase_cached_rows(self):
        old = {"events": [{"id": "old"}], "lastSuccessAt": NOW.isoformat()}
        with patch("forecast_sources._fetch", return_value='<html><script>Codex will reset</script></html>'):
            result = _source_result(source("codex-reset-app"), old, NOW)
        self.assertTrue(result["error"])
        self.assertEqual(result["events"], old["events"])

    def test_app_key_field_changes_fail_visibly(self):
        for old, new in (("event-date", "new-event-date"), ("<h3>", "<h4>"), ("badge badge-amber", "newbadge"), ("reset-overview-footer", "new-overview-footer")):
            with self.subTest(field=old), self.assertRaises(ValueError):
                self.parse("codex-reset-app", app_page().replace(old, new))

    def test_first_page_only_no_next_url_followed_and_metadata_preserved(self):
        payloads = {
            "codexreset-dev": json.dumps(dev_payload(next="https://codexreset.dev/api/status?page=2")),
            "tibo-watch": json.dumps(tibo_payload(nextCursor="page-two", next="https://tibo.watch/api/signals?page=2")),
            "whennextreset": '<rss><channel><link rel="next">https://whennextreset.com/feed.xml?page=2</link></channel></rss>',
            "codex-reset-app": app_page(),
        }
        for adapter, payload in payloads.items():
            with self.subTest(adapter=adapter), patch("forecast_sources._fetch", return_value=payload) as fetch:
                result = _source_result(source(adapter), {}, NOW)
                fetch.assert_called_once_with(source(adapter))
                self.assertFalse(result["error"])
                self.assertTrue(result["sourceGroupReason"])
                self.assertEqual(result["nextAttemptAt"], (NOW + timedelta(seconds=POLL_SECONDS)).isoformat())

    def test_more_sources_never_bypass_persisted_poll_budget(self):
        for src in SOURCES[4:]:
            previous = {"events": [{"id": "cached"}], "nextAttemptAt": (NOW + timedelta(minutes=1)).isoformat()}
            with self.subTest(source=src["id"]), patch("forecast_sources._fetch") as fetch:
                result = _source_result(src, copy.deepcopy(previous), NOW)
                fetch.assert_not_called()
                self.assertEqual(result["events"], previous["events"])
                self.assertTrue(result["sourceGroupReason"])

    def test_new_json_event_counts_are_bounded(self):
        for adapter, data in (("codexreset-dev", dev_payload([dev_item()] * (MAX_EVENTS_PER_SOURCE + 5))), ("tibo-watch", tibo_payload([tibo_item()] * (MAX_EVENTS_PER_SOURCE + 5)))):
            with self.subTest(adapter=adapter):
                rows, _ = self.parse(adapter, data)
                self.assertEqual(len(rows), MAX_EVENTS_PER_SOURCE)


if __name__ == "__main__":
    unittest.main()

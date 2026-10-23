import copy
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from forecast_sources import (FETCH_TIMEOUT_SECONDS, SOURCES, _SafeRedirect, canonical_url,
                              collect_forecasts, event_timing, parse_rss, parse_source, safe_url)

NOW = datetime(2026, 10, 23, 5, 30, tzinfo=timezone.utc)
ORIGINAL = "https://x.com/thsottiaux/status/1234567890"


def source(adapter):
    return next(s for s in SOURCES if s["adapter"] == adapter)


class ForecastSourceTests(unittest.TestCase):
    def test_safe_public_https_only(self):
        for value in ("http://example.com", "file:///C:/secret", "https://localhost", "https://127.0.0.1/", "https://192.168.0.1", "https://user:pass@example.com", "https://example.com:444/"):
            self.assertEqual(safe_url(value), "")
        self.assertEqual(safe_url("https://example.com/a#b"), "https://example.com/a")

    def test_twitter_mirrors_share_canonical_original(self):
        self.assertEqual(canonical_url("https://twitter.com/Thsottiaux/status/1234567890?s=20"), ORIGINAL)

    def test_explicit_iso_date_and_offset(self):
        timing = event_timing("Codex will reset at 2026-10-24T02:00:00+08:00")
        self.assertEqual(timing["resetAt"], "2026-10-23T18:00:00+00:00")
        self.assertEqual(timing["timeKind"], "exact")

    def test_chinese_timezone_deadline(self):
        timing = event_timing("额度最迟于2026年10月24日 02:00 北京时间完成重置")
        self.assertEqual(timing["resetAt"], "2026-10-23T18:00:00+00:00")
        self.assertEqual(timing["timeKind"], "deadline")

    def test_no_implicit_timezone_or_post_time_substitution(self):
        timing = event_timing("Codex reset scheduled on 2026-10-24 02:00", NOW.isoformat())
        self.assertIsNone(timing["resetAt"])
        self.assertIsNone(event_timing("Codex reset!", NOW.isoformat())["resetAt"])

    def test_card_expiry_is_never_a_reset_time(self):
        timing = event_timing("Your reset card expires at 2026-10-24T02:00:00Z")
        self.assertIsNone(timing["resetAt"])
        self.assertIn("有效期", timing["timeReason"])

    def test_ambiguous_pst_and_eod_never_count_down(self):
        for text in ("Landing tomorrow before 10am PST", "Global reset by EOD", "预计明天发放"):
            self.assertIsNone(event_timing(text, NOW.isoformat())["resetAt"])

    def test_explicit_relative_duration_is_a_deadline(self):
        timing = event_timing("Reset landing within the next 2 hours", NOW.isoformat())
        self.assertEqual(timing["resetAt"], (NOW + timedelta(hours=2)).isoformat())
        self.assertEqual(timing["timeKind"], "deadline")

    def test_radar_does_not_trust_high_confidence_or_announcement_timestamp(self):
        payload = {"events": [{"id": "a", "type": "reset", "summary": "Reset announced", "url": ORIGINAL,
                               "announced_at": (NOW + timedelta(hours=1)).isoformat(), "confidence": "high", "effective_at": NOW.isoformat()}], "tweets": [], "stale": False}
        rows, _ = parse_source(source("radar"), json.dumps(payload))
        self.assertEqual(rows[0]["confidence"], "medium")
        self.assertIsNone(rows[0]["resetAt"])

    def test_radar_reply_uses_parent_context_but_not_parent_time(self):
        payload = {"events": [{"id": "a", "type": "reset", "url": ORIGINAL}], "tweets": [
            {"id": "a", "text": "Landing within the next 2 hours", "at": NOW.isoformat(), "in_reply_to_tweet_id": "b"},
            {"id": "b", "text": "Codex reset incoming", "at": (NOW - timedelta(minutes=1)).isoformat()}]}
        rows, _ = parse_source(source("radar"), json.dumps(payload))
        self.assertEqual(rows[0]["resetAt"], (NOW + timedelta(hours=2)).isoformat())

    def test_codex_resets_scheduled_conversion_does_not_override_pst(self):
        payload = {"data": {"scheduled_reset": {"id": "a", "status": "scheduled", "text": "Reset landing tomorrow before 10am PST", "announced_at": NOW.isoformat(),
                      "scheduled_for": (NOW + timedelta(hours=12)).isoformat(), "source": {"type": "x_post", "url": ORIGINAL}}, "latest_reset": None}, "meta": {"generated_at": NOW.isoformat()}}
        rows, _ = parse_source(source("resets"), json.dumps(payload))
        self.assertIsNone(rows[0]["resetAt"])
        self.assertEqual(rows[0]["confidence"], "low")
        self.assertIsNotNone(rows[0]["estimateAt"])

    def test_observed_account_state_not_activity_time(self):
        payload = {"data": {"scheduled_reset": None, "latest_reset": {"id": "a", "text": "Reset observed 2026-10-24T02:00:00Z", "announced_at": NOW.isoformat(), "source": {"type": "observed"}}}}
        rows, _ = parse_source(source("resets"), json.dumps(payload))
        self.assertIsNone(rows[0]["resetAt"])
        self.assertEqual(rows[0]["status"], "recorded")

    def test_whenreset_ts_is_post_time_not_reset_time(self):
        payload = {"products": {"codex": {"events": [{"id": "a", "type": "reset", "ts": (NOW + timedelta(hours=1)).isoformat(), "summary": "A banked reset was added", "kind": "banked", "url": ORIGINAL}]}}}
        rows, _ = parse_source(source("whenreset"), json.dumps(payload))
        self.assertIsNone(rows[0]["resetAt"])
        self.assertEqual(rows[0]["eventKind"], "grant")

    def test_whenreset_delivered_overrides_future_wording(self):
        payload = {"products": {"codex": {"events": [{"id": "a", "type": "reset", "ts": NOW.isoformat(),
                      "summary": "Codex will reset at 2026-10-23T11:00:00Z", "delivered_at": NOW.isoformat(),
                      "delivered_source_url": ORIGINAL, "url": ORIGINAL}]}}}
        rows, _ = parse_source(source("whenreset"), json.dumps(payload))
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["deliveredAt"], NOW.isoformat())

    def test_skill2web_estimated_iso_and_mirrors_do_not_raise_confidence(self):
        payload = {"codex": {"events": [{"id": "a", "text": "Reset landing tomorrow before 10am PST", "url": ORIGINAL, "status": "upcoming", "at": NOW.isoformat(),
                                            "estimatedResetText": (NOW + timedelta(hours=12)).isoformat(), "corroboratedBy": ["a", "b", "c"]}]}}
        page = '<html><script id="public-feed-data" type="application/json">' + json.dumps(payload) + '</script></html>'
        rows, _ = parse_source(source("skill2web"), page)
        self.assertIsNone(rows[0]["resetAt"])
        self.assertEqual(rows[0]["confidence"], "low")

    def test_rss_atom_support_and_unsafe_xml_rejected(self):
        feed = '<rss><channel><item><title>Codex will reset</title><description>before 2026-10-24T02:00:00Z</description><link>' + ORIGINAL + '</link><pubDate>Fri, 23 Oct 2026 05:00:00 GMT</pubDate></item></channel></rss>'
        rows, _ = parse_rss({"id": "rss", "name": "RSS", "url": "https://example.com"}, feed)
        self.assertEqual(rows[0]["timeKind"], "deadline")
        self.assertEqual(rows[0]["publishedAt"], "2026-10-23T05:00:00+00:00")
        with self.assertRaises(ValueError):
            parse_rss({}, '<!DOCTYPE x><rss/>')

    def test_schema_drift_is_visible(self):
        with self.assertRaises(ValueError):
            parse_source(source("whenreset"), '{}')

    def test_all_sources_are_public_and_timeout_bounded(self):
        self.assertEqual(len(SOURCES), 12)
        self.assertLessEqual(FETCH_TIMEOUT_SECONDS, 12)
        self.assertTrue(all(safe_url(s["requestUrl"]) for s in SOURCES))

    def test_partial_failures_keep_cache_and_successful_site(self):
        previous = {"sources": [{"id": s["id"], "lastSuccessAt": NOW.isoformat(), "events": [{"id": "cached"}]} for s in SOURCES]}
        def fetch(src):
            if src["adapter"] == "whenreset":
                return json.dumps({"products": {"codex": {"events": []}}})
            raise urllib.error.HTTPError(src["requestUrl"], 403, "Forbidden", {}, None)
        with patch("forecast_sources._fetch", side_effect=fetch):
            result = collect_forecasts(previous, NOW)
        failed = next(s for s in result["sources"] if s["id"] == "codex-radar")
        self.assertEqual(failed["events"], [{"id": "cached"}])
        self.assertIn("403", failed["error"])
        self.assertGreaterEqual(datetime.fromisoformat(failed["nextAttemptAt"]), NOW + timedelta(hours=1))
        self.assertFalse(next(s for s in result["sources"] if s["id"] == "whenreset")["error"])

    def test_rate_limit_persisted_prevents_immediate_repeat_request(self):
        previous = {"sources": [{"id": s["id"], "nextAttemptAt": (NOW + timedelta(minutes=10)).isoformat(), "events": []} for s in SOURCES]}
        with patch("forecast_sources._fetch") as fetch:
            result = collect_forecasts(previous, NOW)
        fetch.assert_not_called()
        self.assertEqual(len(result["sources"]), len(SOURCES))

    def test_429_retry_after_is_respected(self):
        from forecast_sources import _source_result
        error = urllib.error.HTTPError("https://example.com", 429, "Too Many Requests", {"Retry-After": "7200"}, None)
        with patch("forecast_sources._fetch", side_effect=error):
            result = _source_result(SOURCES[0], {}, NOW)
        self.assertEqual(result["nextAttemptAt"], (NOW + timedelta(hours=2)).isoformat())

    def test_redirect_cannot_leave_observed_source(self):
        import urllib.request
        handler = _SafeRedirect()
        request = urllib.request.Request("https://example.com/feed")
        with self.assertRaises(ValueError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://localhost/a")
        with self.assertRaises(ValueError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://another.example/a")


if __name__ == "__main__":
    unittest.main()

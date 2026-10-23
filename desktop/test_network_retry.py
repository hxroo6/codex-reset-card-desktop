"""Recovery tests use public-schema fixtures and never touch actual accounts."""
import copy
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from forecast_sources import SOURCES, _source_result, collect_forecasts

NOW = datetime(2026, 10, 23, 16, tzinfo=timezone.utc)
SITE = next(s for s in SOURCES if s["adapter"] == "whenreset")
SUCCESS = json.dumps({"products": {"codex": {"events": []}}})


def previous(**changes):
    value = {"id": SITE["id"], "error": "TLS连接失败；保留上次成功缓存", "failureKind": "tls", "httpStatus": None,
             "errorDetail": "old EOF", "events": [{"id": "old-public-event"}], "failures": 3,
             "lastSuccessAt": (NOW-timedelta(hours=1)).isoformat(), "fetchedAt": (NOW-timedelta(minutes=5)).isoformat(),
             "nextAttemptAt": (NOW+timedelta(hours=1)).isoformat()}
    value.update(changes)
    return value


class NetworkRetryTests(unittest.TestCase):
    def test_automatic_poll_respects_network_backoff(self):
        with patch("forecast_sources._fetch") as fetch:
            result = _source_result(SITE, previous(), NOW)
        fetch.assert_not_called()
        self.assertTrue(result["error"])

    def test_manual_network_retry_clears_old_error_on_confirmed_success(self):
        with patch("forecast_sources._fetch", return_value=SUCCESS) as fetch:
            result = _source_result(SITE, previous(), NOW, manual=True)
        fetch.assert_called_once()
        self.assertEqual(result["error"], "")
        self.assertEqual(result["errorDetail"], "")
        self.assertIsNone(result["failureKind"])
        self.assertEqual(result["failures"], 0)
        self.assertEqual(result["lastManualAttemptAt"], NOW.isoformat())
        self.assertEqual(result["nextAttemptAt"], (NOW+timedelta(minutes=15)).isoformat())

    def test_legacy_connection_errors_can_recover_after_upgrade(self):
        for message in ("公开来源连接失败；保留上次成功缓存", "公开来源请求超时；保留上次成功缓存"):
            old = previous(error=message)
            old.pop("failureKind")
            with self.subTest(message=message), patch("forecast_sources._fetch", return_value=SUCCESS) as fetch:
                result = _source_result(SITE, old, NOW, manual=True)
                fetch.assert_called_once()
                self.assertFalse(result["error"])

    def test_http_limits_are_not_bypassed_even_when_mislabelled_as_connection(self):
        for code in (401, 403, 429, 503):
            with self.subTest(code=code), patch("forecast_sources._fetch") as fetch:
                _source_result(SITE, previous(httpStatus=code), NOW, manual=True)
                fetch.assert_not_called()

    def test_legacy_http_limit_messages_are_not_bypassed(self):
        with patch("forecast_sources._fetch") as fetch:
            _source_result(SITE, previous(error="HTTP 429；已退避", failureKind=None), NOW, manual=True)
        fetch.assert_not_called()

    def test_recent_manual_attempt_and_recent_automatic_attempt_are_both_throttled(self):
        for field in ("fetchedAt", "lastManualAttemptAt"):
            with self.subTest(field=field), patch("forecast_sources._fetch") as fetch:
                _source_result(SITE, previous(**{field: (NOW-timedelta(seconds=30)).isoformat()}), NOW, manual=True)
                fetch.assert_not_called()

    def test_manual_does_not_override_a_successful_site_poll_budget(self):
        with patch("forecast_sources._fetch") as fetch:
            _source_result(SITE, previous(error="", failureKind=None), NOW, manual=True)
        fetch.assert_not_called()

    def test_parse_failure_requires_regular_retry_time(self):
        with patch("forecast_sources._fetch") as fetch:
            _source_result(SITE, previous(failureKind="parse"), NOW, manual=True)
        fetch.assert_not_called()

    def test_retry_failure_keeps_cached_data_and_specific_cause(self):
        old = previous()
        with patch("forecast_sources._fetch", side_effect=urllib.error.URLError(ConnectionResetError("connection reset"))):
            result = _source_result(SITE, old, NOW, manual=True)
        self.assertEqual(result["events"], old["events"])
        self.assertEqual(result["lastSuccessAt"], old["lastSuccessAt"])
        self.assertEqual(result["failureKind"], "connection")
        self.assertIn("connection reset", result["errorDetail"])
        self.assertEqual(result["lastManualAttemptAt"], NOW.isoformat())

    def test_collect_passes_user_intent_to_each_source_without_network(self):
        def result(site, old, now, manual=False):
            return {"id": site["id"], "manual": manual}
        with patch("forecast_sources._source_result", side_effect=result):
            automatic = collect_forecasts({}, NOW)
            manual = collect_forecasts({}, NOW, manual=True)
        self.assertTrue(all(not s["manual"] for s in automatic["sources"]))
        self.assertTrue(all(s["manual"] for s in manual["sources"]))


if __name__ == "__main__":
    unittest.main()

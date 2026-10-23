"""Portable fixtures for the four domains explicitly supplied by the user."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from forecast_logic import ForecastStore
from forecast_sources import SOURCES, _source_result, parse_source

NOW = datetime(2026, 10, 23, 13, tzinfo=timezone.utc)
FUTURE = (NOW + timedelta(hours=4)).isoformat()
LINK = "https://x.com/thsottiaux/status/1234567890"


def src(adapter):
    return next(item for item in SOURCES if item["adapter"] == adapter)


def clock():
    return {"config": {"dataUpdatedAt": (NOW-timedelta(days=30)).isoformat()}, "resetEvents": [{"date": "2026-09-12"}],
            "posts": [{"category": "重置", "status": "确认重置", "at": (NOW-timedelta(days=30)).isoformat(), "url": LINK,
                       "title": "历史重置", "summary": "已完成历史重置"}], "eventSources": {}, "probabilityMethod": ["99%"]}


def cn(text="Landing tomorrow before 10am PST together with a small ship."):
    return {"schemaVersion": 2, "generatedAt": FUTURE, "sync": {"status": "fresh", "lastSuccess": NOW.isoformat()},
            "announcement": {"sourcePostId": "1234567890", "status": "active", "target": {"at": FUTURE, "assumed": False}},
            "posts": [{"id": "1234567890", "text": text, "summaryZh": "重置活动预告", "created_at": NOW.isoformat(),
                       "displayState": "announced", "source": LINK}], "events": [],
            "hero": {"targetAt": FUTURE}, "horizons": [{"probability": 0.99, "windowEnd": FUTURE}]}


def uk(text="Landing tomorrow before 10am PST together with a small ship."):
    return {"version": 1, "sync": {"status": "ok", "lastSuccessAt": NOW.isoformat()}, "events": [
        {"id": "1234567890", "type": "positive", "status": "candidate", "text": text, "sourceUrl": LINK,
         "publishedAt": NOW.isoformat(), "suggestedResetAt": FUTURE, "classification": {"phase": "uncertain"}}]}


def article(title="虚构历史记录", date=FUTURE):
    return ('<article class="announcement tone-0"><h3>'+title+'</h3><time datetime="'+date+'">页面时间</time>'
            '<details><p>Codex limits have been reset.</p></details><a href="'+LINK+'">source</a></article>')


def ai(preview=True):
    signal = ('<div class="signal-panel"><div class="signal-watch-summary">北京时间2026年10月24日01:00前；PST标准时间为02:00</div>'
              '<a href="https://x.com/thsottiaux/status/2345678901">原帖</a><details><p lang="en">Landing tomorrow before 10am PST.</p></details>'
              '<strong>90%+</strong></div>') if preview else ''
    return '<html>'+signal+article()+'<script>{"resetAt":"2099-01-01T00:00:00Z"}</script></html>'


class RequestedSiteTests(unittest.TestCase):
    def parse(self, adapter, data):
        return parse_source(src(adapter), data if isinstance(data, str) else json.dumps(data), now=NOW)

    def test_requested_domains_are_all_distinct_and_configured(self):
        self.assertEqual({s["url"] for s in SOURCES[8:]}, {"https://codexclock.com/", "https://whenreset.com.cn/", "https://whenreset.uk/", "https://ai-resets.com/"})

    def test_clock_uses_content_revision_and_stale_archive_never_counts_down(self):
        events, meta = self.parse("codexclock", clock())
        self.assertEqual(meta["snapshotAt"], (NOW-timedelta(days=30)).isoformat())
        store = ForecastStore({"sources": [dict(src("codexclock"), events=events, lastSuccessAt=NOW.isoformat(), **meta)]})
        self.assertTrue(store.source_rows(NOW)[0]["stale"])
        self.assertIsNone(store.next_event(NOW))
        self.assertTrue(all(e["resetAt"] is None for e in events))

    def test_clock_normal_posts_do_not_become_reset_signals(self):
        data = clock()
        data["posts"][0].update(category="产品", status="普通动态", summary="No reset today")
        self.assertEqual(self.parse("codexclock", data)[0], [])

    def test_clock_incomplete_historical_date_is_not_invented(self):
        data = clock()
        data["posts"] = []
        data["eventSources"] = {"2026-09-12": {"url": LINK, "title": "历史额度重置", "time": "9/12 16:09 GMT+8"}}
        event = self.parse("codexclock", data)[0][0]
        self.assertIsNone(event["publishedAt"])
        self.assertIsNone(event["resetAt"])

    def test_cn_pst_target_is_display_only_despite_exact_target_flag(self):
        events, meta = self.parse("whenreset-cn", cn())
        event = events[0]
        self.assertIsNone(event["resetAt"])
        self.assertEqual(event["timeKind"], "window")
        self.assertEqual(event["confidence"], "low")
        self.assertEqual(event["estimateAt"], FUTURE)
        self.assertEqual(meta["snapshotAt"], NOW.isoformat())

    def test_cn_clear_quote_with_matching_deadline_is_usable(self):
        event = self.parse("whenreset-cn", cn("Codex will reset before "+FUTURE))[0][0]
        self.assertEqual(event["resetAt"], FUTURE)
        self.assertEqual(event["timeKind"], "deadline")
        self.assertEqual(event["confidence"], "medium")

    def test_cn_target_conflict_disables_exact_countdown(self):
        data = cn("Codex will reset before "+FUTURE)
        data["announcement"]["target"]["at"] = (NOW+timedelta(hours=5)).isoformat()
        event = self.parse("whenreset-cn", data)[0][0]
        self.assertIsNone(event["resetAt"])
        self.assertEqual(event["confidence"], "low")

    def test_cn_model_probabilities_do_not_supply_event_times(self):
        data = cn("Codex reset coming soon")
        data["announcement"] = {}
        original = self.parse("whenreset-cn", data)[0]
        data["hero"]["targetAt"] = "2099-01-01T00:00:00Z"
        data["horizons"] = [{"probability": 1, "windowEnd": "2099-01-01T00:00:00Z"}]
        self.assertEqual(self.parse("whenreset-cn", data)[0], original)
        self.assertIsNone(original[0]["resetAt"])

    def test_cn_community_completion_is_only_a_low_evidence_record(self):
        data = cn()
        data["posts"] = []
        data["events"] = [{"id": "community-fixture", "source": LINK, "timestamp": FUTURE, "status": "completed", "verified": True,
                           "confirmationSource": "community", "summary": "会员已确认重置"}]
        event = self.parse("whenreset-cn", data)[0][0]
        self.assertEqual(event["status"], "recorded")
        self.assertEqual(event["confidence"], "low")
        self.assertIsNone(event["publishedAt"])
        self.assertIsNone(event["resetAt"])

    def test_cn_cancelled_and_completed_announcements_stop_future_alerts(self):
        for status in ("cancelled", "completed"):
            data = cn("Codex will reset before "+FUTURE)
            data["announcement"]["status"] = status
            event = self.parse("whenreset-cn", data)[0][0]
            self.assertEqual(event["status"], status)
            self.assertIsNone(event["resetAt"])

    def test_uk_unreviewed_candidate_never_gets_an_exact_countdown(self):
        event = self.parse("whenreset-uk", uk("Codex will reset before "+FUTURE))[0][0]
        self.assertEqual(event["status"], "unknown")
        self.assertIsNone(event["resetAt"])
        self.assertEqual(event["confidence"], "low")

    def test_uk_dismissed_rows_are_omitted(self):
        data = uk()
        data["events"][0]["status"] = "dismissed"
        self.assertEqual(self.parse("whenreset-uk", data)[0], [])

    def test_uk_reviewed_primary_quote_can_provide_explicit_time(self):
        data = uk("Codex will reset before "+FUTURE)
        data["events"][0].update(status="reviewed", classification={"phase": "announced"})
        self.assertEqual(self.parse("whenreset-uk", data)[0][0]["resetAt"], FUTURE)

    def test_negative_quotes_are_not_turned_into_upcoming_resets(self):
        for adapter, data in (("whenreset-cn", cn("Codex will not reset at "+FUTURE)), ("whenreset-uk", uk("Codex will not reset at "+FUTURE))):
            if adapter == "whenreset-uk":
                data["events"][0].update(status="reviewed", classification={})
            event = self.parse(adapter, data)[0][0]
            self.assertEqual(event["status"], "unknown")
            self.assertIsNone(event["resetAt"])

    def test_ai_parses_public_html_only_and_keeps_pst_estimate_unverified(self):
        events, meta = self.parse("ai-resets", ai())
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["status"], "upcoming")
        self.assertEqual(events[0]["estimateAt"], FUTURE)
        self.assertTrue(all(e["resetAt"] is None and e["confidence"] == "low" for e in events))
        self.assertIsNone(meta["snapshotAt"])
        self.assertIn("未提供", meta["freshnessNote"])

    def test_ai_catalogue_time_is_not_publication_or_future_reset_time(self):
        event = self.parse("ai-resets", ai(preview=False))[0][0]
        self.assertIsNone(event["publishedAt"])
        self.assertIsNone(event["resetAt"])
        self.assertEqual(event["status"], "recorded")
        self.assertIn(FUTURE, event["context"])

    def test_ai_probability_and_script_values_do_not_change_events(self):
        original = self.parse("ai-resets", ai())[0]
        self.assertEqual(self.parse("ai-resets", ai().replace("90%+", "1%+").replace("2099-01-01", "2040-01-01"))[0], original)

    def test_missing_required_schema_fails_and_cached_source_survives(self):
        fixtures = (("codexclock", '{}'), ("whenreset-cn", '{}'), ("whenreset-uk", '{}'), ("ai-resets", '<html>loading</html>'))
        for adapter, payload in fixtures:
            old = {"events": [{"id": "saved-public-event"}], "lastSuccessAt": NOW.isoformat()}
            with self.subTest(adapter=adapter), patch("forecast_sources._fetch", return_value=payload):
                result = _source_result(src(adapter), copy.deepcopy(old), NOW)
                self.assertTrue(result["error"])
                self.assertEqual(result["events"], old["events"])

    def test_each_adapter_preserves_poll_budget_and_requires_no_additional_requests(self):
        fixtures = {"codexclock": json.dumps(clock()), "whenreset-cn": json.dumps(cn()), "whenreset-uk": json.dumps(uk()), "ai-resets": ai()}
        for adapter, payload in fixtures.items():
            with self.subTest(adapter=adapter), patch("forecast_sources._fetch", return_value=payload) as fetch:
                result = _source_result(src(adapter), {}, NOW)
                self.assertFalse(result["error"])
                self.assertEqual(result["nextAttemptAt"], (NOW+timedelta(minutes=15)).isoformat())
                fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()

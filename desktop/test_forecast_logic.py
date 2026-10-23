import copy
import json
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from forecast_logic import ForecastStore, demo_forecasts
from forecast_sources import SOURCES

NOW = datetime(2026, 10, 23, 5, 30, tzinfo=timezone.utc)


def fixture(minutes=20, kind="exact"):
    source = demo_forecasts(NOW)["sources"][0]
    source["events"][0].update(resetAt=(NOW + timedelta(minutes=minutes)).isoformat(), timeKind=kind)
    return {"schema": 1, "fetchedAt": NOW.isoformat(), "sources": [source]}


class ForecastLogicTests(unittest.TestCase):
    def test_demo_three_fictional_sources_and_future_countdown(self):
        store = ForecastStore(demo_forecasts(NOW))
        self.assertEqual(len(store.source_rows(NOW)), 3)
        self.assertIn(".invalid", store.next_event(NOW)["url"])
        self.assertTrue(store.next_event(NOW)["canCountdown"])
        self.assertEqual(len(store.rows(NOW)), 3)

    def test_empty_store_shows_configured_sites_but_no_fake_time(self):
        store = ForecastStore()
        self.assertEqual({row["id"] for row in store.source_rows(NOW)}, {source["id"] for source in SOURCES})
        self.assertIsNone(store.next_event(NOW))
        self.assertEqual(store.reminders(NOW), [])

    def test_upgrade_adds_unfetched_site_without_losing_cache_or_reminder_history(self):
        saved = fixture()
        saved["sources"][0]["id"] = "existing-real-site"
        saved["seen"] = {"old-reminder": NOW.isoformat()}
        configured = [{"id": "existing-real-site", "name": "Existing", "url": "https://existing.invalid/"},
                      {"id": "new-public-site", "name": "New", "url": "https://new.invalid/"}]
        store = ForecastStore(saved)
        with patch("forecast_logic.SOURCES", configured):
            rows = {row["id"]: row for row in store.source_rows(NOW)}
        self.assertEqual(set(rows), {"existing-real-site", "new-public-site"})
        self.assertEqual(rows["existing-real-site"]["count"], 1)
        self.assertIsNone(rows["new-public-site"]["lastSuccessAt"])
        self.assertEqual(rows["new-public-site"]["count"], 0)
        self.assertTrue(rows["new-public-site"]["stale"])
        self.assertEqual(store.saved()["seen"], saved["seen"])
        self.assertEqual(store.saved()["sources"], saved["sources"])

    def test_site_dependency_is_disclosed_without_raising_confidence(self):
        data = fixture()
        data["sources"][0].update(sourceGroup="shared-upstream", sourceGroupReason="使用同一公告上游，不是独立证据")
        row = ForecastStore(data).source_rows(NOW)[0]
        self.assertIn("同一公告上游", row["confidenceReason"])
        self.assertEqual(row["confidence"], "medium")

    def test_stale_after_two_hours_recomputed_after_sleep(self):
        store = ForecastStore(fixture(180))
        self.assertIsNotNone(store.next_event(NOW))
        later = NOW + timedelta(hours=2, seconds=1)
        self.assertIsNone(store.next_event(later))
        self.assertTrue(store.rows(later)[0]["stale"])
        self.assertEqual(store.reminders(later), [])

    def test_reported_failure_marks_cached_events_stale_immediately(self):
        store = ForecastStore(fixture())
        sid = store.saved()["sources"][0]["id"]
        store.update({"sources": [{"id": sid, "error": "HTTP 403", "events": []}]}, NOW)
        self.assertEqual(len(store.rows(NOW)), 1)
        self.assertTrue(store.rows(NOW)[0]["stale"])
        self.assertIsNone(store.next_event(NOW))

    def test_saved_does_not_expose_internal_mutable_state(self):
        store = ForecastStore(fixture())
        saved = store.saved()
        saved["sources"][0]["events"].clear()
        self.assertEqual(len(store.rows(NOW)), 1)
        json.dumps(store.saved())

    def test_unknown_window_estimate_recorded_and_expired_do_not_countdown(self):
        for kind in ("unknown", "window", "estimate"):
            store = ForecastStore(fixture(kind=kind))
            self.assertIsNone(store.next_event(NOW))
            self.assertEqual(store.reminders(NOW), [])
        for status in ("recorded", "cancelled", "withdrawn", "unknown"):
            data = fixture()
            data["sources"][0]["events"][0]["status"] = status
            self.assertIsNone(ForecastStore(data).next_event(NOW))
        self.assertIsNone(ForecastStore(fixture(-1)).next_event(NOW))

    def test_same_original_mirror_dedupes_without_increasing_confidence(self):
        data = fixture()
        second = copy.deepcopy(data["sources"][0])
        second.update(id="mirror", name="Mirror", url="https://mirror.example.invalid")
        second["events"][0].update(sourceId="mirror", sourceName="Mirror")
        data["sources"].append(second)
        rows = ForecastStore(data).rows(NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["confidence"], "medium")
        self.assertEqual(len(rows[0]["sourceIds"]), 2)

    def test_conflicting_times_of_same_original_disable_countdown(self):
        data = fixture()
        second = copy.deepcopy(data["sources"][0])
        second.update(id="mirror", name="Mirror")
        second["events"][0]["resetAt"] = (NOW + timedelta(hours=1)).isoformat()
        data["sources"].append(second)
        store = ForecastStore(data)
        self.assertIsNone(store.next_event(NOW))
        self.assertEqual(store.rows(NOW)[0]["confidence"], "low")
        self.assertEqual(len(store.source_rows(NOW)), 2)
        self.assertNotEqual(store.source_rows(NOW)[0]["resetAt"], store.source_rows(NOW)[1]["resetAt"])

    def test_cancellation_from_other_current_source_stops_reminders(self):
        data = fixture(1)
        second = copy.deepcopy(data["sources"][0])
        second.update(id="mirror", name="Mirror")
        second["events"][0]["status"] = "cancelled"
        data["sources"].append(second)
        store = ForecastStore(data)
        self.assertIsNone(store.next_event(NOW))
        self.assertEqual(store.reminders(NOW), [])

    def test_completed_from_other_current_source_stops_forecast(self):
        data = fixture(1)
        second = copy.deepcopy(data["sources"][0])
        second.update(id="completed-mirror", name="Mirror")
        second["events"][0].update(status="completed", resetAt=None)
        data["sources"].append(second)
        store = ForecastStore(data)
        self.assertIsNone(store.next_event(NOW))
        self.assertEqual(store.rows(NOW)[0]["status"], "completed")
        self.assertEqual(store.reminders(NOW), [])

    def test_low_confidence_explicit_time_is_displayed_without_countdown(self):
        data = fixture(1)
        data["sources"][0]["events"][0]["confidence"] = "low"
        store = ForecastStore(data)
        self.assertIsNotNone(store.rows(NOW)[0]["resetAt"])
        self.assertIsNone(store.next_event(NOW))
        self.assertEqual(store.reminders(NOW), [])

    def test_stale_duplicate_does_not_override_fresh_time(self):
        data = fixture()
        second = copy.deepcopy(data["sources"][0])
        second.update(id="old", lastSuccessAt=(NOW - timedelta(hours=3)).isoformat())
        second["events"][0].update(resetAt=(NOW + timedelta(hours=1)).isoformat(), status="cancelled")
        data["sources"].append(second)
        self.assertIsNotNone(ForecastStore(data).next_event(NOW))

    def test_milestones_one_note_per_evaluation_and_restart_dedupes(self):
        store = ForecastStore(fixture(9, "deadline"))
        notes = store.reminders(NOW)
        self.assertEqual(len(notes), 1)
        self.assertIn("10 分钟内", notes[0][1])
        self.assertIn("最迟截止", notes[0][1])
        self.assertIn("不代表个人账户", notes[0][1])
        restarted = ForecastStore(json.loads(json.dumps(store.saved())))
        self.assertEqual(restarted.reminders(NOW + timedelta(seconds=10)), [])
        later = NOW + timedelta(minutes=7, seconds=1)
        self.assertIn("2 分钟内", restarted.reminders(later)[0][1])

    def test_due_notification_and_no_late_catchup(self):
        store = ForecastStore(fixture(1))
        store.reminders(NOW)
        self.assertEqual(len(store.reminders(NOW + timedelta(minutes=1))), 1)
        self.assertEqual(store.reminders(NOW + timedelta(minutes=1, seconds=1)), [])
        late = ForecastStore(fixture(-10))
        self.assertEqual(late.reminders(NOW), [])

    def test_source_heartbeat_stale_and_save_rate_limit_field(self):
        data = fixture()
        data["sources"][0]["snapshotAt"] = (NOW - timedelta(hours=3)).isoformat()
        store = ForecastStore(data)
        self.assertTrue(store.source_rows(NOW)[0]["stale"])
        self.assertIsNone(store.next_event(NOW))
        self.assertIn("nextAttemptAt", store.source_rows(NOW)[0])

    def test_site_selects_latest_unknown_announcement_and_its_confidence(self):
        data = fixture()
        old = data["sources"][0]["events"][0]
        old.update(resetAt=None, timeKind="unknown", publishedAt=(NOW - timedelta(days=1)).isoformat())
        newer = dict(old, id="newer", confidence="low", publishedAt=NOW.isoformat(), timeReason="PST歧义")
        data["sources"][0]["events"].append(newer)
        row = ForecastStore(data).source_rows(NOW)[0]
        self.assertEqual(row["confidence"], "low")
        self.assertEqual(row["timeReason"], "PST歧义")

    def test_site_prefers_nearest_clear_future_over_newer_unknown(self):
        data = fixture(5)
        old = data["sources"][0]["events"][0]
        newer = dict(old, id="newer", resetAt=None, timeKind="window", publishedAt=NOW.isoformat(), confidence="low")
        data["sources"][0]["events"].append(newer)
        row = ForecastStore(data).source_rows(NOW)[0]
        self.assertEqual(row["resetAt"], (NOW + timedelta(minutes=5)).isoformat())
        self.assertEqual(row["confidence"], "medium")

    def test_upstream_high_claim_is_capped_at_medium(self):
        data = fixture()
        data["sources"][0]["events"][0]["confidence"] = "high"
        self.assertEqual(ForecastStore(data).rows(NOW)[0]["confidence"], "medium")


if __name__ == "__main__":
    unittest.main()

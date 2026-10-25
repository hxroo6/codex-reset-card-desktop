"""Manual forecast selection uses fictional records, never network or cards."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone

from forecast_logic import ForecastStore, TRUST_INACTIVE

NOW = datetime(2026, 10, 23, 16, tzinfo=timezone.utc)


def sample(minutes=20, **changes):
    event = {"id": "event-a", "title": "虚构预告 A", "originalUrl": "https://example.invalid/event-a",
             "url": "https://example.invalid/event-a", "resetAt": (NOW+timedelta(minutes=minutes)).isoformat(),
             "estimateAt": None, "publishedAt": (NOW-timedelta(minutes=5)).isoformat(), "timeKind": "exact",
             "status": "upcoming", "eventKind": "forecast", "confidence": "low", "confidenceReason": "虚构低证据",
             "timeReason": "虚构时间"}
    event.update(changes)
    return event


def data(*events, **source_changes):
    source = {"id": "demo-source", "name": "虚构来源", "url": "https://example.invalid/",
              "lastSuccessAt": NOW.isoformat(), "error": "", "events": list(events or [sample()])}
    source.update(source_changes)
    return {"schema": 1, "sources": [source]}


def row(store, event_id="event-a", now=NOW):
    return next(item for item in store.rows(now) if item["id"] == event_id)


class ForecastTrustTests(unittest.TestCase):
    def select(self, store, event_id="event-a", now=NOW):
        return store.trust(row(store, event_id, now)["groupId"], now)

    def test_rows_offer_low_evidence_concrete_time_without_enabling_automatic_countdown(self):
        store = ForecastStore(data())
        candidate = row(store)
        self.assertTrue(candidate["canTrust"])
        self.assertEqual(candidate["manualCandidateAt"], sample()["resetAt"])
        self.assertFalse(candidate["manualTrusted"])
        self.assertFalse(candidate["canCountdown"])
        self.assertIn("不提高可信度", candidate["trustReason"])
        self.assertIsNone(store.next_event(NOW))

    def test_window_unknown_and_estimate_can_use_specific_estimate_without_confidence_upgrade(self):
        estimate = (NOW+timedelta(minutes=12)).isoformat()
        for kind in ("window", "unknown", "estimate"):
            with self.subTest(kind=kind):
                store = ForecastStore(data(sample(resetAt=None, estimateAt=estimate, timeKind=kind, status="unknown")))
                self.assertEqual(row(store)["manualCandidateAt"], estimate)
                trusted = self.select(store)
                self.assertTrue(trusted["canCountdown"])
                self.assertEqual(trusted["resetAt"], estimate)
                self.assertEqual(trusted["confidence"], "low")
                self.assertEqual(trusted["timeKind"], kind)

    def test_reset_time_takes_precedence_over_estimate(self):
        store = ForecastStore(data(sample(estimateAt=(NOW+timedelta(minutes=3)).isoformat())))
        self.assertEqual(self.select(store)["resetAt"], sample()["resetAt"])

    def test_untimed_mirror_cannot_hide_another_sites_future_estimate(self):
        estimate = (NOW+timedelta(minutes=45)).isoformat()
        untimed = data(sample(resetAt=None, timeKind="window", timeReason="时区歧义"))
        timed = data(sample(resetAt=None, estimateAt=estimate, timeKind="window", timeReason="时区歧义"),
                     id="demo-estimate-site", name="有估计时间的站点")["sources"][0]
        for sources in ([untimed["sources"][0], timed], [timed, untimed["sources"][0]]):
            with self.subTest(order=[source["id"] for source in sources]):
                store = ForecastStore({"sources": sources})
                merged = row(store)
                self.assertTrue(merged["canTrust"])
                self.assertEqual(merged["manualCandidateAt"], estimate)
                self.assertEqual(merged["manualCandidateSourceId"], "demo-estimate-site")
                self.assertEqual(merged["estimateAt"], estimate)
                self.assertFalse(merged["canCountdown"])
                self.assertIsNone(store.next_event(NOW))
                chosen = self.select(store)
                self.assertEqual(chosen["resetAt"], estimate)
                self.assertEqual(chosen["confidence"], "low")
                restored = ForecastStore(json.loads(json.dumps(store.saved())))
                self.assertEqual(restored.trusted_event(NOW)["manualCandidateSourceId"], "demo-estimate-site")

    def test_fresh_untimed_mirror_does_not_revive_a_stale_estimate(self):
        original = data(sample(resetAt=None, timeKind="window"))
        stale = data(sample(resetAt=None, estimateAt=(NOW+timedelta(hours=1)).isoformat()),
                     id="old-estimate", lastSuccessAt=(NOW-timedelta(hours=3)).isoformat())["sources"][0]
        original["sources"].append(stale)
        self.assertFalse(row(ForecastStore(original))["canTrust"])

    def test_future_estimate_cannot_override_a_current_completed_report(self):
        original = data(sample(resetAt=None, status="completed"))
        timed = data(sample(resetAt=None, estimateAt=(NOW+timedelta(hours=1)).isoformat()),
                     id="estimate-mirror")["sources"][0]
        original["sources"].append(timed)
        store = ForecastStore(original)
        self.assertFalse(row(store)["canTrust"])
        with self.assertRaises(ValueError):
            self.select(store)

    def test_source_displays_a_future_estimate_before_a_newer_untimed_post(self):
        estimate = (NOW+timedelta(minutes=45)).isoformat()
        timed = sample(resetAt=None, estimateAt=estimate, timeKind="window", status="unknown")
        newer = sample(id="newer-untimed", originalUrl="https://example.invalid/newer", resetAt=None,
                       publishedAt=NOW.isoformat(), timeKind="unknown")
        source = ForecastStore(data(timed, newer)).source_rows(NOW)[0]
        self.assertEqual(source["estimateAt"], estimate)

    def test_future_estimate_is_usable_when_reported_reset_time_has_passed(self):
        estimate = (NOW+timedelta(minutes=3)).isoformat()
        store = ForecastStore(data(sample(-2, estimateAt=estimate)))
        self.assertEqual(self.select(store)["resetAt"], estimate)

    def test_no_specific_time_and_expired_time_are_not_selectable(self):
        for event in (sample(resetAt=None), sample(-1), sample(resetAt="tomorrow"), sample(resetAt="2026-10-24T12:00:00")):
            with self.subTest(reset=event["resetAt"]):
                store = ForecastStore(data(event))
                self.assertFalse(row(store)["canTrust"])
                self.assertIsNone(row(store)["manualCandidateAt"])
                with self.assertRaises(ValueError):
                    self.select(store)
                self.assertNotIn("trustedForecast", store.saved())

    def test_terminal_or_historical_records_cannot_be_selected(self):
        for status in TRUST_INACTIVE:
            with self.subTest(status=status):
                store = ForecastStore(data(sample(status=status)))
                self.assertFalse(row(store)["canTrust"])
                with self.assertRaises(ValueError):
                    self.select(store)

    def test_missing_group_cannot_replace_current_selection(self):
        store = ForecastStore(data())
        selected = self.select(store)
        with self.assertRaises(ValueError):
            store.trust("not-in-list", NOW)
        self.assertEqual(store.trusted_event(NOW)["groupId"], selected["groupId"])

    def test_stale_cache_may_be_selected_but_is_explicitly_labelled(self):
        store = ForecastStore(data(error="网络暂时不可用"))
        self.assertTrue(row(store)["canTrust"])
        self.assertIn("缓存陈旧", row(store)["trustReason"])
        selected = self.select(store)
        self.assertTrue(selected["stale"])
        self.assertIn("缓存陈旧", selected["trustReason"])
        self.assertTrue(selected["canCountdown"])
        self.assertIn("缓存陈旧", store.reminders(NOW)[0][1])

    def test_selection_is_json_serializable_and_restored_after_restart(self):
        store = ForecastStore(data())
        selected = self.select(store)
        saved = store.saved()
        self.assertEqual(saved["trustedForecast"]["targetAt"], sample()["resetAt"])
        self.assertEqual(saved["trustedForecast"]["selectedAt"], NOW.isoformat())
        restarted = ForecastStore(json.loads(json.dumps(saved)))
        resumed = restarted.next_event(NOW+timedelta(seconds=10))
        self.assertTrue(resumed["manualTrusted"])
        self.assertEqual(resumed["groupId"], selected["groupId"])
        self.assertEqual(resumed["remainingSeconds"], 20*60-10)
        self.assertTrue(row(restarted)["manualTrusted"])

    def test_changing_selection_and_clearing_restores_automatic_choice(self):
        first = sample(minutes=10, confidence="medium")
        second = sample(minutes=20, id="event-b", originalUrl="https://example.invalid/event-b", confidence="medium")
        store = ForecastStore(data(first, second))
        self.assertEqual(store.next_event(NOW)["id"], "event-a")
        self.select(store, "event-b")
        self.assertEqual(store.next_event(NOW)["id"], "event-b")
        self.select(store, "event-a")
        self.assertFalse(row(store, "event-b")["manualTrusted"])
        self.assertTrue(row(store)["manualTrusted"])
        store.clear_trust()
        self.assertIsNone(store.trusted_event(NOW))
        self.assertNotIn("trustedForecast", store.saved())
        self.assertEqual(store.next_event(NOW)["id"], "event-a")
        self.assertFalse(row(store)["manualTrusted"])

    def test_refresh_never_silently_changes_locked_time(self):
        store = ForecastStore(data())
        selected = self.select(store)
        changed = sample(minutes=50)
        store.update(data(changed), NOW+timedelta(minutes=1))
        self.assertEqual(row(store)["manualCandidateAt"], changed["resetAt"])
        self.assertEqual(store.trusted_event(NOW)["resetAt"], selected["resetAt"])
        self.assertEqual(store.next_event(NOW)["resetAt"], selected["resetAt"])
        # Only another explicit selection adopts the refreshed time.
        self.assertEqual(self.select(store)["resetAt"], changed["resetAt"])

    def test_network_failure_keeps_locked_time_and_minimal_snapshot(self):
        store = ForecastStore(data())
        selected = self.select(store)
        store.update({"sources": [{"id": "demo-source", "error": "TLS 中断", "events": []}]}, NOW)
        current = store.trusted_event(NOW)
        self.assertTrue(current["stale"])
        self.assertFalse(current["trustPaused"])
        self.assertEqual(current["resetAt"], selected["resetAt"])
        self.assertTrue(current["canCountdown"])

    def test_removed_event_keeps_selection_as_stale_instead_of_switching(self):
        store = ForecastStore(data())
        selected = self.select(store)
        store.update({"sources": [{"id": "demo-source", "events": []}]}, NOW)
        self.assertEqual(store.rows(NOW), [])
        current = store.next_event(NOW)
        self.assertEqual(current["groupId"], selected["groupId"])
        self.assertTrue(current["stale"])
        self.assertTrue(current["canCountdown"])

    def test_new_terminal_report_pauses_and_persists_even_after_network_failure(self):
        for status in TRUST_INACTIVE:
            with self.subTest(status=status):
                store = ForecastStore(data())
                self.select(store)
                store.update(data(sample(status=status, resetAt=None)), NOW+timedelta(seconds=1))
                trusted = store.next_event(NOW)
                self.assertTrue(trusted["trustPaused"])
                self.assertFalse(trusted["canCountdown"])
                self.assertEqual(store.reminders(NOW), [])
                self.assertIn("pausedAt", store.saved()["trustedForecast"])
                store = ForecastStore(json.loads(json.dumps(store.saved())))
                store.update({"sources": [{"id": "demo-source", "error": "断网", "events": []}]}, NOW)
                self.assertTrue(store.next_event(NOW)["trustPaused"])
                self.assertEqual(store.reminders(NOW), [])

    def test_old_cancelled_mirror_does_not_pause_fresh_selected_forecast(self):
        original = data()
        old_source = copy.deepcopy(original["sources"][0])
        old_source.update(id="old-mirror", lastSuccessAt=(NOW-timedelta(hours=3)).isoformat())
        old_source["events"][0]["status"] = "cancelled"
        original["sources"].append(old_source)
        store = ForecastStore(original)
        self.assertTrue(row(store)["canTrust"])
        self.assertFalse(self.select(store)["trustPaused"])

    def test_fresh_terminal_mirror_prevents_manual_selection(self):
        original = data()
        mirror = copy.deepcopy(original["sources"][0])
        mirror["id"] = "fresh-mirror"
        mirror["events"][0]["status"] = "recorded"
        original["sources"].append(mirror)
        store = ForecastStore(original)
        self.assertFalse(row(store)["canTrust"])

    def test_due_choice_remains_visible_and_does_not_switch_to_another_future_event(self):
        second = sample(minutes=40, id="event-b", originalUrl="https://example.invalid/event-b", confidence="medium")
        store = ForecastStore(data(sample(minutes=1), second))
        self.select(store)
        store.reminders(NOW)
        due = NOW+timedelta(minutes=1)
        current = store.next_event(due)
        self.assertEqual(current["id"], "event-a")
        self.assertTrue(current["trustEnded"])
        self.assertFalse(current["canCountdown"])
        notes = store.reminders(due)
        self.assertEqual(len([note for note in notes if "手动采信" in note[0]]), 1)
        self.assertIn("已到采信时间", next(note[1] for note in notes if "手动采信" in note[0]))
        self.assertEqual(store.reminders(due+timedelta(seconds=1)), [])
        restarted = ForecastStore(json.loads(json.dumps(store.saved())))
        self.assertEqual(restarted.next_event(due)["id"], "event-a")
        self.assertEqual(restarted.reminders(due), [])

    def test_manual_milestones_are_separately_deduplicated_across_restart(self):
        store = ForecastStore(data(sample(minutes=70)))
        self.select(store)
        self.assertEqual(store.reminders(NOW), [])
        for elapsed, label in ((10, "1 小时内"), (60, "10 分钟内"), (68, "2 分钟内"), (70, "已到采信时间")):
            now = NOW+timedelta(minutes=elapsed)
            notes = store.reminders(now)
            self.assertEqual(len(notes), 1)
            self.assertIn("手动采信", notes[0][0])
            self.assertIn(label, notes[0][1])
            self.assertIn("原可信度低", notes[0][1])
            store = ForecastStore(json.loads(json.dumps(store.saved())))
            self.assertEqual(store.reminders(now+timedelta(seconds=1)), [])
        self.assertTrue(all(key.startswith("manual-forecast:") for key in store.saved()["seen"]))

    def test_automatic_reminder_history_does_not_suppress_manual_choice(self):
        store = ForecastStore(data(sample(minutes=9, confidence="medium")))
        self.assertEqual(len(store.reminders(NOW)), 1)
        self.select(store)
        notes = store.reminders(NOW)
        self.assertEqual(len(notes), 1)
        self.assertIn("手动采信", notes[0][0])

    def test_selected_group_does_not_send_both_manual_and_automatic_notifications(self):
        store = ForecastStore(data(sample(minutes=9, confidence="medium")))
        self.select(store)
        notes = store.reminders(NOW)
        self.assertEqual(len(notes), 1)
        self.assertIn("手动采信", notes[0][0])

    def test_other_groups_keep_automatic_reminder_behavior(self):
        second = sample(minutes=8, id="event-b", originalUrl="https://example.invalid/event-b", confidence="medium")
        store = ForecastStore(data(sample(minutes=9), second))
        self.select(store)
        notes = store.reminders(NOW)
        self.assertEqual(len(notes), 2)
        self.assertEqual(len([note for note in notes if "手动采信" in note[0]]), 1)
        self.assertEqual(len([note for note in notes if note[0] == "公开重置活动预告"]), 1)

    def test_restart_hours_after_due_does_not_replay_manual_milestones(self):
        store = ForecastStore(data(sample(minutes=1)))
        self.select(store)
        late = NOW+timedelta(hours=3)
        self.assertEqual(store.reminders(late), [])
        self.assertTrue(store.next_event(late)["trustEnded"])

    def test_returned_event_is_a_copy_and_invalid_saved_choice_falls_back_safely(self):
        store = ForecastStore(data())
        selected = self.select(store)
        selected["title"] = "changed"
        self.assertEqual(store.trusted_event(NOW)["title"], "虚构预告 A")
        for saved_choice in (None, [], {}, {"groupId": "bad", "targetAt": "unknown", "event": {}}):
            saved = data()
            saved["trustedForecast"] = saved_choice
            self.assertIsNone(ForecastStore(saved).trusted_event(NOW))


if __name__ == "__main__":
    unittest.main(verbosity=2)

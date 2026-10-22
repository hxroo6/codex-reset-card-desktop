"""Boundary tests for the read-only countdown; no Tk windows or real CLI calls."""
import copy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from floating_countdown import automatic_use_view
from panel_logic import PanelModel


NOW = datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc)


class AutomaticUseViewTests(unittest.TestCase):
    def setUp(self):
        # Any accidental connection to the original CLI fails this pure test run.
        self.cli_guard = patch("panel_logic.subprocess.run", side_effect=AssertionError("Real CLI execution is forbidden"))
        self.cli_guard.start()
        self.addCleanup(self.cli_guard.stop)

    def sample(self, trigger_seconds=600, outcome=""):
        trigger = NOW + timedelta(seconds=trigger_seconds)
        expiry = trigger + timedelta(minutes=5)
        card = {"fingerprint": "TEST-001", "expiresAt": expiry.isoformat(),
                "triggerAt": trigger.isoformat(), "lastOutcome": outcome}
        status = {"armed": True, "leadTimeSeconds": 300, "cacheReady": True,
                  "service": {"installed": True, "running": True},
                  "lastDiscoveryAt": NOW.isoformat(), "availableCount": 1,
                  "detailsAvailable": True, "cards": [card],
                  "live": {"availableCount": 1, "detailsAvailable": True,
                           "fetchedAt": NOW.isoformat(),
                           "credits": [{"fingerprint": "TEST-001", "status": "available",
                                        "expiresAt": expiry.isoformat()}]}}
        model = PanelModel()
        model.accept(status, NOW)
        return model

    def assert_no_execution_timer(self, view):
        self.assertNotIn("triggerAt", view)
        self.assertNotEqual(view["status"], "距计划自动使用")
        self.assertNotRegex(view["value"], r"\d{2}:\d{2}:\d{2}")

    def test_counts_down_to_trigger_not_expiry(self):
        view = automatic_use_view(self.sample(trigger_seconds=600), NOW)
        self.assertEqual(view["value"], "00:10:00")
        self.assertEqual(view["triggerAt"], (NOW + timedelta(minutes=10)).isoformat())
        self.assertIn("2026-10-23 10:10:00", view["detail"])
        self.assertEqual(view["status"], "距计划自动使用")
        self.assertEqual(view["tone"], "normal")

    def test_countdown_ticks_with_clock(self):
        model = self.sample(trigger_seconds=600)
        view = automatic_use_view(model, NOW + timedelta(seconds=1))
        self.assertEqual(view["value"], "00:09:59")

    def test_live_only_card_is_not_a_scheduled_execution(self):
        model = self.sample()
        model.status["cards"] = []
        view = automatic_use_view(model, NOW)
        self.assert_no_execution_timer(view)
        self.assertEqual(view["value"], "等待后台发现")

    def test_changed_live_expiry_never_reuses_old_trigger(self):
        model = self.sample()
        model.live["credits"][0]["expiresAt"] = (NOW + timedelta(hours=3)).isoformat()
        view = automatic_use_view(model, NOW)
        self.assert_no_execution_timer(view)
        self.assertEqual(view["value"], "等待后台发现")

    def test_mismatched_recorded_trigger_is_not_confirmed(self):
        model = self.sample()
        model.status["cards"][0]["triggerAt"] = (NOW + timedelta(minutes=11)).isoformat()
        self.assert_no_execution_timer(automatic_use_view(model, NOW))

    def test_exact_trigger_waits_for_backend_result(self):
        view = automatic_use_view(self.sample(trigger_seconds=0), NOW)
        self.assertEqual(view["value"], "等待后台结果")
        self.assertIn("已到使用时间", view["status"])
        self.assertNotIn("已到期", view["value"])

    def test_past_trigger_before_expiry_does_not_count_negative(self):
        view = automatic_use_view(self.sample(trigger_seconds=-1), NOW)
        self.assertEqual(view["value"], "等待后台结果")
        self.assertNotRegex(view["value"], r"-\d")

    def test_expired_card_cannot_keep_an_execution_timer(self):
        self.assert_no_execution_timer(automatic_use_view(self.sample(trigger_seconds=-300), NOW))

    def test_disarmed_and_stopped_services_never_show_timer(self):
        for field in ("disarmed", "stopped", "missing", "unknown_auth", "unknown_running"):
            with self.subTest(state=field):
                model = self.sample()
                if field == "disarmed":
                    model.status["armed"] = False
                elif field == "stopped":
                    model.status["service"]["running"] = False
                elif field == "missing":
                    model.status["service"]["installed"] = False
                elif field == "unknown_auth":
                    model.status.pop("armed")
                else:
                    model.status["service"].pop("running")
                self.assert_no_execution_timer(automatic_use_view(model, NOW))

    def test_disk_snapshot_is_explicitly_unconfirmed(self):
        model = PanelModel(self.sample().saved())
        view = automatic_use_view(model, NOW)
        self.assertIn("状态待确认", view["status"])
        self.assertEqual(view["tone"], "warning")
        self.assertNotEqual(view["status"], "距计划自动使用")

    def test_local_status_90_second_boundary(self):
        model = self.sample()
        model.local_at = NOW - timedelta(seconds=90)
        self.assertEqual(automatic_use_view(model, NOW)["status"], "距计划自动使用")
        model.local_at -= timedelta(microseconds=1)
        view = automatic_use_view(model, NOW)
        self.assertIn("状态待确认", view["status"])
        self.assertEqual(view["tone"], "warning")

    def test_missing_local_timestamp_is_unconfirmed(self):
        model = self.sample()
        model.local_at = None
        self.assertIn("状态待确认", automatic_use_view(model, NOW)["status"])

    def test_stale_or_failed_snapshots_mark_prior_plan(self):
        for reason in ("local_error", "live_error", "old_source"):
            with self.subTest(reason=reason):
                model = self.sample()
                if reason == "old_source":
                    old = (NOW - timedelta(days=2)).isoformat()
                    model.status["lastDiscoveryAt"] = old
                    model.live["fetchedAt"] = old
                else:
                    setattr(model, reason, "isolated test failure")
                view = automatic_use_view(model, NOW)
                self.assertIn("上次计划", view["status"])
                self.assertIn("状态待确认", view["status"])
                self.assertEqual(view["tone"], "warning")

    def test_nothing_to_reset_does_not_invent_retry_time(self):
        model = self.sample(trigger_seconds=-30, outcome="nothingToReset")
        view = automatic_use_view(model, NOW)
        self.assertEqual(view["value"], "等待后台复查")
        self.assertIn("重试时间未知", view["status"])
        self.assertEqual(view["triggerAt"], (NOW - timedelta(seconds=30)).isoformat())
        self.assertNotRegex(view["value"], r"\d{2}:\d{2}:\d{2}")

    def test_stale_nothing_to_reset_keeps_unconfirmed_status(self):
        model = self.sample(trigger_seconds=-30, outcome="nothingToReset")
        model.local_at = NOW - timedelta(seconds=91)
        view = automatic_use_view(model, NOW)
        self.assertEqual(view["value"], "等待后台复查")
        self.assertIn("状态待确认", view["status"])
        self.assertIn("重试时间未知", view["status"])
        self.assertEqual(view["tone"], "warning")

    def test_terminal_card_outcomes_are_not_counted_down(self):
        for outcome in ("reset", "alreadyRedeemed", "noCredit"):
            with self.subTest(outcome=outcome):
                self.assert_no_execution_timer(automatic_use_view(self.sample(outcome=outcome), NOW))

    def test_next_eligible_card_follows_a_terminal_card(self):
        model = self.sample(outcome="reset")
        next_card = copy.deepcopy(model.status["cards"][0])
        next_card.update(fingerprint="TEST-002", lastOutcome="",
                         expiresAt=(NOW + timedelta(minutes=25)).isoformat(),
                         triggerAt=(NOW + timedelta(minutes=20)).isoformat())
        model.status["cards"].append(next_card)
        model.live["credits"].append({"fingerprint": "TEST-002", "status": "available",
                                      "expiresAt": next_card["expiresAt"]})
        view = automatic_use_view(model, NOW)
        self.assertEqual(view["fingerprint"], "TEST-002")
        self.assertEqual(view["value"], "00:20:00")

    def test_redeemed_live_card_is_not_counted_down(self):
        model = self.sample()
        model.live["credits"][0]["status"] = "redeemed"
        self.assert_no_execution_timer(automatic_use_view(model, NOW))

    def test_known_empty_and_missing_details_do_not_imply_a_timer(self):
        model = self.sample()
        model.status["cards"] = []
        model.live.update(availableCount=0, credits=[])
        view = automatic_use_view(model, NOW)
        self.assert_no_execution_timer(view)
        self.assertEqual(view["value"], "暂无待用卡")
        model.live.update(availableCount=1, detailsAvailable=False)
        view = automatic_use_view(model, NOW)
        self.assert_no_execution_timer(view)
        self.assertEqual(view["value"], "等待计划详情")

    def test_unknown_initial_state_has_no_timer(self):
        self.assert_no_execution_timer(automatic_use_view(PanelModel(), NOW))


if __name__ == "__main__":
    unittest.main(verbosity=2)

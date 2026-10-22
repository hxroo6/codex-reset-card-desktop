"""Pure tests for per-card postponement. Never starts the original service or CLI."""
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import schedule_control as control
from schedule_control import postpone_document


NOW = datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc)
FINGERPRINT = "1234567890" + "a" * 54
PREFIX = FINGERPRINT[:10]
OTHER_FINGERPRINT = "abcdef1234" + "b" * 54


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class PostponeDocumentTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"version": 1, "armed": True, "leadTimeSeconds": 300,
                    "authPath": "C:/private/sentinel-secret/auth.json"}
        self.state = {
            "version": 1, "lastDiscoveryAt": (NOW - timedelta(hours=1)).isoformat(),
            "nextDiscoveryAt": (NOW + timedelta(hours=23)).isoformat(),
            "discoveryFailures": 0, "availableCount": 2, "detailsAvailable": True,
            "cards": [
                {"fingerprint": FINGERPRINT, "expiresAt": (NOW + timedelta(minutes=15)).isoformat()},
                {"fingerprint": OTHER_FINGERPRINT, "expiresAt": (NOW + timedelta(days=1)).isoformat(),
                 "nextAttemptAt": (NOW + timedelta(hours=23, minutes=58)).isoformat()},
            ],
            "lastAction": {"at": (NOW - timedelta(hours=1)).isoformat(), "kind": "discovery",
                           "message": "sentinel-secret"},
            "extensions": {"unchanged": ["a", {"value": 7}]},
        }
        self.expected = NOW + timedelta(minutes=10)
        self.cli_guard = patch("subprocess.run", side_effect=AssertionError("Real CLI execution is forbidden"))
        self.cli_guard.start()
        self.addCleanup(self.cli_guard.stop)

    def postpone(self, state=None, cfg=None, prefix=PREFIX, expected=None, now=NOW):
        return postpone_document(self.state if state is None else state,
                                 self.cfg if cfg is None else cfg,
                                 prefix, self.expected.isoformat() if expected is None else expected, now)

    def test_moves_only_target_two_minutes_from_effective_trigger(self):
        changed, result = self.postpone()
        target = self.expected + timedelta(minutes=2)
        self.assertEqual(instant(changed["cards"][0]["nextAttemptAt"]), target)
        self.assertEqual(instant(result["triggerAt"]), target)
        self.assertEqual(instant(result["expiresAt"]), instant(self.state["cards"][0]["expiresAt"]))
        self.assertEqual(result["delayedSeconds"], 120)
        self.assertTrue(result["fingerprint"])
        self.assertTrue(FINGERPRINT.startswith(result["fingerprint"]))

    def test_does_not_mutate_input_state_or_global_config(self):
        before_state, before_cfg = copy.deepcopy(self.state), copy.deepcopy(self.cfg)
        changed, _ = self.postpone()
        self.assertEqual(self.state, before_state)
        self.assertEqual(self.cfg, before_cfg)
        self.assertEqual(changed["cards"][1], before_state["cards"][1])
        reconstructed = copy.deepcopy(changed)
        reconstructed["cards"][0].pop("nextAttemptAt")
        self.assertEqual(reconstructed, before_state)
        changed["extensions"]["unchanged"][1]["value"] = 99
        self.assertEqual(self.state, before_state, "Returned state must not alias nested input fields")

    def test_result_is_limited_to_public_schedule_metadata(self):
        _, result = self.postpone()
        self.assertEqual(set(result), {"fingerprint", "triggerAt", "expiresAt", "delayedSeconds"})
        serialized = json.dumps(result)
        for private in ("sentinel-secret", "authPath", "attemptKey", "lastAction", "access_token", "refresh_token"):
            self.assertNotIn(private, serialized)

    def test_existing_later_attempt_is_the_start_of_two_minute_delay(self):
        later = self.expected + timedelta(seconds=30)
        self.state["cards"][0]["nextAttemptAt"] = later.isoformat()
        changed, result = self.postpone(expected=later.isoformat())
        self.assertEqual(instant(changed["cards"][0]["nextAttemptAt"]), later + timedelta(minutes=2))
        self.assertEqual(instant(result["triggerAt"]), later + timedelta(minutes=2))

    def test_earlier_next_attempt_does_not_override_base_trigger(self):
        self.state["cards"][0]["nextAttemptAt"] = (self.expected - timedelta(seconds=10)).isoformat()
        changed, _ = self.postpone()
        self.assertEqual(instant(changed["cards"][0]["nextAttemptAt"]), self.expected + timedelta(minutes=2))

    def test_repeated_postponement_uses_current_effective_time(self):
        self.cfg["leadTimeSeconds"] = 420
        self.expected = NOW + timedelta(minutes=8)
        first, first_result = self.postpone()
        second, result = self.postpone(state=first, expected=first_result["triggerAt"])
        self.assertEqual(instant(second["cards"][0]["nextAttemptAt"]), self.expected + timedelta(minutes=4))
        self.assertEqual(instant(result["triggerAt"]), self.expected + timedelta(minutes=4))

    def test_exactly_two_minutes_before_expiry_is_allowed(self):
        current_effective = self.expected + timedelta(minutes=1)
        self.state["cards"][0]["nextAttemptAt"] = current_effective.isoformat()
        changed, _ = self.postpone(expected=current_effective.isoformat())
        expiry = instant(self.state["cards"][0]["expiresAt"])
        self.assertEqual(instant(changed["cards"][0]["nextAttemptAt"]), expiry - timedelta(seconds=120))

    def test_one_microsecond_inside_expiry_reserve_is_rejected(self):
        current_effective = self.expected + timedelta(minutes=1, microseconds=1)
        self.state["cards"][0]["nextAttemptAt"] = current_effective.isoformat()
        with self.assertRaises(ValueError):
            self.postpone(expected=current_effective.isoformat())

    def test_third_two_minute_delay_would_violate_expiry_reserve(self):
        self.state["cards"][0]["nextAttemptAt"] = (self.expected + timedelta(minutes=4)).isoformat()
        with self.assertRaises(ValueError):
            self.postpone(expected=self.state["cards"][0]["nextAttemptAt"])

    def test_stale_expected_trigger_is_rejected(self):
        self.state["cards"][0]["nextAttemptAt"] = (self.expected + timedelta(minutes=1)).isoformat()
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            self.postpone()
        self.assertEqual(self.state, before)

    def test_expected_trigger_must_match_even_by_one_microsecond(self):
        with self.assertRaises(ValueError):
            self.postpone(expected=(self.expected + timedelta(microseconds=1)).isoformat())

    def test_equivalent_timezone_offset_matches_same_instant(self):
        expected = self.expected.astimezone(timezone(timedelta(hours=8))).isoformat()
        changed, _ = self.postpone(expected=expected)
        self.assertEqual(instant(changed["cards"][0]["nextAttemptAt"]), self.expected + timedelta(minutes=2))

    def test_at_most_30_seconds_before_trigger_is_rejected(self):
        for remaining in (30, 29, 0, -1):
            with self.subTest(seconds=remaining):
                with self.assertRaises(ValueError):
                    self.postpone(now=self.expected - timedelta(seconds=remaining))

    def test_more_than_30_seconds_before_trigger_is_allowed(self):
        _, result = self.postpone(now=self.expected - timedelta(seconds=30, microseconds=1))
        self.assertEqual(instant(result["triggerAt"]), self.expected + timedelta(minutes=2))

    def test_unarmed_or_unknown_authorization_is_rejected(self):
        for value in (False, None, 0, "true"):
            with self.subTest(armed=value):
                cfg = dict(self.cfg, armed=value)
                with self.assertRaises(ValueError):
                    self.postpone(cfg=cfg)

    def test_invalid_state_and_config_versions_are_rejected(self):
        for invalid in (0, 2, -1, "1", True, None):
            for which in ("state", "config"):
                with self.subTest(version=invalid, document=which):
                    state, cfg = copy.deepcopy(self.state), copy.deepcopy(self.cfg)
                    (state if which == "state" else cfg)["version"] = invalid
                    with self.assertRaises(ValueError):
                        self.postpone(state=state, cfg=cfg)

    def test_missing_versions_are_rejected(self):
        for which in ("state", "config"):
            with self.subTest(document=which):
                state, cfg = copy.deepcopy(self.state), copy.deepcopy(self.cfg)
                (state if which == "state" else cfg).pop("version")
                with self.assertRaises(ValueError):
                    self.postpone(state=state, cfg=cfg)

    def test_prefix_must_be_exactly_ten_hex_characters(self):
        for invalid in ("", "123456789", "12345678901", "123456789g", "../state!!", None, 1234567890):
            with self.subTest(prefix=invalid):
                with self.assertRaises(ValueError):
                    self.postpone(prefix=invalid)

    def test_unknown_fingerprint_is_rejected(self):
        with self.assertRaises(ValueError):
            self.postpone(prefix="eeeeeeeeee")

    def test_ambiguous_short_fingerprint_is_rejected(self):
        duplicate = copy.deepcopy(self.state["cards"][0])
        duplicate["fingerprint"] = PREFIX + "c" * 54
        self.state["cards"].append(duplicate)
        with self.assertRaises(ValueError):
            self.postpone()

    def test_in_flight_idempotency_key_is_rejected(self):
        self.state["cards"][0]["attemptKey"] = "in-flight-sensitive-key"
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError) as caught:
            self.postpone()
        self.assertEqual(self.state, before)
        self.assertNotIn("in-flight-sensitive-key", str(caught.exception))

    def test_any_prior_outcome_blocks_postponement(self):
        for outcome in ("nothingToReset", "requestFailed", "verificationFailed", "reset", "alreadyRedeemed", "noCredit", "dryRun"):
            with self.subTest(outcome=outcome):
                state = copy.deepcopy(self.state)
                state["cards"][0]["lastOutcome"] = outcome
                with self.assertRaises(ValueError):
                    self.postpone(state=state)

    def test_empty_attempt_key_and_outcome_do_not_block(self):
        self.state["cards"][0].update(attemptKey="", lastOutcome="")
        _, result = self.postpone()
        self.assertEqual(result["delayedSeconds"], 120)

    def test_invalid_or_naive_timestamps_are_rejected(self):
        for field in ("expiresAt", "nextAttemptAt", "expected"):
            for invalid in ("not-a-time", "2026-10-23T02:10:00", "", None):
                if field == "nextAttemptAt" and invalid is None:
                    continue  # An optional null pointer is valid in the Go state schema.
                with self.subTest(field=field, value=invalid):
                    state = copy.deepcopy(self.state)
                    if field == "expected":
                        # Pass None directly: the convenience wrapper uses it for its default.
                        with self.assertRaises(ValueError):
                            postpone_document(state, self.cfg, PREFIX, invalid, NOW)
                    else:
                        state["cards"][0][field] = invalid
                        with self.assertRaises(ValueError):
                            self.postpone(state=state)

    def test_null_optional_next_attempt_uses_base_trigger(self):
        self.state["cards"][0]["nextAttemptAt"] = None
        changed, _ = self.postpone()
        self.assertEqual(instant(changed["cards"][0]["nextAttemptAt"]), self.expected + timedelta(minutes=2))

    def test_naive_now_is_rejected(self):
        with self.assertRaises(ValueError):
            self.postpone(now=NOW.replace(tzinfo=None))

    def test_expired_target_is_rejected_even_when_other_cards_are_safe(self):
        expiry = NOW - timedelta(seconds=1)
        self.state["cards"][0]["expiresAt"] = expiry.isoformat()
        expected = expiry - timedelta(seconds=self.cfg["leadTimeSeconds"])
        with self.assertRaises(ValueError):
            self.postpone(expected=expected.isoformat())

    def test_other_live_card_in_execution_window_blocks_service_interruption(self):
        self.state["cards"][1]["expiresAt"] = (NOW + timedelta(seconds=320)).isoformat()
        self.state["cards"][1].pop("nextAttemptAt")
        with self.assertRaises(ValueError):
            self.postpone()

    def test_other_live_card_with_in_flight_or_retry_state_blocks_stop(self):
        for field, value in (("attemptKey", "other-in-flight"), ("lastOutcome", "requestFailed")):
            with self.subTest(field=field):
                state = copy.deepcopy(self.state)
                state["cards"][1][field] = value
                with self.assertRaises(ValueError):
                    self.postpone(state=state)


class PostponeTransactionTests(unittest.TestCase):
    """Exercise stop/write/restart ordering with all OS/service boundaries replaced."""

    def setUp(self):
        self.expected = NOW + timedelta(minutes=10)
        self.state_path, self.cfg_path = Path("fixture-state.json"), Path("fixture-config.json")
        self.state = {"version": 1, "cards": [
            {"fingerprint": FINGERPRINT, "expiresAt": (NOW + timedelta(minutes=15)).isoformat()}
        ]}
        self.cfg = {"version": 1, "armed": True, "leadTimeSeconds": 300}
        self.events, self.cli_calls, self.writes = [], [], []
        self.daemon_locked = False
        self.stop_requested = False
        self.service_installed = True
        self.service_running = True
        self.stop_error = None
        self.start_error = None
        self.write_error = None
        self.state_after_stop = None
        self.read_count = 0

        @contextmanager
        def operation_guard(name, timeout=8):
            self.assertEqual(name, "Local\\CodexResetCardPanel-Postpone")
            self.events.append("operation-lock")
            try:
                yield
            finally:
                self.events.append("operation-unlock")

        @contextmanager
        def daemon_guard():
            self.events.append("daemon-lock")
            self.daemon_locked = True
            try:
                yield
            finally:
                self.daemon_locked = False
                self.events.append("daemon-unlock")

        def cli(*arguments):
            self.cli_calls.append(arguments)
            self.events.append(arguments)
            if arguments == ("status", "--json"):
                return {"service": {"installed": self.service_installed, "running": self.service_running}}
            if arguments == ("service", "stop"):
                self.stop_requested = True
                if self.stop_error:
                    raise self.stop_error
                return None
            if arguments == ("service", "start"):
                self.assertFalse(self.daemon_locked, "Helper must release daemon mutex before restart")
                if self.start_error:
                    raise self.start_error
                return None
            self.fail("Unexpected command: " + repr(arguments))

        def read(path):
            if path == self.cfg_path:
                return copy.deepcopy(self.cfg)
            self.assertEqual(path, self.state_path)
            self.read_count += 1
            if self.stop_requested and self.state_after_stop is not None:
                return copy.deepcopy(self.state_after_stop)
            return copy.deepcopy(self.state)

        def write(path, document):
            self.assertEqual(path, self.state_path)
            self.assertTrue(self.daemon_locked, "State write must be enclosed by daemon mutex")
            self.assertTrue(self.stop_requested, "State write requires stopping the daemon first")
            self.events.append("state-write")
            if self.write_error:
                raise self.write_error
            self.writes.append(copy.deepcopy(document))

        def daemon_probe():
            self.assertFalse(self.daemon_locked, "Restart probe must run after releasing helper daemon mutex")
            self.events.append("daemon-probe")
            return True

        for name, value in (("_require_compatible", lambda: None), ("named_guard", operation_guard), ("daemon_guard", daemon_guard),
                            ("_cli", cli), ("_read", read), ("atomic_write_state", write),
                            ("_paths", lambda: (self.state_path, self.cfg_path, Path("fixture.lock"))),
                            ("utcnow", lambda: NOW), ("_daemon_running", daemon_probe)):
            self.replace(name, value)
        forbidden = patch("subprocess.run", side_effect=AssertionError("Real CLI execution is forbidden"))
        forbidden.start()
        self.addCleanup(forbidden.stop)
        forbidden_win32 = patch.object(control.ctypes, "WinDLL", side_effect=AssertionError("Real Win32 access is forbidden"), create=True)
        forbidden_win32.start()
        self.addCleanup(forbidden_win32.stop)

    def replace(self, name, value):
        replacement = patch.object(control, name, value)
        replacement.start()
        self.addCleanup(replacement.stop)

    def postpone(self):
        return control.postpone_card(PREFIX, self.expected.isoformat())

    def tearDown(self):
        allowed = {("status", "--json"), ("service", "stop"), ("service", "start")}
        self.assertTrue(set(self.cli_calls) <= allowed, "No consume/config/authorization commands are allowed")
        self.assertFalse(self.daemon_locked, "Daemon lock must always be released")

    def test_success_locks_state_and_releases_mutex_before_restart(self):
        result = self.postpone()
        self.assertEqual(instant(result["triggerAt"]), self.expected + timedelta(minutes=2))
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.cli_calls, [("status", "--json"), ("service", "stop"), ("service", "start")])
        self.assertLess(self.events.index(("service", "stop")), self.events.index("daemon-lock"))
        self.assertLess(self.events.index("daemon-lock"), self.events.index("state-write"))
        self.assertLess(self.events.index("state-write"), self.events.index("daemon-unlock"))
        self.assertLess(self.events.index("daemon-unlock"), self.events.index(("service", "start")))
        self.assertEqual(self.events[-1], "operation-unlock")
        self.assertEqual(self.read_count, 2, "The state must be reread after acquiring daemon lock")

    def test_preflight_rejection_does_not_stop_write_or_restart(self):
        self.cfg["armed"] = False
        with self.assertRaises(ValueError):
            self.postpone()
        self.assertEqual(self.cli_calls, [("status", "--json")])
        self.assertEqual(self.writes, [])
        self.assertNotIn("daemon-lock", self.events)

    def test_preexisting_stopped_or_missing_service_is_not_started(self):
        for installed, running in ((True, False), (False, False)):
            with self.subTest(installed=installed, running=running):
                self.cli_calls.clear()
                self.service_installed, self.service_running = installed, running
                with self.assertRaises(RuntimeError):
                    self.postpone()
                self.assertEqual(self.cli_calls, [("status", "--json")])
                self.assertEqual(self.writes, [])

    def test_stop_failure_still_attempts_service_recovery(self):
        self.stop_error = RuntimeError("stop failed")
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            self.postpone()
        self.assertIn(("service", "start"), self.cli_calls)
        self.assertEqual(self.writes, [])
        self.assertNotIn("daemon-lock", self.events)

    def test_daemon_lock_failure_still_restores_service(self):
        @contextmanager
        def blocked():
            raise RuntimeError("daemon mutex still exists")
            yield
        self.replace("daemon_guard", blocked)
        with self.assertRaisesRegex(RuntimeError, "daemon mutex still exists"):
            self.postpone()
        self.assertIn(("service", "start"), self.cli_calls)
        self.assertEqual(self.writes, [])

    def test_plan_changed_while_stopping_is_revalidated_without_writing(self):
        self.state_after_stop = copy.deepcopy(self.state)
        self.state_after_stop["cards"][0]["nextAttemptAt"] = (self.expected + timedelta(minutes=1)).isoformat()
        with self.assertRaisesRegex(RuntimeError, "后台计划已改变"):
            self.postpone()
        self.assertEqual(self.writes, [])
        self.assertIn(("service", "start"), self.cli_calls)
        self.assertIn("daemon-unlock", self.events)

    def test_save_failure_releases_mutex_and_restores_service(self):
        self.write_error = OSError("atomic replacement failed")
        before = copy.deepcopy(self.state)
        with self.assertRaisesRegex(RuntimeError, "atomic replacement failed"):
            self.postpone()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.state, before)
        self.assertLess(self.events.index("daemon-unlock"), self.events.index(("service", "start")))

    def test_restart_failure_after_commit_reports_saved_plan_without_rollback(self):
        self.start_error = RuntimeError("start failed")
        with self.assertRaises(RuntimeError) as caught:
            self.postpone()
        self.assertIn("已保存", str(caught.exception))
        self.assertIn("后台未恢复", str(caught.exception))
        self.assertEqual(len(self.writes), 1, "A committed delay must not be rolled back to an earlier trigger")
        self.assertEqual(instant(self.writes[0]["cards"][0]["nextAttemptAt"]), self.expected + timedelta(minutes=2))

    def test_restart_failure_without_commit_does_not_report_saved(self):
        self.stop_error, self.start_error = RuntimeError("stop failed"), RuntimeError("start failed")
        with self.assertRaises(RuntimeError) as caught:
            self.postpone()
        self.assertIn("未完成", str(caught.exception))
        self.assertIn("后台未恢复", str(caught.exception))
        self.assertNotIn("已保存", str(caught.exception))
        self.assertEqual(self.writes, [])

    def test_successful_task_start_is_not_accepted_without_running_confirmation(self):
        self.replace("_daemon_running", lambda: False)
        with patch.object(control.time, "monotonic", side_effect=[0, 1, 9]), patch.object(control.time, "sleep"):
            with self.assertRaises(RuntimeError) as caught:
                self.postpone()
        self.assertIn("已保存", str(caught.exception))
        self.assertIn("后台未恢复", str(caught.exception))
        self.assertEqual(len(self.writes), 1)


class RestartConfirmationTests(unittest.TestCase):
    def test_waits_for_existing_daemon_mutex_without_cli_status_probe(self):
        with patch.object(control, "_cli") as cli, \
                patch.object(control, "_daemon_running", side_effect=[False, False, True]) as probe, \
                patch.object(control.time, "monotonic", side_effect=[0, 1, 2, 3]), \
                patch.object(control.time, "sleep") as sleep, \
                patch.object(control.ctypes, "WinDLL", side_effect=AssertionError("Real Win32 access is forbidden"), create=True):
            control._restart_and_confirm()
        cli.assert_called_once_with("service", "start")
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_existing_mutex_never_appears_times_out_without_real_wait(self):
        with patch.object(control, "_cli") as cli, \
                patch.object(control, "_daemon_running", return_value=False) as probe, \
                patch.object(control.time, "monotonic", side_effect=[0, 1, 9]), \
                patch.object(control.time, "sleep") as sleep, \
                patch.object(control.ctypes, "WinDLL", side_effect=AssertionError("Real Win32 access is forbidden"), create=True):
            with self.assertRaisesRegex(RuntimeError, "后台任务未恢复运行"):
                control._restart_and_confirm()
        cli.assert_called_once_with("service", "start")
        probe.assert_called_once_with()
        sleep.assert_called_once_with(0.15)


class CliBoundaryTests(unittest.TestCase):
    def test_cli_rejects_consumption_and_unapproved_commands_before_subprocess(self):
        with patch("subprocess.run") as process:
            for command in (("consume",), ("run",), ("arm",), ("config", "--lead-time", "60"),
                            ("status", "--refresh", "--json")):
                with self.subTest(command=command):
                    with self.assertRaises(ValueError):
                        control._cli(*command)
            process.assert_not_called()


class AtomicStateWriteTests(unittest.TestCase):
    def test_atomic_write_replaces_only_requested_file_and_removes_temporary(self):
        with tempfile.TemporaryDirectory(prefix="reset-card-state-test-") as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"version":1,"marker":"original"}', encoding="utf-8")
            replacement = {"version": 1, "marker": "延期计划", "cards": []}
            control.atomic_write_state(path, replacement)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), replacement)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_replace_failure_keeps_original_state_and_cleans_temp(self):
        with tempfile.TemporaryDirectory(prefix="reset-card-state-test-") as directory:
            path = Path(directory) / "state.json"
            original = b'{"version":1,"marker":"original"}\n'
            path.write_bytes(original)
            with patch.object(control.os, "replace", side_effect=OSError("simulated replace failure")):
                with self.assertRaises(OSError):
                    control.atomic_write_state(path, {"version": 1, "cards": []})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_serialization_failure_keeps_original_state_and_cleans_temp(self):
        with tempfile.TemporaryDirectory(prefix="reset-card-state-test-") as directory:
            path = Path(directory) / "state.json"
            original = b'{"version":1,"marker":"original"}\n'
            path.write_bytes(original)
            with patch.object(control.json, "dump", side_effect=OSError("simulated write failure")):
                with self.assertRaises(OSError):
                    control.atomic_write_state(path, {"version": 1, "cards": []})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])


class ScheduleTimingReadTests(unittest.TestCase):
    def setUp(self):
        self.expiry = (NOW + timedelta(minutes=15)).isoformat()
        self.next_attempt = (NOW + timedelta(minutes=12)).isoformat()
        self.status = {"cards": [{"fingerprint": PREFIX, "expiresAt": self.expiry,
                                  "triggerAt": (NOW + timedelta(minutes=10)).isoformat()}]}
        self.raw = {"version": 1, "cards": [{"fingerprint": FINGERPRINT, "expiresAt": self.expiry,
                                             "nextAttemptAt": self.next_attempt,
                                             "attemptKey": "sensitive-idempotency-key"}]}

    def enrich(self):
        with patch.object(control, "_paths", return_value=(Path("fixture-state.json"), None, None)), \
                patch.object(control, "_read", return_value=self.raw), \
                patch("subprocess.run", side_effect=AssertionError("Real CLI execution is forbidden")):
            return control.add_schedule_timing(self.status)

    def test_matched_timing_exposes_schedule_and_boolean_without_private_key(self):
        before = copy.deepcopy(self.status)
        enriched = self.enrich()
        self.assertIs(enriched["timingReady"], True)
        self.assertEqual(enriched["cards"][0]["nextAttemptAt"], self.next_attempt)
        self.assertIs(enriched["cards"][0]["attemptInProgress"], True)
        self.assertNotIn("attemptKey", json.dumps(enriched))
        self.assertNotIn("sensitive-idempotency-key", json.dumps(enriched))
        self.assertEqual(self.status, before)

    def test_ambiguous_matching_state_does_not_confirm_timing(self):
        duplicate = copy.deepcopy(self.raw["cards"][0])
        duplicate["fingerprint"] = PREFIX + "c" * 54
        self.raw["cards"].append(duplicate)
        enriched = self.enrich()
        self.assertIs(enriched["timingReady"], False)
        self.assertNotIn("nextAttemptAt", enriched["cards"][0])

    def test_expiry_changed_in_state_does_not_attach_old_card_timing(self):
        self.raw["cards"][0]["expiresAt"] = (NOW + timedelta(hours=1)).isoformat()
        enriched = self.enrich()
        self.assertIs(enriched["timingReady"], False)
        self.assertNotIn("nextAttemptAt", enriched["cards"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)

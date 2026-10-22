import copy
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from panel_logic import PanelModel, ReminderEngine, demo_status
from schedule_control import postpone_card
from upstream_compatibility import _inspect, inspect_upstream

NOW = datetime(2026, 10, 23, 1, tzinfo=timezone.utc)


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        _inspect.cache_clear()

    def test_supported_version_and_fingerprint_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "fixture.exe"
            binary.write_bytes(b"fixture")
            result = subprocess.CompletedProcess([], 0, "0.2.0\n", "")
            with patch("upstream_compatibility.subprocess.run", return_value=result) as run:
                self.assertTrue(inspect_upstream(binary)["supported"])
                self.assertTrue(inspect_upstream(binary)["supported"])
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0][1:], ["version"])

    def test_changed_binary_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "fixture.exe"
            binary.write_bytes(b"old")
            with patch("upstream_compatibility.subprocess.run", side_effect=[subprocess.CompletedProcess([], 0, "0.2.0", ""), subprocess.CompletedProcess([], 0, "0.3.0", "")]) as run:
                self.assertTrue(inspect_upstream(binary)["supported"])
                binary.write_bytes(b"different-size")
                self.assertFalse(inspect_upstream(binary)["supported"])
                self.assertEqual(run.call_count, 2)

    def test_unknown_version_blocks_before_any_service_operation(self):
        with patch("schedule_control.inspect_upstream", return_value={"supported": False, "message": "unsupported"}), patch("schedule_control._cli") as cli, patch("schedule_control.named_guard") as guard:
            with self.assertRaisesRegex(RuntimeError, "unsupported"):
                postpone_card("1234567890", NOW.isoformat())
            cli.assert_not_called()
            guard.assert_not_called()

    def test_missing_binary_is_not_compatible(self):
        with patch("upstream_compatibility.subprocess.run") as run:
            self.assertFalse(inspect_upstream(Path("not-a-real-binary.exe"))["supported"])
            run.assert_not_called()


class ResultNotificationTests(unittest.TestCase):
    def notes(self, outcome, previous=None):
        status = demo_status(NOW)
        status["lastAction"] = {"at": NOW.isoformat(), "kind": "consume", "fingerprint": "TEST-RESULT", "outcome": outcome}
        model = PanelModel()
        model.accept(status, NOW)
        engine = ReminderEngine(copy.deepcopy(previous))
        return model, engine, engine.evaluate(model, NOW)

    def test_success_is_explicit_timestamped_and_deduplicated(self):
        model, engine, notes = self.notes("reset")
        title, text = next(item for item in notes if item[0] == "重置卡使用成功")
        self.assertIn("TEST-RESULT", text)
        self.assertIn("2026-10-23 09:00:00", text)
        self.assertFalse(any(title == "重置卡使用成功" for title, _ in engine.evaluate(model, NOW)))

    def test_unavailable_card_is_failure(self):
        _, _, notes = self.notes("noCredit")
        self.assertTrue(any(title == "重置卡使用失败" for title, _ in notes))
        self.assertFalse(any(title == "重置卡使用成功" for title, _ in notes))

    def test_already_redeemed_is_never_reported_as_new_success(self):
        _, _, notes = self.notes("alreadyRedeemed")
        self.assertTrue(any(title == "重置卡此前已兑换" for title, _ in notes))
        self.assertFalse(any(title == "重置卡使用成功" for title, _ in notes))

    def test_uncertain_result_is_not_reported_as_definite_failure(self):
        _, _, notes = self.notes("")
        self.assertTrue(any(title == "使用结果待确认" for title, _ in notes))
        self.assertFalse(any(title in ("重置卡使用成功", "重置卡使用失败") for title, _ in notes))

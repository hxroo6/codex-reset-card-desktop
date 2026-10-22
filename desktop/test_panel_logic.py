import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from panel_logic import (PanelModel, ReminderEngine, child_proxy_env, clean_status, clock_text,
                         demo_status, load_json, parse_time, read_status,
                         remaining_text, save_json)


NOW = datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc)


class PanelLogicTests(unittest.TestCase):
    def sample(self):
        model = PanelModel()
        model.accept(demo_status(NOW), NOW)
        return model

    def test_unknown_is_not_zero_and_null_lists_safe(self):
        model = PanelModel()
        model.accept({"cards": None, "service": None, "live": {"credits": None}}, NOW)
        _, data, _ = model.source()
        self.assertNotIn("availableCount", data)
        self.assertEqual(model.cards(NOW), [])
        self.assertTrue(model.stale(NOW))

    def test_whitelist_excludes_unexpected_secret_fields(self):
        data = demo_status(NOW)
        data["access_token"] = "secret"
        data["live"]["access_token"] = "secret"
        data["cards"][0]["rawID"] = "secret"
        self.assertNotIn("secret", json.dumps(clean_status(data)))

    def test_beijing_time_and_invalid_time(self):
        self.assertEqual(clock_text("2026-10-22T18:00:00Z"), "2026-10-23 02:00:00")
        self.assertIsNone(parse_time("not-a-date"))
        self.assertIsNone(parse_time("2026-10-23T02:00:00"))
        self.assertEqual(remaining_text(90061), "1天 01:01:01")

    def test_live_survives_cache_refresh_and_online_failure(self):
        model = self.sample()
        live = copy.deepcopy(model.live)
        cache = demo_status(NOW)
        cache.pop("live")
        model.accept(cache, NOW + timedelta(seconds=30))
        model.live_error = "HTTP 429"
        self.assertEqual(model.live, live)
        self.assertTrue(model.stale(NOW + timedelta(seconds=30)))
        self.assertEqual(model.source()[0], "在线只读快照")

    def test_newer_daemon_cache_supersedes_old_live(self):
        model = self.sample()
        cache = demo_status(NOW + timedelta(minutes=3))
        cache.pop("live")
        cache["lastDiscoveryAt"] = (NOW + timedelta(minutes=2)).isoformat()
        model.accept(cache, NOW + timedelta(minutes=3))
        self.assertEqual(model.source()[0], "原服务缓存")

    def test_expired_and_redeemed_live_cards_are_filtered(self):
        model = self.sample()
        model.live["credits"][0]["status"] = "redeemed"
        model.live["credits"][1]["expiresAt"] = (NOW - timedelta(seconds=1)).isoformat()
        self.assertEqual(len(model.cards(NOW)), 1)

    def test_live_expiry_change_never_reuses_old_trigger(self):
        model = self.sample()
        new_expiry = NOW + timedelta(hours=2)
        model.live["credits"][0]["expiresAt"] = new_expiry.isoformat()
        card = next(c for c in model.cards(NOW) if c["fingerprint"] == "DEMO-A7831")
        self.assertEqual(card["trigger"], new_expiry - timedelta(minutes=5))
        self.assertFalse(card["recorded"])
        self.assertFalse(card["scheduled"])

    def test_unarmed_or_stopped_never_claims_scheduled(self):
        model = self.sample()
        model.status["armed"] = False
        self.assertTrue(model.cards(NOW)[0]["recorded"])
        self.assertFalse(model.cards(NOW)[0]["scheduled"])
        model.status["armed"] = True
        model.status["service"]["running"] = False
        self.assertFalse(model.cards(NOW)[0]["scheduled"])

    def test_thresholds_merge_and_dedupe_survives_restart(self):
        model = self.sample()
        engine = ReminderEngine()
        first = engine.evaluate(model, NOW)
        # A card inside 10m gives one alert, not 24h + 1h + 10m spam.
        self.assertEqual(sum("DEMO-A7831" in text for _, text in first), 1)
        self.assertIn("10 分钟", first[0][1])
        restarted = ReminderEngine(json.loads(json.dumps(engine.state)))
        self.assertEqual(restarted.evaluate(model, NOW + timedelta(seconds=1)), [])

    def test_stale_data_does_not_emit_expiry_alert(self):
        model = self.sample()
        model.live_error = "timeout"
        notes = ReminderEngine().evaluate(model, NOW)
        self.assertFalse(any(title == "卡片到期提醒" for title, _ in notes))
        self.assertTrue(any(title == "读取状态失败" for title, _ in notes))

    def test_service_error_recovers_then_alerts_new_transition(self):
        model = self.sample()
        model.status["service"]["running"] = False
        engine = ReminderEngine()
        self.assertTrue(any(title == "原服务异常" for title, _ in engine.evaluate(model, NOW)))
        self.assertFalse(any(title == "原服务异常" for title, _ in engine.evaluate(model, NOW)))
        model.status["service"]["running"] = True
        engine.evaluate(model, NOW)
        model.status["service"]["running"] = False
        self.assertTrue(any(title == "原服务异常" for title, _ in engine.evaluate(model, NOW)))

    def test_consume_result_dedupe_and_no_false_reset_success(self):
        model = self.sample()
        model.status["lastAction"] = {"at": NOW.isoformat(), "kind": "consume", "outcome": "nothingToReset"}
        engine = ReminderEngine()
        notes = engine.evaluate(model, NOW)
        result = next(text for title, text in notes if title == "当前无需重置")
        self.assertIn("无需重置", result)
        self.assertNotIn("成功", result)
        self.assertFalse(any(title == "当前无需重置" for title, _ in engine.evaluate(model, NOW)))

    def test_persist_and_disk_loaded_is_stale_until_checked(self):
        model = self.sample()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            save_json(path, model.saved())
            reloaded = PanelModel(load_json(path, {}))
            self.assertTrue(reloaded.stale(NOW))
            reloaded.accept(demo_status(NOW), NOW)
            self.assertFalse(reloaded.stale(NOW))

    def test_cli_only_issues_status_and_refresh_read_commands(self):
        result = subprocess.CompletedProcess([], 0, json.dumps(demo_status(NOW)), "")
        with patch("panel_logic.default_binary", return_value=Path(__file__)), patch("panel_logic.subprocess.run", return_value=result) as run, patch("upstream_compatibility.inspect_upstream", return_value={"version": "0.2.0", "supported": True}):
            read_status(False)
            self.assertEqual(run.call_args.args[0][1:], ["status", "--json"])
            read_status(True)
            self.assertEqual(run.call_args.args[0][1:], ["status", "--refresh", "--json"])
            self.assertIn("env", run.call_args.kwargs)
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_system_proxy_maps_only_child_and_explicit_environment_wins(self):
        fake_key = unittest.mock.MagicMock()
        fake_key.__enter__.return_value = fake_key
        fake_reg = types.SimpleNamespace(
            HKEY_CURRENT_USER=1, OpenKey=lambda *args: fake_key,
            QueryValueEx=lambda key, name: (1 if name == "ProxyEnable" else "http=127.0.0.1:7900;https=127.0.0.1:7901", 1))
        with patch.dict("sys.modules", {"winreg": fake_reg}), patch.dict("os.environ", {}, clear=True):
            child = child_proxy_env()
            self.assertEqual(child["HTTP_PROXY"], "http://127.0.0.1:7900")
            self.assertEqual(child["HTTPS_PROXY"], "http://127.0.0.1:7901")
            import os
            self.assertNotIn("HTTP_PROXY", os.environ)
        with patch.dict("sys.modules", {"winreg": fake_reg}), patch.dict("os.environ", {"HTTPS_PROXY": "http://explicit:9999"}, clear=True):
            self.assertEqual(child_proxy_env()["HTTPS_PROXY"], "http://explicit:9999")


if __name__ == "__main__":
    unittest.main(verbosity=2)

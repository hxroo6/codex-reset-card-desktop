"""Network-free checks for public-source retry and recovery feedback."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forecast_logic import ForecastStore
from reset_card_panel import Panel, forecast_health_change

NOW = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)


def source(key, failed=False):
    return {"id": key, "name": "演示来源 " + key, "error": "TLS 连接中断" if failed else "",
            "lastSuccessAt": NOW.isoformat(), "events": []}


class ForecastHealthFeedbackTests(unittest.TestCase):
    def test_all_failed_is_reported_once_and_cache_failure_set_is_reusable(self):
        sources = [source("a", True), source("b", True)]
        failed, note = forecast_health_change(sources)
        self.assertEqual(failed, {"a", "b"})
        self.assertIn("全部来源", note[1])
        self.assertIn("保留上次成功缓存", note[1])
        self.assertIsNone(forecast_health_change(sources, list(failed))[1])

    def test_partial_recovery_and_new_failure_identity_are_reported(self):
        failed, note = forecast_health_change([source("a"), source("b", True)], {"a", "b"})
        self.assertEqual(failed, {"b"})
        self.assertEqual(note[0], "公开预告采集恢复")
        self.assertIn("已恢复 1 个", note[1])
        failed, note = forecast_health_change([source("a", True), source("b")], failed)
        self.assertEqual(failed, {"a"})
        self.assertIsNotNone(note)

    def test_healthy_unchanged_collection_does_not_create_history(self):
        self.assertEqual(forecast_health_change([source("a"), source("b")]), (set(), None))

    def panel_stub(self):
        return SimpleNamespace(forecast_busy=False, forecast_attempt=None, forecast_next=NOW,
                               forecast_store=ForecastStore(), forecast_results=queue.Queue(),
                               demo=False, save_session=Mock(), add_history=Mock(),
                               forecast_section=SimpleNamespace(render=Mock()))

    def run_request(self, panel, **kwargs):
        def immediate_thread(*, target, daemon):
            return SimpleNamespace(start=target)
        with patch("reset_card_panel.utcnow", return_value=NOW), \
                patch("reset_card_panel.threading.Thread", side_effect=immediate_thread), \
                patch("reset_card_panel.collect_forecasts", return_value={"sources": []}) as collect:
            Panel.request_forecasts(panel, **kwargs)
        return collect

    def test_manual_request_is_forwarded_but_automatic_request_is_not_manual(self):
        for manual in (True, False):
            panel = self.panel_stub()
            collect = self.run_request(panel, manual=manual)
            collect.assert_called_once_with({"schema": 1, "sources": [], "seen": {}}, manual=manual)
            self.assertTrue(panel.forecast_busy)

    def test_manual_request_inside_sixty_seconds_explains_throttle(self):
        panel = self.panel_stub()
        panel.forecast_attempt = NOW - timedelta(seconds=30)
        collect = self.run_request(panel, manual=True)
        collect.assert_not_called()
        panel.add_history.assert_called_once()
        self.assertIn("60 秒", panel.add_history.call_args.args[1])
        panel.save_session.assert_called_once()

    def test_demo_request_never_uses_real_collector(self):
        panel = self.panel_stub()
        panel.demo = True
        self.run_request(panel, manual=True).assert_not_called()
        result, error = panel.forecast_results.get_nowait()
        self.assertIsNone(error)
        self.assertTrue(result["sources"])

    def test_drain_saves_failure_transition_without_popup_and_deduplicates(self):
        panel = self.panel_stub()
        panel.forecast_failed_sources = set()
        panel.forecast_results.put(({"sources": [source("a", True), source("b", True)]}, None))
        panel.save_forecasts, panel.render, panel.show_notice = Mock(), Mock(), Mock()
        Panel.drain_forecasts(panel)
        panel.add_history.assert_called_once()
        panel.save_session.assert_called_once()
        panel.show_notice.assert_not_called()
        panel.forecast_results.put(({"sources": [source("a", True), source("b", True)]}, None))
        Panel.drain_forecasts(panel)
        self.assertEqual(panel.add_history.call_count, 1)
        self.assertEqual(panel.save_session.call_count, 1)
        panel.forecast_results.put(({"sources": [source("a"), source("b", True)]}, None))
        Panel.drain_forecasts(panel)
        self.assertEqual(panel.add_history.call_count, 2)
        self.assertEqual(panel.save_session.call_count, 2)
        self.assertEqual(panel.forecast_failed_sources, {"b"})

    def test_failure_identity_is_persisted_with_session(self):
        panel = self.panel_stub()
        panel.model = SimpleNamespace(saved=lambda: {})
        panel.live_attempt, panel.next_live = None, NOW
        panel.blocked, panel.failures = False, 0
        panel.history_entries, panel.pending_critical = [], []
        panel.forecast_failed_sources = {"b", "a"}
        panel.data_dir = Path("not-written")
        panel.reminders = SimpleNamespace(state={})
        with patch("reset_card_panel.save_json") as save:
            Panel.save_session(panel)
        self.assertEqual(save.call_args_list[0].args[1]["forecastFailedSources"], ["a", "b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Pure forecast display checks; optional real Tk smoke uses fictional data only."""
from datetime import datetime, timedelta, timezone
import unittest

from forecast_widgets import confidence_view, forecast_event_view, forecast_health_counts, forecast_source_view

NOW = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)


def sample(**changes):
    event = {"id": "demo-reset", "title": "演示公开重置活动", "sourceId": "demo-source", "sourceName": "演示来源",
             "url": "https://example.invalid/demo", "publishedAt": (NOW - timedelta(hours=1)).isoformat(),
             "resetAt": (NOW + timedelta(minutes=10)).isoformat(), "timeKind": "exact", "eventKind": "forecast",
             "confidence": "high", "confidenceReason": "演示原帖明确给出带时区的时间", "status": "upcoming"}
    event.update(changes)
    return event


class ForecastViewTests(unittest.TestCase):
    def test_future_exact_time_counts_down_in_beijing(self):
        view = forecast_event_view(sample(), NOW)
        self.assertTrue(view["canCountdown"])
        self.assertEqual(view["countdown"], "00:10:00")
        self.assertEqual(view["countdownLabel"], "距预计重置")
        self.assertIn("2026-10-23 16:10:00", view["time"])

    def test_one_second_tick(self):
        self.assertEqual(forecast_event_view(sample(), NOW + timedelta(seconds=1))["countdown"], "00:09:59")

    def test_no_time_never_uses_publication_date(self):
        view = forecast_event_view(sample(resetAt=None), NOW)
        self.assertFalse(view["canCountdown"])
        self.assertEqual(view["time"], "未公布准确时间")
        self.assertNotIn("15:00:00", view["time"])
        self.assertIn("15:00:00", view["published"])

    def test_missing_timezone_is_not_precise(self):
        self.assertFalse(forecast_event_view(sample(resetAt="2026-10-23T17:00:00"), NOW)["canCountdown"])

    def test_deadline_is_labelled_as_deadline(self):
        view = forecast_event_view(sample(timeKind="deadline", canCountdown=True), NOW)
        self.assertTrue(view["canCountdown"])
        self.assertEqual(view["countdownLabel"], "距预告截止")
        self.assertNotIn("距重置", view["countdownLabel"])

    def test_window_requires_collector_permission_and_is_labelled(self):
        self.assertFalse(forecast_event_view(sample(timeKind="window"), NOW)["canCountdown"])
        view = forecast_event_view(sample(timeKind="window", canCountdown=True), NOW)
        self.assertTrue(view["canCountdown"])
        self.assertEqual(view["countdownLabel"], "距预告窗口结束")

    def test_explicit_collector_rejection_is_preserved(self):
        self.assertFalse(forecast_event_view(sample(canCountdown=False), NOW)["canCountdown"])

    def test_estimate_and_unknown_never_count_down_even_when_flagged(self):
        for kind in ("estimate", "unknown", "nonsense"):
            with self.subTest(kind=kind):
                self.assertFalse(forecast_event_view(sample(timeKind=kind, canCountdown=True), NOW)["canCountdown"])

    def test_estimate_at_only_displays_unverified_claim_and_never_counts_down(self):
        event = sample(resetAt=None, estimateAt=(NOW + timedelta(minutes=20)).isoformat(), timeKind="window",
                       timeReason="原帖 PST 与当地夏令时存在歧义", canCountdown=True)
        view = forecast_event_view(event, NOW)
        self.assertEqual(view["time"], "来源估计 2026-10-23 16:20:00（待核实）")
        self.assertFalse(view["canCountdown"])
        self.assertEqual(view["countdown"], "未公布准确时间")
        self.assertIn("PST", view["timeReason"])
        self.assertIn("不代表实际到账", view["status"])
        self.assertIsNone(event["resetAt"])

    def test_estimate_at_without_timezone_is_not_displayed_as_a_verified_date(self):
        view = forecast_event_view(sample(resetAt=None, estimateAt="2026-10-23T17:00:00", canCountdown=True), NOW)
        self.assertEqual(view["time"], "未公布准确时间")
        self.assertFalse(view["canCountdown"])

    def test_past_time_is_not_claimed_as_completed_reset(self):
        for at in (NOW, NOW - timedelta(seconds=1)):
            view = forecast_event_view(sample(resetAt=at.isoformat()), NOW)
            self.assertFalse(view["canCountdown"])
            self.assertEqual(view["countdown"], "预告时间已过")
            self.assertIn("不代表", view["status"])

    def test_stale_and_inactive_do_not_count_down(self):
        for changes in ({"stale": True}, {"status": "expired"}, {"status": "cancelled"}, {"eventKind": "related"}):
            with self.subTest(changes=changes):
                self.assertFalse(forecast_event_view(sample(**changes), NOW)["canCountdown"])

    def test_grant_is_not_labelled_reset(self):
        view = forecast_event_view(sample(eventKind="grant"), NOW)
        self.assertEqual(view["countdownLabel"], "距预告发放")
        self.assertIn("预告发放", view["time"])

    def test_secondary_claim_is_capped_at_medium(self):
        view = confidence_view(sample(originalUrl="https://example.invalid/original"))
        self.assertEqual(view["label"], "中证据")
        self.assertIn("以原帖为准", view["reason"])

    def test_unknown_grade_has_no_invented_probability(self):
        for confidence in (None, "99%", "certain", [], {}):
            self.assertEqual(confidence_view({"confidence": confidence})["label"], "低证据")

    def test_original_link_is_preferred(self):
        self.assertEqual(forecast_event_view(sample(originalUrl="https://example.invalid/original"), NOW)["url"], "https://example.invalid/original")

    def test_generic_title_uses_bounded_original_excerpt(self):
        text = "明天将公布 Codex 重置活动，具体额度规则待官方确认。" * 20
        view = forecast_event_view(sample(title="Codex 公开重置活动", text=text), NOW)
        self.assertTrue(view["title"].startswith("明天将公布 Codex"))
        self.assertEqual(len(view["title"]), 80)
        self.assertTrue(view["title"].endswith("…"))
        self.assertEqual(len(view["summary"]), 320)

    def test_explicit_and_fictional_titles_are_preserved(self):
        for title in ("官方公布新的领取截止时间", "演示公开重置活动"):
            view = forecast_event_view(sample(title=title, text="原文不会替换明确标题"), NOW)
            self.assertEqual(view["title"], title)

    def test_excerpt_separates_context_and_time_evidence_and_collapses_whitespace(self):
        view = forecast_event_view(sample(title="Codex 公开重置活动", text="Quota\n\n reset\t notice",
                                          context="父帖背景" * 50, timeReason="原帖明确时区"), NOW)
        self.assertEqual(view["title"], "Quota reset notice")
        self.assertEqual(view["summary"], "Quota reset notice")
        self.assertEqual(len(view["context"]), 100)
        self.assertEqual(view["timeReason"], "原帖明确时区")

    def test_source_separates_fetch_time_from_forecast(self):
        source = {"id": "demo-source", "name": "演示来源", "lastSuccessAt": NOW.isoformat(), "confidence": "medium"}
        view = forecast_source_view(source, [sample()], NOW)
        self.assertIn("16:00:00", view["last"])
        self.assertIn("16:10:00", view["time"])
        self.assertIn("1 条", view["state"])

    def test_source_without_future_event_does_not_infer_time(self):
        view = forecast_source_view({"sourceId": "demo-source", "sourceName": "演示来源"}, [sample(resetAt=NOW.isoformat())], NOW)
        self.assertEqual(view["time"], "未公布准确时间")

    def test_source_error_and_old_cache_are_explicit(self):
        self.assertIn("采集失败", forecast_source_view({"error": "网络不可用"}, [], NOW)["state"])
        self.assertIn("旧缓存", forecast_source_view({"stale": True}, [], NOW)["state"])

    def test_source_failure_shows_retry_in_beijing_and_redacts_detail(self):
        view = forecast_source_view({"error": "TLS 连接中断", "nextAttemptAt": (NOW + timedelta(minutes=15)).isoformat(),
                                     "errorDetail": "https://username:private@proxy.invalid/ Bearer sensitive password=secret token=private"}, [], NOW)
        self.assertIn("下次重试 2026-10-23 16:15:00 北京时间", view["state"])
        self.assertIn("下次重试", view["retry"])
        for secret in ("username", "private", "sensitive", "password=secret"):
            self.assertNotIn(secret, view["errorDetail"])
        self.assertIn("已隐藏", view["errorDetail"])

    def test_health_counts_distinguish_failed_retrying_stale_and_updated(self):
        sources = [{"lastSuccessAt": NOW.isoformat()},
                   {"lastSuccessAt": NOW.isoformat(), "stale": True},
                   {"error": "TLS 中断", "nextAttemptAt": (NOW + timedelta(minutes=5)).isoformat()},
                   {"error": "连接失败", "nextAttemptAt": (NOW - timedelta(seconds=1)).isoformat()}, {}]
        self.assertEqual(forecast_health_counts(sources, NOW), {"updated": 1, "failed": 2, "waiting": 1})

    def test_merged_event_keeps_count_but_does_not_assign_another_sites_time(self):
        source = {"id": "second", "name": "第二演示来源", "lastSuccessAt": NOW.isoformat()}
        event = sample(sourceIds=["demo-source", "second"])
        view = forecast_source_view(source, [event], NOW)
        self.assertEqual(view["time"], "未公布准确时间")
        self.assertIn("1 条", view["state"])

    def test_source_specific_report_overrides_merged_primary_time(self):
        source = {"id": "second", "name": "第二演示来源", "lastSuccessAt": NOW.isoformat()}
        event = sample(sources=[{"sourceId": "second", "resetAt": (NOW + timedelta(minutes=20)).isoformat(), "timeKind": "deadline"}])
        view = forecast_source_view(source, [event], NOW)
        self.assertIn("16:20:00", view["time"])
        self.assertIn("预告截止", view["time"])

    def test_merged_source_metadata_does_not_prove_its_own_date(self):
        source = {"id": "second", "name": "第二演示来源"}
        event = sample(sources=[{"id": "second", "name": "第二演示来源", "url": "https://example.invalid/second"}])
        self.assertEqual(forecast_source_view(source, [event], NOW)["time"], "未公布准确时间")

    def test_source_own_events_and_grade_override_merged_claim(self):
        source = {"id": "second", "name": "第二演示来源", "lastSuccessAt": NOW.isoformat(),
                  "confidence": "low", "confidenceReason": "来源时区有歧义", "timeReason": "原文未明确时区",
                  "events": [sample(sourceId="second", resetAt=(NOW + timedelta(minutes=40)).isoformat(), confidence="medium")]}
        view = forecast_source_view(source, [sample()], NOW)
        self.assertIn("16:40:00", view["time"])
        self.assertEqual(view["confidence"], "低证据")
        self.assertIn("时区有歧义", view["reason"])
        self.assertIn("未明确时区", view["timeReason"])

    def test_source_row_explicit_time_and_count_take_precedence(self):
        source = {"id": "demo-source", "name": "演示来源", "lastSuccessAt": NOW.isoformat(), "count": 3,
                  "resetAt": (NOW + timedelta(minutes=30)).isoformat(), "timeKind": "deadline"}
        view = forecast_source_view(source, [sample()], NOW)
        self.assertIn("16:30:00", view["time"])
        self.assertIn("3 条", view["state"])

    def test_source_own_estimate_is_shown_despite_generic_pending_time_text(self):
        source = {"id": "second", "name": "第二演示来源", "resetAt": None, "timeKind": "window",
                  "estimateAt": (NOW + timedelta(minutes=30)).isoformat(), "timeText": "时间待核实",
                  "timeReason": "站点换算采用 PST，原文时区待确认", "events": []}
        view = forecast_source_view(source, [sample()], NOW)
        self.assertEqual(view["time"], "来源估计 2026-10-23 16:30:00（待核实）")
        self.assertIn("PST", view["timeReason"])

    def test_source_does_not_inherit_primary_sites_estimate(self):
        source = {"id": "second", "name": "第二演示来源"}
        event = sample(resetAt=None, estimateAt=(NOW + timedelta(minutes=30)).isoformat(), sourceIds=["demo-source", "second"])
        self.assertEqual(forecast_source_view(source, [event], NOW)["time"], "未公布准确时间")

    def test_empty_shapes_are_tolerated(self):
        self.assertFalse(forecast_event_view(None, NOW)["canCountdown"])
        self.assertEqual(forecast_source_view(None, [], NOW)["name"], "未命名来源")


def run_tk_smoke():
    """Exercise actual widgets with demo data; never invokes network or CLI."""
    import tkinter as tk
    from forecast_widgets import ForecastFloating, ForecastSection

    class Store:
        def rows(self):
            return [sample(), sample(id="demo-unknown", title="演示时区待核实", resetAt=None, confidence="low", timeKind="window",
                                     estimateAt=(NOW + timedelta(minutes=20)).isoformat(), timeReason="演示原帖时区歧义")]

        def source_rows(self):
            return [{"id": "demo-source", "name": "演示来源", "url": "https://example.invalid/", "lastSuccessAt": NOW.isoformat()}]

    root = tk.Tk()
    root.geometry("1100x750")
    opened, toggled = [], []
    section = ForecastSection(root, lambda: None, lambda: toggled.append(True), opened.append,
                              tk.BooleanVar(value=True), tk.BooleanVar(value=True), tk.BooleanVar(value=True))
    section.frame.pack(fill="both", expand=True)
    section.render(Store(), NOW)
    floating = ForecastFloating(root, lambda: None, lambda: None)
    try:
        root.update()
        assert len(section.source_tree.get_children()) == 1
        assert len(section.event_tree.get_children()) == 2
        assert "原文发布时间" in section.reason.cget("text")
        assert "演示来源" in section.source_reason.cget("text")
        assert "来源估计" in section.event_tree.item("event:demo-unknown", "values")[1]
        section.open_event_button.invoke()
        assert opened == ["https://example.invalid/demo"]
        section.topmost_switch.invoke()
        assert toggled == [True]
        section.render(Store(), NOW, busy=True)
        assert section.refresh_button.cget("state") == "disabled"
        floating.render(sample(), NOW)
        floating.position(bottom_offset=240)
        root.update()
        assert floating.value.cget("text") == "00:10:00"
        assert floating.window.winfo_viewable()
        assert floating.status.winfo_rooty() + floating.status.winfo_height() <= floating.window.winfo_rooty() + floating.window.winfo_height()
        floating.set_topmost(False)
        root.update()
        assert not floating.window.attributes("-topmost")
        floating.render(None, NOW)
        assert floating.value.cget("text") == "暂无准确预告"
        section.event_tree.selection_set("event:demo-unknown")
        section._selected()
        assert "演示原帖时区歧义" in section.reason.cget("text")
        floating.render(Store().rows()[1], NOW)
        assert floating.value.cget("text") == "未公布准确时间"
        assert "来源估计" in floating.time.cget("text")
        return {"forecastWidgetSmoke": "passed", "network": False, "realCardOperations": False,
                "sourceRows": 1, "eventRows": 2, "separatePublicationTime": True, "floatingVisible": True,
                "floatingOffset": 240, "noTimeHasNoCountdown": True, "topmostToggle": True}
    finally:
        floating.close()
        root.destroy()


if __name__ == "__main__":
    import json
    import sys
    if "--tk-smoke" in sys.argv:
        print(json.dumps(run_tk_smoke(), ensure_ascii=False))
    else:
        unittest.main(verbosity=2)

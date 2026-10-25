"""Pure forecast display checks; optional real Tk smoke uses fictional data only."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock

from forecast_widgets import ForecastSection, confidence_view, forecast_event_view, forecast_health_counts, forecast_source_view

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

    def test_manual_trust_counts_down_locked_estimate_without_changing_evidence(self):
        event = sample(manualTrusted=True, timeKind="estimate", confidence="low", stale=True, canCountdown=False)
        view = forecast_event_view(event, NOW)
        self.assertTrue(view["canCountdown"])
        self.assertEqual(view["countdown"], "00:10:00")
        self.assertEqual(view["confidence"], "低证据")
        self.assertIn("手动采信", view["countdownLabel"])
        self.assertIn("预计时间 2026-10-23 16:10:00", view["time"])
        self.assertIn("旧缓存", view["status"])
        self.assertEqual(event["confidence"], "low")

    def test_manual_trust_supports_locked_datetime_without_inventing_missing_time(self):
        event = sample(resetAt=None, reset=NOW + timedelta(minutes=20), manualTrusted=True)
        self.assertEqual(forecast_event_view(event, NOW)["countdown"], "00:20:00")
        for changes in ({"resetAt": None}, {"resetAt": "2026-10-23T17:00:00"}):
            view = forecast_event_view(sample(manualTrusted=True, remainingSeconds=100, **changes), NOW)
            self.assertFalse(view["canCountdown"])
            self.assertIn("时间待确认", view["countdown"])

    def test_manual_trust_ended_or_paused_does_not_show_running_countdown(self):
        for changes in ({"trustEnded": True}, {"resetAt": NOW.isoformat()}):
            view = forecast_event_view(sample(manualTrusted=True, **changes), NOW)
            self.assertFalse(view["canCountdown"])
            self.assertEqual(view["countdown"], "采信时间已到")
        for changes in ({"trustPaused": True}, {"status": "cancelled"}, {"status": "completed"}):
            view = forecast_event_view(sample(manualTrusted=True, **changes), NOW)
            self.assertFalse(view["canCountdown"])
            self.assertEqual(view["countdown"], "手动采信已暂停")
            self.assertIn("手动采信", view["status"])

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


class ManualTrustControlsTests(unittest.TestCase):
    def section(self, event):
        section = ForecastSection.__new__(ForecastSection)
        section.event_tree = Mock()
        section.event_tree.selection.return_value = ("event:demo-reset",)
        section._events = {"event:demo-reset": event}
        section._event_views = {"event:demo-reset": forecast_event_view(event, NOW)}
        section.reason, section.open_event_button, section.trust_button, section.trust_hint = Mock(), Mock(), Mock(), Mock()
        section.on_trust, section.on_clear_trust = Mock(), Mock()
        section.trusted_event = None
        return section

    def test_selected_low_evidence_estimate_calls_backend_group_identity(self):
        event = sample(groupId="group-of-original-post", canTrust=True, confidence="low", timeKind="estimate",
                       manualCandidateAt=(NOW + timedelta(minutes=10)).isoformat(), trustReason="站点估算，可自行选择采信")
        section = self.section(event)
        section._selected()
        section.trust_button.configure.assert_called_with(state="normal")
        self.assertIn("站点估算，可自行选择采信", section.reason.configure.call_args.kwargs["text"])
        self.assertIn("预计时间 2026-10-23 16:10:00", section.reason.configure.call_args.kwargs["text"])
        self.assertIn("可采信预计时间：2026-10-23 16:10:00", section.trust_hint.configure.call_args.kwargs["text"])
        section._trust_selected()
        section.on_trust.assert_called_once_with("group-of-original-post")
        self.assertEqual(event["confidence"], "low")

    def test_no_time_reason_disables_button_and_guards_callback(self):
        section = self.section(sample(resetAt=None, groupId="no-date", canTrust=False, trustReason="未公布任何可用预计时间"))
        section._selected()
        section.trust_button.configure.assert_called_with(state="disabled")
        self.assertIn("未公布任何可用预计时间", section.reason.configure.call_args.kwargs["text"])
        self.assertIn("无法采信：未公布任何可用预计时间", section.trust_hint.configure.call_args.kwargs["text"])
        section._trust_selected()
        section.on_trust.assert_not_called()

    def test_cancellation_requires_current_manual_target(self):
        section = self.section(sample())
        section._clear_trust()
        section.on_clear_trust.assert_not_called()
        section.trusted_event = sample(manualTrusted=True)
        section._clear_trust()
        section.on_clear_trust.assert_called_once_with()

    def test_no_selected_row_disables_trust(self):
        section = self.section(sample())
        section.event_tree.selection.return_value = ()
        section._selected()
        section.trust_button.configure.assert_called_with(state="disabled")
        section._trust_selected()
        section.on_trust.assert_not_called()

    def test_candidate_time_identifies_the_mirror_that_reported_it(self):
        event = sample(groupId="estimate-original", canTrust=True, manualCandidateAt=NOW.isoformat(),
                       manualCandidateSourceName="提供时间的网站", trustReason="估计时间")
        section = self.section(event)
        section._selected()
        self.assertIn("来源：提供时间的网站", section.trust_hint.configure.call_args.kwargs["text"])
        self.assertIn("时间来源：提供时间的网站", section.reason.configure.call_args.kwargs["text"])

    def source_section(self, events):
        section = self.section(sample())
        section._events = events
        section._event_views = {key: forecast_event_view(row, NOW) for key, row in events.items()}
        section.source_tree, section.open_source_button, section.source_reason = Mock(), Mock(), Mock()
        section.source_tree.selection.return_value = ("source:b",)
        section._sources = {"source:b": {"id": "b", "name": "网站 B"}}
        section._source_views = {"source:b": forecast_source_view(section._sources["source:b"], [], NOW)}
        section._last_source_selection = "source:a"
        section._source_empty_name = ""
        return section

    def test_source_selection_locates_trustable_event_from_merged_sources(self):
        section = self.source_section({
            "event:unrelated": sample(sourceId="a", canTrust=True),
            "event:newer-unknown": sample(sourceId="b", status="upcoming", canTrust=False, publishedAt=NOW.isoformat()),
            "event:merged-estimate": sample(sourceId="a", sourceIds=["a", "b"], canTrust=True,
                                             publishedAt=(NOW-timedelta(hours=1)).isoformat())})
        section._source_selected(event=object())
        section.event_tree.selection_set.assert_called_once_with("event:merged-estimate")
        section.event_tree.see.assert_called_once_with("event:merged-estimate")

    def test_source_selection_without_trustable_event_prefers_latest_upcoming(self):
        section = self.source_section({
            "event:completed": sample(sourceId="b", status="completed", canTrust=False, publishedAt=NOW.isoformat()),
            "event:old": sample(sourceId="b", status="upcoming", canTrust=False, publishedAt=(NOW-timedelta(hours=2)).isoformat()),
            "event:recent": sample(sourceId="b", status="upcoming", canTrust=False, publishedAt=(NOW-timedelta(hours=1)).isoformat())})
        section._source_selected(event=object())
        section.event_tree.selection_set.assert_called_once_with("event:recent")

    def test_render_and_queued_source_selection_do_not_override_event_choice(self):
        section = self.source_section({"event:estimate": sample(sourceId="b", canTrust=True)})
        section._source_selected()  # render records the programmatic selection
        section._source_selected(event=object())  # queued TreeviewSelect
        section.event_tree.selection_set.assert_not_called()

    def test_source_without_events_clears_unrelated_selection_and_explains(self):
        section = self.source_section({"event:unrelated": sample(sourceId="a", canTrust=True)})
        section.event_tree.selection.return_value = ("event:unrelated",)
        section.event_tree.selection_remove.side_effect = lambda *keys: setattr(section.event_tree.selection, "return_value", ())
        section._source_selected(event=object())
        section.event_tree.selection_remove.assert_called_once_with("event:unrelated")
        section.trust_button.configure.assert_called_with(state="disabled")
        self.assertIn("网站 B暂无相关预告", section.trust_hint.configure.call_args.kwargs["text"])


def run_tk_smoke():
    """Exercise actual widgets with demo data; never invokes network or CLI."""
    import tkinter as tk
    from forecast_widgets import ForecastFloating, ForecastSection

    class Store:
        def rows(self):
            return [sample(groupId="demo-exact", canTrust=True, trustReason="演示明确时间，可选择采信",
                           sourceId="demo-source", sourceIds=["demo-source"],
                           manualCandidateAt=(NOW + timedelta(minutes=10)).isoformat()),
                    sample(id="demo-unknown", groupId="demo-estimate", title="演示时区待核实", resetAt=None,
                           sourceId="demo-second", sourceIds=["demo-second"],
                           confidence="low", timeKind="window", canTrust=True, trustReason="演示估算，可自行选择采信",
                           manualCandidateSourceName="演示来源 B",
                           manualCandidateAt=(NOW + timedelta(minutes=20)).isoformat(),
                           estimateAt=(NOW + timedelta(minutes=20)).isoformat(), timeReason="演示原帖时区歧义")]

        def source_rows(self):
            return [{"id": "demo-source", "name": "演示来源", "url": "https://example.invalid/", "lastSuccessAt": NOW.isoformat()},
                    {"id": "demo-second", "name": "演示来源 B", "url": "https://example.invalid/b", "lastSuccessAt": NOW.isoformat()},
                    {"id": "demo-empty", "name": "演示空来源", "url": "https://example.invalid/empty", "lastSuccessAt": NOW.isoformat()}]

    root = tk.Tk()
    root.geometry("1100x750")
    opened, toggled, trusted_ids, cleared = [], [], [], []
    section = ForecastSection(root, lambda: None, lambda: toggled.append(True), opened.append,
                              tk.BooleanVar(value=True), tk.BooleanVar(value=True), tk.BooleanVar(value=True),
                              on_trust=trusted_ids.append, on_clear_trust=lambda: cleared.append(True))
    section.frame.pack(fill="both", expand=True)
    section.render(Store(), NOW)
    floating = ForecastFloating(root, lambda: None, lambda: None)
    try:
        root.update()
        assert len(section.source_tree.get_children()) == 3
        assert len(section.event_tree.get_children()) == 2
        assert "原文发布时间" in section.reason.cget("text")
        assert "演示来源" in section.source_reason.cget("text")
        assert "来源估计" in section.event_tree.item("event:demo-unknown", "values")[1]
        section.open_event_button.invoke()
        assert opened == ["https://example.invalid/demo"]
        assert "可手动采信 2 条" in section.summary.cget("text")
        section.source_tree.selection_set("source:demo-second")
        root.update()
        assert section.event_tree.selection() == ("event:demo-unknown",), "选择网站没有定位该站预告"
        assert "来源：演示来源 B" in section.trust_hint.cget("text")
        section.event_tree.selection_set("event:demo-reset")
        root.update()
        section.render(Store(), NOW)
        root.update()
        assert section.event_tree.selection() == ("event:demo-reset",), "定时渲染覆盖了手动选择"
        section.source_tree.selection_set("source:demo-empty")
        root.update()
        assert not section.event_tree.selection(), "没有相关预告的网站仍选中别站事件"
        assert "演示空来源暂无相关预告" in section.trust_hint.cget("text")
        section.render(Store(), NOW)
        root.update()
        assert not section.event_tree.selection(), "定时渲染重新选中了无关预告"
        section.source_tree.selection_set("source:demo-source")
        root.update()
        assert section.event_tree.selection() == ("event:demo-reset",)
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
        assert section.trust_button.cget("state") == "normal"
        section.trust_button.invoke()
        assert trusted_ids == ["demo-estimate"]
        trusted = dict(Store().rows()[1], manualTrusted=True, resetAt=(NOW + timedelta(minutes=20)).isoformat(), stale=True)
        section.render(Store(), NOW, trusted=trusted)
        assert section.event_tree.selection() == ("event:demo-unknown",)
        assert section.trust_button.cget("state") == "normal"
        assert section.clear_trust_button.cget("state") == "normal"
        assert "演示时区待核实" in section.trust_status.cget("text")
        assert "16:20:00" in section.trust_status.cget("text")
        assert "手动采信" in section.event_tree.item("event:demo-unknown", "values")[1]
        assert "16:20:00" in section.event_tree.item("event:demo-unknown", "values")[1]
        section.render(Store(), NOW, trusted=dict(trusted, trustPaused=True, trustEnded=True, status="completed"))
        assert "已暂停" in section.trust_status.cget("text")
        assert "重新选择" in section.trust_status.cget("text")
        section.render(Store(), NOW, trusted=trusted)
        floating.render(trusted, NOW)
        root.update()
        assert floating.value.cget("text") == "00:20:00"
        assert "手动采信" in floating.label.cget("text")
        assert "低证据" in floating.source.cget("text")
        assert "旧缓存" in floating.status.cget("text")
        assert floating.status.winfo_rooty() + floating.status.winfo_height() <= floating.window.winfo_rooty() + floating.window.winfo_height()
        section.clear_trust_button.invoke()
        assert cleared == [True]
        section.render(Store(), NOW)
        assert section.clear_trust_button.cget("state") == "disabled"
        assert "当前手动采信：无" in section.trust_status.cget("text")
        class NoTrustStore(Store):
            def rows(self):
                return [sample(groupId="unknown", resetAt=None, canTrust=False, trustReason="没有具体时间，不能开启手动倒计时")]
        section.render(NoTrustStore(), NOW)
        root.update()
        assert "可手动采信 0 条" in section.summary.cget("text")
        assert "当前没有带未来具体时间" in section.summary.cget("text")
        assert section.trust_button.cget("state") == "disabled"
        assert "没有具体时间" in section.trust_hint.cget("text")
        return {"forecastWidgetSmoke": "passed", "network": False, "realCardOperations": False,
                "sourceRows": 3, "eventRows": 2, "separatePublicationTime": True, "floatingVisible": True,
                "floatingOffset": 240, "noTimeHasNoCountdown": True, "topmostToggle": True,
                "manualTrustSelection": True, "manualTrustCancel": True, "manualTrustKeepsEvidence": True,
                "sourceSelectionLocatesEvent": True, "renderPreservesUserSelection": True,
                "emptySourceHasNoUnrelatedSelection": True, "pausedChoiceExplained": True,
                "zeroTrustableForecastsExplained": True}
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

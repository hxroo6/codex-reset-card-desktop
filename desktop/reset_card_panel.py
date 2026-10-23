"""Chinese Windows dashboard for codex-reset-card."""
from __future__ import annotations

import argparse
import copy
import ctypes
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from datetime import timedelta
from urllib.parse import urlsplit
import webbrowser

from panel_logic import (BEIJING, OUTCOMES, PanelModel, ReminderEngine, clock_text,
                         demo_status, load_json, parse_time, read_status,
                         remaining_text, safe_error, save_json, utcnow)
from floating_countdown import FloatingCountdown, automatic_use_view, work_area
from app_config import MIN_EXPIRY_RESERVE_SECONDS
from forecast_logic import ForecastStore, demo_forecasts
from forecast_sources import SOURCES, collect_forecasts
from forecast_widgets import ForecastSection, ForecastFloating

BG = "#F0F5F8"
NAVY = "#102C40"
TEAL = "#008A81"
TEXT = "#16354B"
MUTED = "#526C7F"
WHITE = "#FFFFFF"
ORANGE = "#A95C08"
RED = "#AC3544"


def forecast_health_change(sources, previous_failed=()):
    """Report source failures/recoveries once, without displaying error popups."""
    sources = [source for source in sources if isinstance(source, dict) and source.get("id")]
    failed = {source["id"] for source in sources if source.get("error")}
    previous_failed = set(previous_failed)
    if failed == previous_failed:
        return failed, None
    updated = [source for source in sources if source.get("lastSuccessAt") and not source.get("error")]
    recovered = [source for source in updated if source["id"] in previous_failed]
    message = f"{len(sources)} 个公开来源：{len(updated)} 个可读取，{len(failed)} 个采集失败。"
    if failed:
        message += "全部来源暂时无法采集；" if len(failed) == len(sources) else "部分来源暂时无法采集；"
        message += "保留上次成功缓存，失败来源按各自退避时间重试。"
    if recovered:
        message += f"已恢复 {len(recovered)} 个：" + "、".join(safe_error(source.get("name") or source["id"]) for source in recovered) + "。"
    title = "公开预告采集恢复" if recovered else "公开预告采集异常"
    return failed, (title, message)


class Panel:
    def __init__(self, root, demo=False, smoke=False, minimized=False, smoke_report=None):
        self.root, self.demo = root, demo
        self.smoke = smoke
        self.smoke_report = Path(smoke_report) if smoke_report else None
        self.root.title("Codex Reset Card · 到期与提醒" + (" [演示模式]" if demo else ""))
        self.root.geometry("1160x900")
        self.root.minsize(1000, 730)
        self.root.configure(bg=BG)
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        self.data_dir = local / "CodexResetCardPanel" / ("demo" if demo else "data")
        self.session = load_json(self.data_dir / "session.json", {})
        self.settings = load_json(self.data_dir / "settings.json", {"sound": True, "autoLive": True})
        self.model = PanelModel(self.session.get("snapshot"))
        self.reminders = ReminderEngine(load_json(self.data_dir / "reminders.json", {}))
        self.results = queue.Queue()
        self.control_results = queue.Queue()
        self.forecast_results = queue.Queue()
        self.forecast_busy = False
        self.forecast_store = ForecastStore(load_json(self.data_dir / "forecasts.json", {}))
        saved_failures = self.session.get("forecastFailedSources")
        self.forecast_failed_sources = set(saved_failures) if isinstance(saved_failures, list) and all(isinstance(value, str) for value in saved_failures) else set()
        self.forecast_next = parse_time(self.session.get("forecastNext")) or utcnow()
        self.forecast_attempt = parse_time(self.session.get("forecastAttempt"))
        cached_sites = {source.get("id") for source in self.forecast_store.saved().get("sources", []) if isinstance(source, dict)}
        if not demo and {source["id"] for source in SOURCES} - cached_sites:
            self.forecast_next = utcnow()
        self.busy = False
        self.postpone_busy = False
        self.control_message = ""
        self.control_message_until = 0.0
        self.closed = False
        self.next_cache = 0.0
        self.live_attempt = parse_time(self.session.get("liveAttempt"))
        self.next_live = parse_time(self.session.get("nextLive")) or utcnow()
        self.blocked = bool(self.session.get("autoBlocked", False))
        self.failures = int(self.session.get("failures", 0))
        self.last_notice = None
        self.last_notice_persistent = False
        self.notice_identity = None
        self.pending_critical = [tuple(item) for item in self.session.get("pendingNotices", [])
                                 if isinstance(item, list) and len(item) == 2 and all(isinstance(value, str) for value in item)][:20]
        self.history_entries = self.session.get("history", [])[-100:]
        self.sound = tk.BooleanVar(value=bool(self.settings.get("sound", True)))
        self.auto_live = tk.BooleanVar(value=bool(self.settings.get("autoLive", True)))
        self.floating_enabled = tk.BooleanVar(value=bool(self.settings.get("floatingCountdown", True)))
        self.floating_topmost = tk.BooleanVar(value=bool(self.settings.get("floatingTopmost", True)))
        self.floating_window = None
        self.forecast_enabled = tk.BooleanVar(value=bool(self.settings.get("publicForecasts", True)))
        self.forecast_floating_enabled = tk.BooleanVar(value=bool(self.settings.get("forecastFloating", True)))
        self.forecast_topmost = tk.BooleanVar(value=bool(self.settings.get("forecastTopmost", True)))
        self.forecast_window = None
        if demo:
            self.forecast_store.update(demo_forecasts())
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if demo:
            # Demo is fully isolated: no CLI process, auth files or original state.
            self.model = PanelModel()
            self.model.accept(demo_status())
            self.demo_snapshot = self.model.status
            self.blocked = False
            self.add_history("演示模式", "全部数据为本地虚构，不连接账号，不消费真实卡。")
            if smoke:
                self.auto_live.set(False)
                self.pending_critical = []
        self.render()
        self.root.after(100, self.tick)
        if self.pending_critical:
            self.root.after(350, lambda: self.show_notice(*self.pending_critical[0], persistent=True))
        if minimized:
            self.root.after(0, self.root.iconify)
        if smoke:
            self.root.after(1100, self.smoke_test)

    def build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=WHITE, fieldbackground=WHITE, foreground=TEXT, rowheight=40,
                        font=("Microsoft YaHei UI", 10), borderwidth=0)
        style.configure("Treeview.Heading", background="#E7F0F4", foreground=MUTED,
                        font=("Microsoft YaHei UI", 10, "bold"), relief="flat", padding=9)
        style.map("Treeview", background=[("selected", "#D9F2EE")], foreground=[("selected", TEXT)])
        header = tk.Frame(self.root, bg=NAVY, padx=28, pady=19)
        header.pack(fill="x")
        tk.Label(header, text="RESET CARD", bg=NAVY, fg="#58D7C4", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(header, text="卡片与重置预告，一眼掌握", bg=NAVY, fg=WHITE, font=("Microsoft YaHei UI", 23, "bold")).pack(anchor="w", pady=(2, 4))
        self.clock = tk.Label(header, bg=NAVY, fg="#B9D0DE", font=("Microsoft YaHei UI", 10))
        self.clock.pack(anchor="w")
        # Reserve the lifecycle notice before allocating the scrolling body.
        # At the minimum window size all controls remain reachable by scrolling.
        self.footer = tk.Label(self.root, bg=BG, fg=MUTED, text="", anchor="w", justify="left",
                               font=("Microsoft YaHei UI", 9), wraplength=1060)
        self.footer.pack(side="bottom", fill="x", padx=24, pady=(8, 12))
        viewport = tk.Frame(self.root, bg=BG)
        viewport.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(viewport, bg=BG, highlightthickness=0, borderwidth=0)
        body_scroll = ttk.Scrollbar(viewport, orient="vertical", command=self.canvas.yview)
        body_scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=body_scroll.set)
        body = tk.Frame(self.canvas, bg=BG, padx=24, pady=14)
        body_window = self.canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        def fit_body(event):
            self.canvas.itemconfigure(body_window, width=event.width)
            for name in ("banner", "detail", "action"):
                widget = getattr(self, name, None)
                if widget:
                    widget.configure(wraplength=max(600, event.width - 84))
            self.footer.configure(wraplength=max(600, self.root.winfo_width() - 60))
        self.canvas.bind("<Configure>", fit_body)

        def scroll_body(event):
            if event.widget is not getattr(self, "tree", None):
                self.canvas.yview_scroll(int(-event.delta / 120), "units")
        self.root.bind("<MouseWheel>", scroll_body)
        toolbar = tk.Frame(body, bg=BG)
        toolbar.pack(fill="x", pady=(0, 11))
        self.refresh_button = tk.Button(toolbar, text="在线只读刷新", command=lambda: self.request(True, manual=True),
                                        font=("Microsoft YaHei UI", 10, "bold"), bg=TEAL, fg=WHITE, activebackground="#00726B",
                                        relief="flat", padx=18, pady=8, cursor="hand2")
        self.refresh_button.pack(side="left")
        tk.Button(toolbar, text="提醒测试", command=self.test_notice, bg=WHITE, fg=TEXT, relief="flat",
                  font=("Microsoft YaHei UI", 10), padx=14, pady=8).pack(side="left", padx=8)
        if self.demo:
            tk.Label(toolbar, text="演示模式 · 虚构数据", bg=BG, fg=TEAL, font=("Microsoft YaHei UI", 11, "bold")).pack(side="left", padx=6)
        tk.Checkbutton(toolbar, text="提醒声音", variable=self.sound, command=self.save_settings, bg=BG, fg=MUTED,
                       font=("Microsoft YaHei UI", 10), activebackground=BG).pack(side="right")
        tk.Checkbutton(toolbar, text="每 5 分钟在线查询", variable=self.auto_live, command=self.save_settings, bg=BG, fg=MUTED,
                       font=("Microsoft YaHei UI", 10), activebackground=BG).pack(side="right", padx=8)
        self.floating_switch = tk.Checkbutton(toolbar, text="倒计时悬浮窗", variable=self.floating_enabled,
                       command=self.toggle_floating, bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 10), activebackground=BG)
        self.floating_switch.pack(side="right", padx=8)
        self.topmost_switch = tk.Checkbutton(toolbar, text="悬浮窗置顶", variable=self.floating_topmost,
                       command=self.toggle_floating, bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 10), activebackground=BG)
        self.topmost_switch.pack(side="right", padx=(4, 0))
        metrics = tk.Frame(body, bg=BG)
        metrics.pack(fill="x")
        self.metrics = {}
        for index, (key, title) in enumerate((("auth", "兑换授权"), ("service", "后台服务"), ("lead", "兑换提前量"), ("count", "可用卡数量"))):
            metrics.grid_columnconfigure(index, weight=1, uniform="metric")
            card = tk.Frame(metrics, bg=WHITE, padx=18, pady=13, highlightbackground="#DDE8ED", highlightthickness=1)
            card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 6, 0 if index == 3 else 6))
            tk.Label(card, text=title, bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 10)).pack(anchor="w")
            value = tk.Label(card, text="未知", bg=WHITE, fg=TEXT, font=("Microsoft YaHei UI", 18, "bold"))
            value.pack(anchor="w", pady=(6, 0))
            self.metrics[key] = value
        self.banner = tk.Label(body, bg="#E0F2ED", fg="#23685D", anchor="w", justify="left", padx=14, pady=10,
                               font=("Microsoft YaHei UI", 10), wraplength=1040)
        self.banner.pack(fill="x", pady=(12, 9))
        self.next_card = tk.Label(body, text="等待卡片信息", bg=BG, fg=TEXT, anchor="w", font=("Microsoft YaHei UI", 15, "bold"))
        self.next_card.pack(fill="x", pady=(1, 6))
        columns = ("fingerprint", "expires", "trigger", "countdown", "state")
        frame = tk.Frame(body, bg=WHITE)
        frame.pack(fill="x")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=3, selectmode="browse")
        for key, title, width in zip(columns, ("卡片指纹", "到期时间（北京时间）", "触发时间（北京时间）", "距到期", "计划状态"), (126, 195, 195, 140, 230)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=80, anchor="w", stretch=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.detail = tk.Label(body, text="", bg=BG, fg=MUTED, justify="left", anchor="w", font=("Microsoft YaHei UI", 9), wraplength=1060)
        self.detail.pack(fill="x", pady=(9, 8))
        self.forecast_section = ForecastSection(
            body, lambda: self.request_forecasts(manual=True), self.toggle_forecasts, self.open_forecast_url,
            self.forecast_enabled, self.forecast_floating_enabled, self.forecast_topmost,
            on_trust=self.trust_forecast, on_clear_trust=self.clear_forecast_trust)
        self.forecast_section.frame.pack(fill="x", pady=(4, 12))
        lower = tk.Frame(body, bg=WHITE, padx=14, pady=10)
        lower.pack(fill="both", expand=True)
        tk.Label(lower, text="最近动作与提醒", bg=WHITE, fg=TEXT, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        self.action = tk.Label(lower, bg=WHITE, fg=MUTED, anchor="w", justify="left", wraplength=1040, font=("Microsoft YaHei UI", 9))
        self.action.pack(fill="x", pady=(5, 5))
        self.history = tk.Text(lower, height=4, bg=WHITE, fg=MUTED, relief="flat", font=("Microsoft YaHei UI", 9), wrap="word", padx=0, pady=2)
        self.history.pack(fill="both", expand=True)
        self.history.configure(state="disabled")
        self.update_history()

    def save_settings(self):
        self.settings.update(sound=self.sound.get(), autoLive=self.auto_live.get(),
                             floatingCountdown=self.floating_enabled.get(), floatingTopmost=self.floating_topmost.get(),
                             publicForecasts=self.forecast_enabled.get(), forecastFloating=self.forecast_floating_enabled.get(),
                             forecastTopmost=self.forecast_topmost.get())
        try:
            save_json(self.data_dir / "settings.json", self.settings)
        except OSError as exc:
            self.add_history("设置保存失败", safe_error(exc))

    def toggle_floating(self):
        self.update_floating()
        self.save_settings()

    def hide_floating(self):
        self.floating_enabled.set(False)
        self.toggle_floating()

    def open_main_panel(self):
        self.root.deiconify()
        self.root.lift()

    def update_floating(self, now=None):
        if self.floating_enabled.get():
            if self.floating_window is None:
                self.floating_window = FloatingCountdown(self.root, self.hide_floating, self.open_main_panel,
                                                         self.floating_topmost.get(), on_postpone=self.postpone)
            self.floating_window.set_topmost(self.floating_topmost.get())
            view = automatic_use_view(self.model, now)
            if self.postpone_busy:
                view.update(postponeEnabled=False, postponeBusy=True, controlMessage="正在安全更新后台计划…")
            elif self.busy:
                view.update(postponeEnabled=False, controlMessage="正在读取状态，请稍候")
            elif self.control_message and time.monotonic() < self.control_message_until:
                view["controlMessage"] = self.control_message
            self.floating_window.render(view)
        elif self.floating_window is not None:
            self.floating_window.close()
            self.floating_window = None
        self.update_forecast_floating(now)
        self.position_notice()

    def toggle_forecasts(self):
        self.save_settings()
        self.render()

    def trust_forecast(self, group_id):
        try:
            event = self.forecast_store.trust(group_id)
        except ValueError as exc:
            self.add_history("预告无法采信", safe_error(exc))
        else:
            self.forecast_enabled.set(True)
            self.forecast_floating_enabled.set(True)
            self.save_settings()
            self.save_forecasts()
            self.add_history("手动采信预告", f"{event.get('title', '公开活动')}；锁定预计时间：{event['timeText']}。")
        self.save_session()
        self.render()

    def clear_forecast_trust(self):
        self.forecast_store.clear_trust()
        self.save_forecasts()
        self.add_history("已取消手动采信", "预告悬浮窗恢复按来源证据与明确时间自动选择。")
        self.save_session()
        self.render()

    def hide_forecast_floating(self):
        self.forecast_floating_enabled.set(False)
        self.toggle_forecasts()

    def update_forecast_floating(self, now=None):
        if self.forecast_floating_enabled.get():
            if self.forecast_window is None:
                self.forecast_window = ForecastFloating(
                    self.root, self.hide_forecast_floating, self.open_main_panel, self.forecast_topmost.get())
            self.forecast_window.set_topmost(self.forecast_topmost.get())
            event = self.forecast_store.next_event(now) if self.forecast_enabled.get() else None
            if event is None and self.forecast_enabled.get():
                # Keep an uncertain latest announcement visible, with no numeric
                # countdown. The collector's canCountdown decision is retained.
                event = next((row for row in self.forecast_store.rows(now)
                              if row.get("status") in ("upcoming", "scheduled")), None)
            self.forecast_window.render(event, now, paused=not self.forecast_enabled.get())
            self.forecast_window.position(FloatingCountdown.HEIGHT + 12 if self.floating_window else 0)
        elif self.forecast_window is not None:
            self.forecast_window.close()
            self.forecast_window = None

    def open_forecast_url(self, value):
        # Remote titles and links are untrusted data. Opening needs a user click.
        try:
            parsed = urlsplit(value)
            valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                     and not parsed.password and not any(char.isspace() for char in value)
                     and len(value) <= 2048)
        except (ValueError, TypeError):
            valid = False
        if valid:
            webbrowser.open(value)
        else:
            self.add_history("预告链接不可用", "只支持有效的公开 HTTPS 链接。")

    def save_forecasts(self):
        try:
            save_json(self.data_dir / "forecasts.json", self.forecast_store.saved())
        except OSError as exc:
            self.add_history("预告保存失败", safe_error(exc))

    def request_forecasts(self, manual=False):
        if self.forecast_busy:
            return
        now = utcnow()
        if self.forecast_attempt and (now - self.forecast_attempt).total_seconds() < 60:
            if manual:
                self.add_history("预告采集节流", "两次公开预告采集至少间隔 60 秒，请稍候；来源自身的 HTTP 访问限制仍会保留。")
                self.save_session()
            return
        self.forecast_busy = True
        self.forecast_attempt = now
        self.forecast_next = now + timedelta(minutes=15)
        self.save_session()
        previous = self.forecast_store.saved()

        def worker():
            try:
                result = demo_forecasts() if self.demo else collect_forecasts(previous, manual=manual)
                self.forecast_results.put((result, None))
            except Exception as exc:
                self.forecast_results.put((None, safe_error(exc)))
        threading.Thread(target=worker, daemon=True).start()
        self.forecast_section.render(self.forecast_store, now, busy=True, trusted=self.forecast_store.trusted_event(now))

    def drain_forecasts(self):
        try:
            result, error = self.forecast_results.get_nowait()
        except queue.Empty:
            return
        self.forecast_busy = False
        if error:
            self.add_history("公开预告采集失败", error)
        else:
            self.forecast_store.update(result)
            self.save_forecasts()
            failed, note = forecast_health_change(self.forecast_store.saved().get("sources", []), self.forecast_failed_sources)
            self.forecast_failed_sources = failed
            if note:
                self.add_history(*note)
                self.save_session()
        self.render()

    def evaluate_forecast_reminders(self):
        if self.smoke or not self.forecast_enabled.get():
            return
        notes = self.forecast_store.reminders()
        if notes:
            self.save_forecasts()
            for title, text in notes:
                self.add_history(title, text)
            # Public forecasts must never block an important card-use result.
            self.show_notice(notes[0][0], "\n".join(text for _, text in notes), persistent=False)

    def position_notice(self):
        if self.last_notice and self.last_notice.winfo_exists():
            left, top, right, bottom = work_area(self.root)
            desktop_bottom = bottom
            width, height, margin = 440, 225, 16
            if self.floating_window is not None:
                # The reminder has a native title bar; the countdown is borderless.
                bottom -= FloatingCountdown.HEIGHT + 52
            if self.forecast_window is not None:
                bottom -= ForecastFloating.HEIGHT + 12
            x, y = right - width - margin, bottom - height - margin
            if y < top + 32 and (self.floating_window or self.forecast_window):
                # On short screens put the notice beside the countdown column.
                column_width = max(FloatingCountdown.WIDTH if self.floating_window else 0,
                                   ForecastFloating.WIDTH if self.forecast_window else 0)
                x, y = right - column_width - width - margin - 12, desktop_bottom - height - margin
            x, y = max(left, x), max(top + 32, y)
            self.last_notice.geometry(f"{width}x{height}+{x}+{y}")

    def save_session(self):
        value = {"snapshot": self.model.saved(), "liveAttempt": self.live_attempt.isoformat() if self.live_attempt else None,
                 "nextLive": self.next_live.isoformat(), "autoBlocked": self.blocked, "failures": self.failures,
                 "history": self.history_entries[-100:], "pendingNotices": self.pending_critical}
        value.update(forecastNext=self.forecast_next.isoformat(),
                     forecastAttempt=self.forecast_attempt.isoformat() if self.forecast_attempt else None,
                     forecastFailedSources=sorted(self.forecast_failed_sources))
        try:
            save_json(self.data_dir / "session.json", value)
            save_json(self.data_dir / "reminders.json", self.reminders.state)
        except OSError as exc:
            self.add_history("提醒记录保存失败", safe_error(exc))

    def request(self, online=False, manual=False):
        if self.busy or self.postpone_busy:
            return
        now = utcnow()
        if online and self.live_attempt and (now - self.live_attempt).total_seconds() < 60:
            if manual:
                self.add_history("查询节流", "两次在线请求至少间隔 60 秒，请稍候。")
            return
        self.busy = True
        if online:
            self.live_attempt = now
            self.next_live = now + timedelta(minutes=5)
            self.save_session()  # Persist throttle before a network request.
        self.refresh_button.configure(state="disabled", text="正在读取…")

        def worker():
            try:
                if self.demo:
                    result = dict(self.demo_snapshot)
                    if online:
                        result["live"] = dict(self.demo_snapshot["live"], fetchedAt=utcnow().isoformat())
                    else:
                        result.pop("live", None)
                else:
                    result = read_status(online)
                self.results.put((online, result, None))
            except Exception as exc:
                self.results.put((online, None, safe_error(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def postpone(self):
        """Move only the displayed card's current plan; keep all I/O off Tk."""
        if self.postpone_busy or self.busy:
            return
        view = automatic_use_view(self.model)
        if not view.get("postponeEnabled"):
            self.control_message = view.get("postponeReason") or "当前计划不可推迟"
            self.control_message_until = time.monotonic() + 12
            self.update_floating()
            return
        fingerprint, expected = view["fingerprint"], view["triggerAt"]
        self.postpone_busy = True
        self.control_message = ""
        self.update_floating()

        def worker():
            change = None
            try:
                if self.demo:
                    # No real state writes, service commands or network calls in demo.
                    status = copy.deepcopy(self.demo_snapshot)
                    card = next(item for item in status["cards"] if item["fingerprint"] == fingerprint)
                    trigger = parse_time(card.get("nextAttemptAt") or card.get("triggerAt"))
                    if trigger != parse_time(expected):
                        raise RuntimeError("演示计划已变化，请刷新后再试")
                    delayed = trigger + timedelta(minutes=2)
                    expiry = parse_time(card["expiresAt"])
                    if delayed > expiry - timedelta(seconds=MIN_EXPIRY_RESERVE_SECONDS):
                        raise RuntimeError("再推迟将超过有效期到期前 2 分钟")
                    card["nextAttemptAt"] = delayed.isoformat()
                    change = {"fingerprint": fingerprint, "triggerAt": delayed.isoformat(),
                              "expiresAt": expiry.isoformat(), "delayedSeconds": 120}
                else:
                    from schedule_control import postpone_card
                    change = postpone_card(fingerprint, expected)
                    status = read_status(False)
                self.control_results.put((change, status, None))
            except Exception as exc:
                self.control_results.put((change, None, safe_error(exc)))
        # Finish the service restoration even if the UI is being shut down.
        threading.Thread(target=worker, daemon=False).start()

    def drain_controls(self):
        try:
            change, status, error = self.control_results.get_nowait()
        except queue.Empty:
            return
        self.postpone_busy = False
        self.control_message_until = time.monotonic() + 12
        if error:
            if change:
                self.model.status["timingReady"] = False
                self.model.local_error = error
                self.control_message = "计划已修改，状态待刷新确认"
                self.add_history("推迟后读取失败", "后台已返回新计划，但状态刷新失败：" + error)
            else:
                self.control_message = "推迟失败：" + error
                self.add_history("推迟失败", error)
            self.next_cache = 0.0
        else:
            if self.demo:
                self.demo_snapshot = status
            # accept() retains the last online snapshot on a local-only refresh.
            self.model.accept(status)
            self.next_cache = time.monotonic() + 30
            self.control_message = "已推迟 2 分钟 · 最迟到期前 2 分钟"
            self.add_history("自动使用已推迟", f"卡 {change['fingerprint']} 的新使用时间：{clock_text(change['triggerAt'])}；有效期到期：{clock_text(change['expiresAt'])}。")
        self.save_session()
        self.render()

    def drain(self):
        try:
            online, result, error = self.results.get_nowait()
        except queue.Empty:
            return
        self.busy = False
        self.next_cache = time.monotonic() + 30
        if error:
            if online:
                self.model.live_error = error
                self.failures += 1
                self.next_live = utcnow() + timedelta(seconds=min(3600, 300 * (2 ** min(self.failures - 1, 4))))
                if any(code in error for code in ("401", "403", "429")):
                    self.blocked = True
                self.add_history("在线查询失败", error)
            else:
                self.model.local_error = error
                self.add_history("本地状态读取失败", error)
        else:
            self.model.accept(result)
            if online:
                self.blocked = False
                self.failures = 0
                self.next_live = utcnow() + timedelta(minutes=5)
                self.add_history("在线只读查询", "已更新服务端快照；此操作不向原服务写入卡片。")
        self.evaluate_reminders()
        self.save_session()
        self.render()

    def evaluate_reminders(self):
        notes = self.reminders.evaluate(self.model)
        if notes:
            for title, text in notes:
                self.add_history(title, text)
            self.save_session()
            self.show_notice(" · ".join(dict.fromkeys(title for title, _ in notes)), "\n".join(text for _, text in notes))

    def tick(self):
        if self.closed:
            return
        self.drain()
        self.drain_controls()
        self.drain_forecasts()
        now = utcnow()
        if self.forecast_enabled.get() and not self.forecast_busy and now >= self.forecast_next:
            self.request_forecasts()
        if not self.busy and not self.postpone_busy:
            if self.auto_live.get() and not self.blocked and now >= self.next_live:
                self.request(True)
            elif time.monotonic() >= self.next_cache:
                self.request(False)
        self.evaluate_reminders()
        self.evaluate_forecast_reminders()
        self.render()
        self.root.after(1000, self.tick)

    def render(self):
        now = utcnow()
        status = self.model.status
        self.clock.configure(text="北京时间  " + clock_text(now) + "    ·    卡片缓存 30 秒 / 在线查询 5 分钟 / 公开预告 15 分钟")
        armed = status.get("armed")
        self.metrics["auth"].configure(text="已授权" if armed is True else "未授权" if armed is False else "未知", fg=TEAL if armed is True else ORANGE)
        service = status.get("service", {})
        running, installed = service.get("running"), service.get("installed")
        self.metrics["service"].configure(text="运行中" if running is True else "已停止" if installed is True else "未安装" if installed is False else "未知", fg=TEAL if running is True else RED)
        lead = status.get("leadTimeSeconds")
        lead_text = f"{lead / 60:g} 分钟" if isinstance(lead, (int, float)) and not isinstance(lead, bool) else "未知"
        self.metrics["lead"].configure(text=lead_text)
        source, data, source_at = self.model.source()
        count = data.get("availableCount")
        known_count = isinstance(count, int) and not isinstance(count, bool) and count >= 0
        self.metrics["count"].configure(text=f"{count} 张" if known_count else "未知")
        stale = self.model.stale(now)
        timing_unknown = status.get("timingReady") is False
        prefix = "陈旧快照 / 状态未确认" if stale else "本地执行计划待确认" if timing_unknown else "快照已更新"
        banner = f"{prefix}  ·  {source}  ·  快照时间：{clock_text(source_at)}"
        error = self.model.live_error or self.model.local_error or status.get("timingReadError", "")
        if error:
            banner += "\n最近错误：" + (error if len(error) <= 180 else error[:180] + "…（完整信息见历史）")
        if self.blocked:
            banner += "\n自动在线查询已暂停（401 / 403 / 429）；处理原因后可手动刷新。"
        self.banner.configure(text=banner, bg="#FFF0D8" if stale or timing_unknown else "#E0F2ED", fg=ORANGE if stale or timing_unknown else "#23685D")
        cards = self.model.cards(now)
        self.next_card.configure(text=("最近到期  " + remaining_text((cards[0]["expires"] - now).total_seconds())) if cards else
                                 "尚无可显示的到期卡片" if known_count and count == 0 else "等待卡片有效期详情")
        # Preserve selection while updating the countdown.
        selected = self.tree.selection()
        for row in self.tree.get_children():
            self.tree.delete(row)
        for index, card in enumerate(cards):
            if armed is False:
                label = "未授权 · 不会自动执行"
            elif not card["recorded"]:
                label = "时间推算 · 服务尚未记录"
            elif running is not True:
                label = "服务未运行 · 计划待确认"
            elif timing_unknown:
                label = "本地执行计划尚未读取 · 待确认"
            elif card["trigger"] and card["trigger"] <= now:
                label = "已到触发时间 · 等待服务结果"
            else:
                label = "原服务已记录"
            if stale:
                label += " · 快照陈旧"
            if card["outcome"]:
                label += " · " + OUTCOMES.get(card["outcome"], card["outcome"])
            trigger_text = "待确认" if timing_unknown else clock_text(card["trigger"])
            self.tree.insert("", "end", iid=str(index), values=(card["fingerprint"], clock_text(card["expires"]), trigger_text, remaining_text((card["expires"] - now).total_seconds()), label))
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        details = "原工具每天发现新卡；面板在线快照仅用于查看与提醒，不会更新原服务计划。"
        if data and data.get("detailsAvailable") is not True:
            details += " 当前未提供逐卡有效期，无法计算到期提醒。"
        details += f"\n原服务上次发现：{clock_text(status.get('lastDiscoveryAt'))}    下次发现：{clock_text(status.get('nextDiscoveryAt'))}"
        self.detail.configure(text=details)
        action = status.get("lastAction") or {}
        outcome = action.get("outcome") or action.get("message")
        action_text = f"{clock_text(action.get('at'))}  ·  {action.get('kind', '未知动作')}  ·  {OUTCOMES.get(outcome, outcome or '未提供结果')}" if action else "原工具尚未报告最近动作。"
        self.action.configure(text=action_text)
        left = max(0, int(60 - (now - self.live_attempt).total_seconds())) if self.live_attempt else 0
        if not self.busy:
            self.refresh_button.configure(state="disabled" if left else "normal", text=f"在线刷新（{left}s）" if left else "在线只读刷新")
        live_note = "自动在线查询暂停" if self.blocked else "在线自动查询已关闭" if not self.auto_live.get() else f"下次在线查询 {clock_text(self.next_live)}"
        self.footer.configure(text=f"提醒：到期前 24 小时 / 1 小时 / 10 分钟、使用结果、服务异常。最小化后仍提醒，关闭面板后提醒停止，原服务继续运行。\n{live_note}  ·  每次推迟 2 分钟，最迟到期前 2 分钟；重要结果需手动确认。")
        self.forecast_section.render(self.forecast_store, now, busy=self.forecast_busy,
                                     trusted=self.forecast_store.trusted_event(now))
        self.update_floating(now)

    def add_history(self, title, text):
        self.history_entries.append(f"{clock_text(utcnow())}  [{title}]  {text}")
        self.history_entries = self.history_entries[-100:]
        if hasattr(self, "history"):
            self.update_history()

    def update_history(self):
        self.history.configure(state="normal")
        self.history.delete("1.0", "end")
        self.history.insert("end", "\n".join(self.history_entries[-100:]))
        self.history.see("end")
        self.history.configure(state="disabled")

    def dismiss_notice(self):
        identity = self.notice_identity
        if self.last_notice and self.last_notice.winfo_exists():
            self.last_notice.destroy()
        self.last_notice = None
        self.last_notice_persistent = False
        self.notice_identity = None
        if identity in self.pending_critical:
            self.pending_critical.remove(identity)
            self.save_session()
        if self.pending_critical:
            self.show_notice(*self.pending_critical[0], persistent=True)

    def show_notice(self, title, text, persistent=None):
        if persistent is None:
            persistent = any(word in title for word in ("使用成功", "使用失败", "待确认", "此前已兑换", "检查失败"))
        if persistent and (title, text) not in self.pending_critical:
            self.pending_critical.append((title, text))
            self.pending_critical = self.pending_critical[-20:]
            self.save_session()
        if self.last_notice and self.last_notice.winfo_exists():
            if self.last_notice_persistent:
                return
            self.last_notice.destroy()
        note = tk.Toplevel(self.root)
        self.last_notice = note
        self.last_notice_persistent = bool(persistent)
        self.notice_identity = (title, text)
        note.protocol("WM_DELETE_WINDOW", self.dismiss_notice)
        note.title("Reset Card 提醒")
        note.configure(bg=NAVY, padx=20, pady=18)
        note.attributes("-topmost", True)
        self.position_notice()
        tk.Label(note, text=title, bg=NAVY, fg="#65E2CD", font=("Microsoft YaHei UI", 13, "bold"), wraplength=400, justify="left").pack(anchor="w")
        tk.Label(note, text=text, bg=NAVY, fg=WHITE, font=("Microsoft YaHei UI", 10), wraplength=400, justify="left").pack(anchor="w", pady=12)
        tk.Button(note, text="知道了", bg=TEAL, fg=WHITE, relief="flat", font=("Microsoft YaHei UI", 10), padx=15, command=self.dismiss_notice).pack(anchor="e")
        if not persistent:
            note.after(20000, lambda: self.dismiss_notice() if self.last_notice is note and note.winfo_exists() else None)
        if self.sound.get():
            try:
                import winsound
                winsound.PlaySound("SystemAsterisk" if persistent else "SystemNotification", winsound.SND_ALIAS | winsound.SND_ASYNC)
            except (ImportError, RuntimeError):
                pass

    def test_notice(self):
        text = "这是一条本地测试提醒。不会执行兑换，也不会连接账号。最小化面板后仍可收到此类提示。"
        self.add_history("提醒测试", text)
        self.show_notice("提醒功能正常", text)

    def smoke_test(self):
        # Exercises actual Tk widget layout and notification lifecycle without CLI/network.
        assert self.demo, "UI smoke test must run in demo mode"
        self.drain()
        if self.busy:
            self.root.after(100, self.smoke_test)
            return
        assert len(self.tree.get_children()) == 3
        assert "3" in self.metrics["count"].cget("text")
        self.root.update_idletasks()
        assert self.footer.winfo_ismapped(), "默认尺寸下生命周期说明不可见"
        assert self.footer.winfo_rooty() + self.footer.winfo_height() <= self.root.winfo_rooty() + self.root.winfo_height()
        self.forecast_enabled.set(True)
        self.forecast_floating_enabled.set(True)
        self.forecast_topmost.set(True)
        self.toggle_forecasts()
        self.root.update()
        assert len(self.forecast_section.source_tree.get_children()) >= 3, "未分别显示多个预告来源"
        self.forecast_section.render(ForecastStore())
        assert len(self.forecast_section.source_tree.get_children()) == len(SOURCES), "新增配置来源未全部列出"
        assert str(len(SOURCES)) + " 个公开来源" in self.forecast_section.summary.cget("text")
        # Resolve lazy site adapters in the frozen build without network I/O.
        from forecast_sources import parse_source
        for site in SOURCES:
            if site["adapter"] in ("codexclock", "whenreset-cn", "whenreset-uk", "ai-resets"):
                try:
                    parse_source(site, "{}")
                except ValueError:
                    pass
                else:
                    raise AssertionError("站点适配器未拒绝无效结构")
        self.forecast_section.render(self.forecast_store)
        assert self.forecast_store.next_event(), "演示未来预告未进入倒计时"
        assert self.forecast_window.window.winfo_viewable(), "预告悬浮窗未显示"
        forecast_before = self.forecast_store
        unknown = demo_forecasts()
        for source in unknown["sources"]:
            for event in source["events"]:
                event.update(resetAt=None, timeKind="unknown", confidence="low")
        self.forecast_store = ForecastStore(unknown)
        self.update_floating()
        assert not self.forecast_store.next_event()
        assert "演示" in self.forecast_window.title.cget("text"), "未知时间预告未显示原公告"
        assert "未公布" in self.forecast_window.value.cget("text"), "未知时间显示了虚假倒数"
        self.forecast_store = forecast_before
        self.update_floating()
        self.smoke_forecast_trust()
        assert self.forecast_window.window.attributes("-topmost"), "预告悬浮窗置顶未生效"
        self.forecast_section.topmost_switch.invoke()
        assert not self.forecast_window.window.attributes("-topmost"), "预告取消置顶未生效"
        assert load_json(self.data_dir / "settings.json", {}).get("forecastTopmost") is False
        self.forecast_section.topmost_switch.invoke()
        self.forecast_section.floating_switch.invoke()
        assert self.forecast_window is None
        assert load_json(self.data_dir / "settings.json", {}).get("forecastFloating") is False
        self.forecast_section.floating_switch.invoke()
        self.forecast_section.enabled_switch.invoke()
        assert "已关闭" in self.forecast_window.status.cget("text")
        assert load_json(self.data_dir / "settings.json", {}).get("publicForecasts") is False
        self.forecast_section.enabled_switch.invoke()
        self.floating_enabled.set(True)
        self.floating_topmost.set(True)
        self.toggle_floating()
        self.root.update()
        assert self.floating_window.window.winfo_viewable(), "悬浮倒计时不可见"
        assert self.floating_window.window.attributes("-topmost"), "置顶开关未生效"
        self.topmost_switch.invoke()
        self.root.update()
        assert not self.floating_window.window.attributes("-topmost"), "取消置顶未生效"
        assert load_json(self.data_dir / "settings.json", {}).get("floatingTopmost") is False
        self.topmost_switch.invoke()
        self.floating_switch.invoke()
        self.root.update()
        assert self.floating_window is None, "关闭悬浮窗未生效"
        assert load_json(self.data_dir / "settings.json", {}).get("floatingCountdown") is False
        self.floating_switch.invoke()
        self.root.update()
        assert self.floating_window.window.winfo_viewable(), "重新开启悬浮窗未生效"
        assert load_json(self.data_dir / "settings.json", {}).get("floatingCountdown") is True
        self.smoke_postpone_before = parse_time(automatic_use_view(self.model)["triggerAt"])
        self.smoke_other_triggers = [item.get("nextAttemptAt") or item["triggerAt"] for item in self.demo_snapshot["cards"][1:]]
        self.smoke_live_before = copy.deepcopy(self.model.live)
        self.smoke_postpone_count = 0
        self.smoke_postpone_deadline = time.monotonic() + 15
        self.root.after(100, self.smoke_postpone)

    def smoke_forecast_trust(self):
        # A deliberately uncertain fixture exercises explicit user choice;
        # only the demo cache is touched, never actual forecasts or accounts.
        original = self.forecast_store
        fixture = demo_forecasts()
        fixture["sources"][1]["events"][0].update(estimateAt=(utcnow() + timedelta(minutes=11)).isoformat())
        self.forecast_store = ForecastStore(fixture)
        self.render()
        candidate = next(row for row in self.forecast_store.rows() if "demo-community" in row["sourceIds"])
        assert candidate["canTrust"] and not candidate["canCountdown"], "估算时间未保留手动选择入口"
        selected = next(key for key, row in self.forecast_section._events.items() if row.get("groupId") == candidate["groupId"])
        self.forecast_section.event_tree.selection_set(selected)
        self.forecast_section._selected()
        assert self.forecast_section.trust_button.cget("state") == "normal", "未启用采信按钮"
        self.forecast_section.trust_button.invoke()
        self.root.update_idletasks()
        trusted = self.forecast_store.next_event()
        assert trusted["manualTrusted"] and trusted["confidence"] == "low", "手动采信更改了证据等级"
        assert "手动采信" in self.forecast_window.label.cget("text"), "浮窗未标明手动采信"
        assert self.forecast_window.value.cget("text") not in ("暂无准确预告", "未公布准确时间"), "采信后未倒数"
        reloaded = ForecastStore(load_json(self.data_dir / "forecasts.json", {}))
        assert reloaded.trusted_event()["resetAt"] == trusted["resetAt"], "手动采信未持久保存"
        assert self.forecast_section.clear_trust_button.cget("state") == "normal", "无法取消手动采信"
        self.forecast_section.clear_trust_button.invoke()
        assert self.forecast_store.trusted_event() is None, "取消手动采信未生效"
        self.forecast_store = original
        self.save_forecasts()
        self.render()

    def smoke_postpone(self):
        self.drain()
        self.drain_controls()
        assert time.monotonic() < self.smoke_postpone_deadline, "延期测试未在时限内完成"
        if self.busy or self.postpone_busy:
            self.root.after(100, self.smoke_postpone)
            return
        self.render()
        view = automatic_use_view(self.model)
        self.root.update_idletasks()
        button = self.floating_window.postpone_button
        corner = self.floating_window.window
        assert button.winfo_ismapped() and button.winfo_rooty() + button.winfo_height() <= corner.winfo_rooty() + corner.winfo_height(), "延期按钮被裁切"
        assert clock_text(view["expiresAt"]) in self.floating_window.expiry.cget("text"), "未显示有效期到期时间"
        expected = self.smoke_postpone_before + timedelta(seconds=120 * self.smoke_postpone_count)
        assert parse_time(view["triggerAt"]) == expected, "延期未恰好增加 120 秒"
        assert self.model.live == self.smoke_live_before, "延期更改了在线快照"
        assert [item.get("nextAttemptAt") or item["triggerAt"] for item in self.demo_snapshot["cards"][1:]] == self.smoke_other_triggers, "延期影响了其他卡片"
        if self.smoke_postpone_count < 1:
            assert view["postponeEnabled"], "允许范围内延期按钮不可用"
            assert self.floating_window.postpone_button.cget("state") == "normal"
            self.floating_window.postpone_button.invoke()
            assert self.postpone_busy, "点击未启动后台延期"
            assert self.floating_window.postpone_button.cget("state") == "disabled", "处理中未禁止重复点击"
            self.smoke_postpone_count += 1
            self.root.after(100, self.smoke_postpone)
            return
        assert parse_time(view["triggerAt"]) <= parse_time(view["expiresAt"]) - timedelta(seconds=MIN_EXPIRY_RESERVE_SECONDS), "计划未保留 2 分钟"
        assert not view["postponeEnabled"], "下一次越过到期前 2 分钟仍可延期"
        assert self.floating_window.postpone_button.cget("state") == "disabled", "到达延期上限未禁用按钮"
        self.root.geometry("1000x730")
        self.root.after(200, self.smoke_small_window)

    def smoke_small_window(self):
        self.root.update_idletasks()
        assert self.footer.winfo_ismapped(), "最小尺寸下生命周期说明不可见"
        assert self.footer.winfo_rooty() + self.footer.winfo_height() <= self.root.winfo_rooty() + self.root.winfo_height()
        self.test_notice()
        self.root.update_idletasks()
        assert self.last_notice.winfo_exists()
        self.last_notice.destroy()
        self.root.iconify()
        self.root.after(250, self.smoke_minimized)

    def smoke_minimized(self):
        self.test_notice()
        self.root.update()
        assert self.root.state() == "iconic", self.root.state()
        assert self.last_notice.winfo_viewable(), "提醒弹窗在最小化后不可见"
        assert self.floating_window.window.winfo_viewable(), "主窗口最小化后倒计时不可见"
        assert self.forecast_window.window.winfo_viewable(), "主窗口最小化后预告倒计时不可见"
        corner = self.floating_window.window
        def overlap(first, second):
            return (first.winfo_rootx() < second.winfo_rootx() + second.winfo_width()
                    and second.winfo_rootx() < first.winfo_rootx() + first.winfo_width()
                    and first.winfo_rooty() < second.winfo_rooty() + second.winfo_height()
                    and second.winfo_rooty() < first.winfo_rooty() + first.winfo_height())
        assert not overlap(self.last_notice, corner), "提醒与悬浮窗重叠"
        forecast_corner = self.forecast_window.window
        assert forecast_corner.winfo_rooty() + forecast_corner.winfo_height() <= corner.winfo_rooty(), "两个倒计时悬浮窗重叠"
        assert not overlap(self.last_notice, forecast_corner), "提醒与预告倒计时重叠"
        from unittest.mock import patch
        area = work_area(self.root)
        short_area = (area[0], area[1], max(area[2], area[0] + 1000), area[1] + 680)
        with patch.object(sys.modules[__name__], "work_area", return_value=short_area), \
                patch("forecast_widgets.work_area", return_value=short_area), \
                patch("floating_countdown.work_area", return_value=short_area):
            self.update_floating()
            self.root.update()
            assert not overlap(self.last_notice, corner), "短屏提醒遮挡卡片浮窗"
            assert not overlap(self.last_notice, forecast_corner), "短屏提醒遮挡预告浮窗"
        self.update_floating()
        self.root.update()
        result = {"uiSmoke": "passed", "rows": 3, "notification": "created_and_closed",
                  "minimizedRoot": self.root.state(), "minimizedNotificationVisible": bool(self.last_notice.winfo_viewable()),
                  "notificationGeometry": self.last_notice.geometry(), "footerVisibleAtMinimumSize": True,
                  "floatingVisibleWhileMinimized": bool(corner.winfo_viewable()), "floatingTogglePersisted": True,
                  "topmostTogglePersisted": True, "notificationDoesNotOverlap": True,
                  "expiryTimeVisible": True, "postponeClickAddsSeconds": 120, "postponeClicks": 1,
                  "postponeLimitBeforeExpirySeconds": MIN_EXPIRY_RESERVE_SECONDS, "postponeLimitDisablesButton": True,
                  "otherCardSchedulesPreserved": True, "liveSnapshotPreserved": True, "network": False}
        result.update(publicForecastSources=len(self.forecast_section.source_tree.get_children()),
                      forecastCountdownVisibleWhileMinimized=True, forecastTogglePersisted=True,
                      forecastTopmostTogglePersisted=True, forecastCollectionTogglePersisted=True,
                      forecastWindowsDoNotOverlap=True, shortScreenNoticeDoesNotOverlap=True)
        result.update(uncertainForecastVisibleWithoutCountdown=True, configuredPublicSources=len(SOURCES), requestedSiteAdaptersBundled=True)
        result.update(manualForecastTrustStartsCountdown=True, manualForecastTrustPersisted=True,
                      manualForecastTrustKeepsEvidenceGrade=True, manualForecastTrustCanBeCancelled=True)
        frozen = bool(getattr(sys, "frozen", False))
        bundle = Path(getattr(sys, "_MEIPASS", ".")).resolve()
        tcl_library = Path(str(self.root.tk.call("info", "library"))).resolve()
        result.update(frozen=frozen, runtimeBundled=frozen and any(bundle.glob("python3*.dll")),
                      tkLibraryBundled=frozen and tcl_library.is_relative_to(bundle))
        self.last_notice.destroy()
        self.last_notice = None
        self.show_notice("公开重置活动预告", "演示活动提醒。", persistent=False)
        public_notice = self.last_notice
        self.show_notice("重置卡使用失败", "演示失败通知，不涉及真实卡片。", persistent=True)
        self.root.update()
        assert not public_notice.winfo_exists(), "公开预告阻止了重要使用失败提醒"
        assert self.last_notice_persistent and self.pending_critical
        assert load_json(self.data_dir / "session.json", {}).get("pendingNotices"), "重要提醒未保存，重启可能丢失"
        important = self.last_notice
        self.show_notice("普通到期提醒", "不应覆盖失败通知")
        assert self.last_notice is important, "重要结果被普通提醒覆盖"
        self.dismiss_notice()
        assert not self.pending_critical
        result.update(resultReminderNeedsAcknowledgement=True, importantReminderNotOverwritten=True,
                      cardFailurePreemptsPublicForecast=True)
        if self.smoke_report:
            self.smoke_report.parent.mkdir(parents=True, exist_ok=True)
            self.smoke_report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False))
        self.close()

    def close(self):
        if self.postpone_busy:
            self.control_message = "正在更新后台计划，请稍候再关闭面板"
            self.control_message_until = time.monotonic() + 12
            self.update_floating()
            return
        self.closed = True
        self.save_settings()
        self.save_session()
        self.save_forecasts()
        if self.floating_window is not None:
            self.floating_window.close()
            self.floating_window = None
        if self.forecast_window is not None:
            self.forecast_window.close()
            self.forecast_window = None
        self.root.destroy()


def acquire_single_instance(demo):
    if os.name != "nt":
        return None
    name = "Local\\CodexResetCardPanel-Demo" if demo else "Local\\CodexResetCardPanel"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, name)
    if ctypes.get_last_error() == 183:
        ctypes.WinDLL("user32").MessageBoxW(None, "面板已经在运行，请检查任务栏。", "Codex Reset Card", 0x40)
        kernel.CloseHandle(ctypes.c_void_p(handle))
        return False
    return handle


def main():
    parser = argparse.ArgumentParser(description="Codex Reset Card 中文只读面板")
    parser.add_argument("--demo", action="store_true", help="使用隔离的本地虚构数据")
    parser.add_argument("--smoke-test", action="store_true", help="运行实际 Tk UI 冒烟测试并退出（隐含 --demo）")
    parser.add_argument("--minimized", action="store_true", help="以最小化状态启动，提醒仍可用")
    parser.add_argument("--smoke-report", help="将演示窗口测试结果写入 JSON，适用于独立 EXE")
    args = parser.parse_args()
    demo = args.demo or args.smoke_test
    mutex = acquire_single_instance(demo)
    if mutex is False:
        return
    root = tk.Tk()
    smoke_errors = []
    if args.smoke_test:
        def fail_smoke(exc_type, exc_value, exc_traceback):
            import traceback
            smoke_errors.append(exc_value)
            traceback.print_exception(exc_type, exc_value, exc_traceback)
            root.destroy()
        root.report_callback_exception = fail_smoke
    Panel(root, demo=demo, smoke=args.smoke_test, minimized=args.minimized, smoke_report=args.smoke_report)
    root.mainloop()
    if mutex:
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(mutex))
    if smoke_errors:
        if args.smoke_report:
            Path(args.smoke_report).write_text(json.dumps({"uiSmoke": "failed", "error": str(smoke_errors[0])}, ensure_ascii=False), encoding="utf-8")
        raise RuntimeError("UI smoke test failed") from smoke_errors[0]


if __name__ == "__main__":
    main()

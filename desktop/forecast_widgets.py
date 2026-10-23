"""Read-only public forecast widgets. No collector, browser or card operations."""
from __future__ import annotations

from datetime import datetime
import re
import tkinter as tk
from tkinter import ttk

from floating_countdown import work_area
from panel_logic import clock_text, parse_time, remaining_text, safe_error, utcnow

BG, NAVY, TEAL = "#F0F5F8", "#102C40", "#008A81"
TEXT, MUTED, WHITE = "#16354B", "#526C7F", "#FFFFFF"
ORANGE, BORDER = "#A95C08", "#DDE8ED"
CONFIDENCE = {"high": "高证据", "medium": "中证据", "low": "低证据"}
INACTIVE = {"expired", "cancelled", "withdrawn", "historical", "archived", "retracted"}


def _text(value, fallback="", limit=1200):
    return (str(value).strip()[:limit] or fallback) if value is not None else fallback


def _time(value):
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    return parse_time(value)


def _excerpt(value, limit):
    text = " ".join(_text(value, limit=5000).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _error_detail(value):
    text = safe_error(value) if value else ""
    text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[已隐藏]@", text)
    return re.sub(r"(?i)(password|passwd|secret|api_key|token)[\s\"']*[:=][\s\"']*[^\s\",;&]+", r"\1=[已隐藏]", text)


def forecast_health_counts(sources, now=None):
    now = _time(now) or utcnow()
    sources = [source for source in sources if isinstance(source, dict)]
    failed = [source for source in sources if source.get("error")]
    waiting = sum(bool((retry := _time(source.get("nextAttemptAt"))) and retry > now) for source in failed)
    updated = sum(bool(source.get("lastSuccessAt") and not source.get("error") and not source.get("stale")) for source in sources)
    return {"updated": updated, "failed": len(failed), "waiting": waiting}


def confidence_view(item):
    """Evidence grades describe sourcing, never a probability of a reset."""
    item = item if isinstance(item, dict) else {}
    level = item.get("confidence", "low")
    level = level if isinstance(level, str) and level in CONFIDENCE else "low"
    reason = _text(item.get("confidenceReason"), "尚无充分可核对证据，请查看来源原文。")
    original, url = _text(item.get("originalUrl")), _text(item.get("url"))
    secondary = bool(item.get("isSecondary")) or bool(original and url and original != url)
    if secondary:
        if level == "high":
            level = "medium"
        reason += " 转述信息最多按中证据展示；以原帖为准。"
    return {"level": level, "label": CONFIDENCE[level], "reason": reason}


def forecast_event_view(event=None, now=None):
    """Build strings and an honest countdown without inferring a missing date."""
    now = _time(now) or utcnow()
    event = event if isinstance(event, dict) else {}
    grade = confidence_view(event)
    reset_at = _time(event.get("resetAt"))
    estimate_at = _time(event.get("estimateAt"))
    published_at = _time(event.get("publishedAt"))
    kind = _text(event.get("timeKind"), "unknown")
    event_kind = _text(event.get("eventKind"), "forecast")
    labels = {"exact": "预计重置", "deadline": "预告截止", "window": "预告窗口结束", "estimate": "来源估计"}
    if event_kind == "grant" and kind == "exact":
        labels["exact"] = "预告发放"
    time_text = labels.get(kind, "预告时间") + " " + clock_text(reset_at) if reset_at else "未公布准确时间"
    if reset_at is None and estimate_at is not None:
        time_text = "来源估计 " + clock_text(estimate_at) + "（待核实）"
    title = _text(event.get("title"), "暂无准确预告")
    summary = _excerpt(event.get("text"), 320)
    context = _excerpt(event.get("context"), 100)
    if title in {"Codex 公开重置活动", "Codex 公开活动", "公开重置活动", "Codex 重置活动", "重置卡发放活动"} and summary:
        title = _excerpt(event.get("text"), 80)
    result = {"title": title, "summary": summary, "context": context,
              "source": _text(event.get("sourceName"), "来源未提供"),
              "url": _text(event.get("originalUrl")) or _text(event.get("url")),
              "time": time_text, "published": clock_text(published_at) if published_at else "未提供",
              "confidence": grade["label"], "confidenceLevel": grade["level"], "reason": grade["reason"],
              "timeReason": _text(event.get("timeReason")),
              "countdown": "暂无准确预告" if not event else "未公布准确时间",
              "countdownLabel": "公开活动预告", "canCountdown": False,
              "status": "来源采集后有明确时间才显示倒计时", "stale": bool(event.get("stale"))}
    if not event:
        return result
    if reset_at is None and estimate_at is not None:
        result.update(status="来源估计待核实，不参与倒计时；不代表实际到账")
    if reset_at and kind in ("exact", "deadline", "window"):
        if reset_at <= now:
            result.update(countdown="预告时间已过", status="等待来源确认结果；不代表额度已到账")
        elif kind == "window":
            result.update(countdown="时间窗口预告", status="具体重置时刻未公布；不能推断到账时间")
        else:
            result.update(countdown="时间待确认", status="尚不符合倒计时条件，请核对来源")
    elif kind == "estimate" and reset_at:
        result.update(status="来源仅给出估计；不作为准确时间倒计时")
    elif not reset_at and event.get("timeReason"):
        result.update(status=_text(event.get("timeReason"), limit=58) + "；不代表实际到账")
    status = _text(event.get("status")).lower()
    # An explicit collector decision is authoritative. The fallback only accepts
    # a clearly typed exact/deadline time, not a publication date or guess.
    permitted = event.get("canCountdown") is True if "canCountdown" in event else kind in ("exact", "deadline")
    permitted = permitted and bool(reset_at and reset_at > now) and kind in ("exact", "deadline", "window")
    permitted = permitted and event_kind in ("forecast", "reset", "grant") and status not in INACTIVE
    if event.get("stale"):
        result.update(countdown="预告待重新确认", status="来源数据已陈旧；暂停倒计时")
        permitted = False
    if permitted:
        countdown_label = {"exact": "距预计重置", "deadline": "距预告截止", "window": "距预告窗口结束"}[kind]
        if event_kind == "grant" and kind == "exact":
            countdown_label = "距预告发放"
        result.update(canCountdown=True, countdown=remaining_text((reset_at - now).total_seconds()),
                      countdownLabel=countdown_label,
                      status="公开预告仅供提醒；实际到账以账号为准" if kind == "exact" else "截止/窗口倒计时不代表重置到账时间")
    return result


def forecast_source_view(source=None, events=(), now=None):
    source = source if isinstance(source, dict) else {}
    now = _time(now) or utcnow()
    source_id = _text(source.get("id") or source.get("sourceId"))
    name = _text(source.get("name") or source.get("sourceName"), "未命名来源")
    matching = []
    own_events = source.get("events")
    candidates = own_events if isinstance(own_events, (list, tuple)) else events
    for item in candidates:
        if not isinstance(item, dict):
            continue
        primary_match = isinstance(own_events, (list, tuple)) or bool(source_id and _text(item.get("sourceId")) == source_id)
        primary_match = primary_match or bool(not source_id and _text(item.get("sourceName")) == name)
        ids = item.get("sourceIds", [])
        extra_match = isinstance(ids, (list, tuple)) and source_id in ids
        # A merged event may retain a site's own report. Use that report's
        # time when supplied, rather than silently assigning the primary date.
        own_report = None
        reports = item.get("sources", [])
        for report in reports if isinstance(reports, (list, tuple)) else []:
            if isinstance(report, dict) and source_id and _text(report.get("sourceId") or report.get("id")) == source_id:
                own_report = dict(item, **report)
                if not primary_match and "resetAt" not in report:
                    own_report["resetAt"] = None
                if not primary_match and "estimateAt" not in report:
                    own_report["estimateAt"] = None
                break
            if isinstance(report, str) and report == source_id:
                extra_match = True
        if own_report is not None:
            matching.append(own_report)
        elif primary_match:
            matching.append(item)
        elif extra_match:
            # Shared identity is useful for counts, but without the site's own
            # text/time record it does not prove that site reported this date.
            matching.append(dict(item, resetAt=None, estimateAt=None))
    timed = [item for item in matching if _time(item.get("resetAt")) and item.get("timeKind") in ("exact", "deadline", "window")]
    future = [item for item in timed if _time(item["resetAt"]) > now]
    selected = source if _time(source.get("resetAt")) or _time(source.get("estimateAt")) else min(future, key=lambda item: _time(item["resetAt"])) if future else None
    if selected is None:
        estimates = [item for item in matching if _time(item.get("estimateAt"))]
        if estimates:
            selected = min(estimates, key=lambda item: abs((_time(item["estimateAt"]) - now).total_seconds()))
    if isinstance(source.get("nextEvent"), dict):
        selected = source["nextEvent"]
    grade = confidence_view(source if "confidence" in source or "confidenceReason" in source else selected)
    time_text = forecast_event_view(selected, now)["time"] if selected else "未公布准确时间"
    if source.get("timeText") and not (selected and not _time(selected.get("resetAt")) and _time(selected.get("estimateAt"))):
        time_text = _text(source["timeText"])
    last_at = _time(source.get("lastSuccessAt"))
    error = _error_detail(source.get("error"))[:200]
    retry_at = _time(source.get("nextAttemptAt"))
    retry_text = ("下次重试 " + clock_text(retry_at) + " 北京时间") if error and retry_at else ""
    error_detail = _error_detail(source.get("errorDetail"))
    stale = bool(source.get("stale"))
    if error:
        state = "采集失败 · " + error
        if retry_text:
            state += " · " + retry_text
    elif stale:
        state = "旧缓存 · 待重新采集"
    elif last_at:
        count = source.get("count")
        count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else len(matching)
        state = "已检查 · " + str(count) + " 条相关内容"
    else:
        state = "尚未采集"
    return {"name": name, "confidence": grade["label"], "reason": grade["reason"], "time": time_text,
            "timeReason": _text(source.get("timeReason")) or _text((selected or {}).get("timeReason")),
            "state": state, "last": clock_text(last_at) if last_at else "暂无成功采集",
            "url": _text(source.get("url")), "stale": stale,
            "errorDetail": error_detail, "retry": retry_text}


class ForecastSection:
    def __init__(self, parent, on_refresh, on_toggle, on_open_url, enabled_var, floating_var, topmost_var):
        self.on_open_url = on_open_url
        self.enabled_var = enabled_var
        self.frame = tk.Frame(parent, bg=WHITE, padx=14, pady=12, highlightbackground=BORDER, highlightthickness=1)
        header = tk.Frame(self.frame, bg=WHITE)
        header.pack(fill="x")
        tk.Label(header, text="公开重置预告", bg=WHITE, fg=TEXT,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        self.refresh_button = tk.Button(header, text="采集公开预告", command=on_refresh, bg=TEAL, fg=WHITE,
                                        activebackground="#00726B", activeforeground=WHITE, relief="flat",
                                        font=("Microsoft YaHei UI", 10), cursor="hand2", padx=12, pady=6)
        self.refresh_button.pack(side="right")
        switches = tk.Frame(self.frame, bg=WHITE)
        switches.pack(fill="x", pady=(7, 3))
        self.enabled_switch = tk.Checkbutton(switches, text="自动采集公开预告", variable=enabled_var, command=on_toggle,
                                            bg=WHITE, activebackground=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 10))
        self.floating_switch = tk.Checkbutton(switches, text="预告倒计时悬浮窗", variable=floating_var, command=on_toggle,
                                             bg=WHITE, activebackground=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 10))
        self.topmost_switch = tk.Checkbutton(switches, text="预告悬浮窗置顶", variable=topmost_var, command=on_toggle,
                                            bg=WHITE, activebackground=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 10))
        for widget in (self.enabled_switch, self.floating_switch, self.topmost_switch):
            widget.pack(side="left", padx=(0, 12))
        self.summary = tk.Label(self.frame, text="尚未采集公开来源", bg=WHITE, fg=MUTED, anchor="w",
                                font=("Microsoft YaHei UI", 9), justify="left")
        self.summary.pack(fill="x", pady=(0, 5))
        self.source_tree = self._table(("source", "confidence", "time", "checked"),
                                      ("来源网站", "证据可信度", "预计重置 / 截至时间（北京时间）", "最近成功采集与当前状态"),
                                      (170, 100, 265, 360), 4)
        self.source_reason = tk.Label(self.frame, text="选择来源网站查看该站的可信度依据。", bg="#F4F8FA", fg=MUTED,
                                      font=("Microsoft YaHei UI", 9), justify="left", anchor="w", wraplength=900, padx=9, pady=7)
        self.source_reason.pack(fill="x", pady=(6, 0))
        tk.Label(self.frame, text="预告与活动原文", bg=WHITE, fg=TEXT, anchor="w",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(fill="x", pady=(10, 5))
        self.event_tree = self._table(("title", "time", "confidence", "source"),
                                     ("标题", "预告时间（北京时间）", "证据可信度", "来源网站"), (320, 260, 100, 190), 4)
        self.reason = tk.Label(self.frame, text="选择一条预告查看证据依据及原帖链接。", bg="#F4F8FA", fg=MUTED,
                               font=("Microsoft YaHei UI", 9), justify="left", anchor="w", wraplength=900, padx=9, pady=7)
        self.reason.pack(fill="x", pady=(8, 4))
        links = tk.Frame(self.frame, bg=WHITE)
        links.pack(fill="x")
        self.open_event_button = tk.Button(links, text="查看原帖链接", command=self._open_event, state="disabled",
                                           bg=BG, fg=TEAL, relief="flat", font=("Microsoft YaHei UI", 9), padx=12, pady=5)
        self.open_event_button.pack(side="left")
        self.open_source_button = tk.Button(links, text="查看来源网站", command=self._open_source, state="disabled",
                                            bg=BG, fg=TEAL, relief="flat", font=("Microsoft YaHei UI", 9), padx=12, pady=5)
        self.open_source_button.pack(side="left", padx=8)
        tk.Label(self.frame, text="高 / 中 / 低表示证据强弱，非兑现概率。转述最多按中证据展示；未公布准确时间时不推测。",
                 bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 9), anchor="w", justify="left", wraplength=900).pack(fill="x", pady=(6, 0))
        self.event_tree.bind("<<TreeviewSelect>>", self._selected)
        self.source_tree.bind("<<TreeviewSelect>>", self._source_selected)
        self.event_tree.bind("<Double-Button-1>", lambda event: self._open_event())
        def fit_reasons(event):
            self.reason.configure(wraplength=max(250, event.width - 45))
            self.source_reason.configure(wraplength=max(250, event.width - 45))
        self.frame.bind("<Configure>", fit_reasons)
        self._events, self._sources, self._event_views, self._source_views = {}, {}, {}, {}

    def _table(self, columns, headings, widths, height):
        frame = tk.Frame(self.frame, bg=WHITE)
        frame.pack(fill="x")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=height, selectmode="browse")
        for key, label, width in zip(columns, headings, widths):
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=min(width, 80), anchor="w", stretch=True)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.grid_columnconfigure(0, weight=1)
        return tree

    @staticmethod
    def _rows(store, method):
        value = getattr(store, method, lambda: [])()
        return [item for item in value if isinstance(item, dict)] if isinstance(value, (list, tuple)) else []

    @staticmethod
    def _sync(tree, rows):
        desired = {key for key, _ in rows}
        for key in tree.get_children():
            if key not in desired:
                tree.delete(key)
        for index, (key, values) in enumerate(rows):
            if tree.exists(key):
                if tuple(tree.item(key, "values")) != tuple(str(value) for value in values):
                    tree.item(key, values=values)
                tree.move(key, "", index)
            else:
                tree.insert("", index, iid=key, values=values)
        if rows and not tree.selection():
            tree.selection_set(rows[0][0])

    def render(self, store, now=None, busy=False):
        now = _time(now) or utcnow()
        events, sources = self._rows(store, "rows"), self._rows(store, "source_rows")
        event_rows, source_rows = [], []
        self._events, self._sources, self._event_views, self._source_views = {}, {}, {}, {}
        for index, event in enumerate(events):
            key = "event:" + _text(event.get("id"), str(index))
            if key in self._events:
                key += ":" + str(index)
            self._events[key] = event
            view = self._event_views[key] = forecast_event_view(event, now)
            event_rows.append((key, (view["title"], view["time"], view["confidence"], view["source"])))
        for index, source in enumerate(sources):
            key = "source:" + _text(source.get("id") or source.get("sourceId"), str(index))
            if key in self._sources:
                key += ":" + str(index)
            self._sources[key] = source
            view = self._source_views[key] = forecast_source_view(source, events, now)
            source_rows.append((key, (view["name"], view["confidence"], view["time"], view["last"] + " · " + view["state"])))
        self._sync(self.source_tree, source_rows)
        self._sync(self.event_tree, event_rows)
        ready = sum(1 for event in events if forecast_event_view(event, now)["canCountdown"])
        health = forecast_health_counts(sources, now)
        paused = "采集与提醒已关闭 · " if not self.enabled_var.get() else ""
        self.summary.configure(text=paused + f"{len(sources)} 个公开来源 · 已更新 {health['updated']} / 采集失败 {health['failed']}（等待重试 {health['waiting']}） · {len(events)} 条相关内容 · {ready} 条有明确未来时间。来源列表可滚动；发布时间与预告时间分别显示。")
        self.refresh_button.configure(state="disabled" if busy else "normal", text="正在采集…" if busy else "采集公开预告")
        self._selected()
        self._source_selected()

    def _selected(self, event=None):
        selected = self.event_tree.selection()
        view = self._event_views.get(selected[0]) if selected else None
        if not view:
            self.reason.configure(text="暂无相关预告。没有明确时间时，预告悬浮窗不推测倒计时。")
            self.open_event_button.configure(state="disabled")
            return
        detail = f"{view['confidence']}：{view['reason']}\n原文发布时间：{view['published']}（北京时间）  ·  {view['time']}\n{view['status']}"
        if view.get("timeReason"):
            detail += "\n时间依据：" + view["timeReason"]
        if view.get("summary"):
            detail += "\n原文摘要：" + view["summary"]
        if view.get("context"):
            detail += "\n父帖摘要：" + view["context"]
        self.reason.configure(text=detail)
        self.open_event_button.configure(state="normal" if view["url"] else "disabled")

    def _source_selected(self, event=None):
        selected = self.source_tree.selection()
        source = self._sources.get(selected[0], {}) if selected else {}
        view = self._source_views.get(selected[0], {}) if selected else {}
        self.open_source_button.configure(state="normal" if source.get("url") else "disabled")
        if view:
            detail = f"{view['name']} · {view['confidence']}：{view['reason']}\n{view['time']} · {view['state']}"
            if view.get("timeReason"):
                detail += "\n时间依据：" + view["timeReason"]
            if view.get("errorDetail"):
                detail += "\n错误详情：" + view["errorDetail"]
            self.source_reason.configure(text=detail)
        else:
            self.source_reason.configure(text="尚无来源记录。采集后可逐站查看可信度依据及采集状态。")

    def _open_event(self):
        selected = self.event_tree.selection()
        view = self._event_views.get(selected[0], {}) if selected else {}
        if view.get("url"):
            self.on_open_url(view["url"])

    def _open_source(self):
        selected = self.source_tree.selection()
        source = self._sources.get(selected[0], {}) if selected else {}
        if source.get("url"):
            self.on_open_url(source["url"])


class ForecastFloating:
    WIDTH, HEIGHT, MARGIN = 360, 216, 16

    def __init__(self, root, on_hide, on_open, topmost=True):
        self.root = root
        self.window = tk.Toplevel(root)
        self.window.title("Reset Card 公开重置预告倒计时")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", bool(topmost))
        self.window.configure(bg="#3278A4", padx=1, pady=1)
        self.window.protocol("WM_DELETE_WINDOW", on_hide)
        body = tk.Frame(self.window, bg=NAVY, padx=14, pady=10)
        body.pack(fill="both", expand=True)
        header = tk.Frame(body, bg=NAVY)
        header.pack(fill="x")
        tk.Label(header, text="公开重置预告", bg=NAVY, fg="#8BCDF0",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        tk.Button(header, text="×", command=on_hide, bg=NAVY, fg="#B9D0DE", relief="flat", borderwidth=0,
                  cursor="hand2", font=("Segoe UI", 13), padx=4, pady=0).pack(side="right")
        self.label = tk.Label(body, text="公开活动预告", bg=NAVY, fg="#B9D0DE", anchor="w", font=("Microsoft YaHei UI", 9))
        self.label.pack(fill="x")
        self.value = tk.Label(body, text="暂无准确预告", bg=NAVY, fg=WHITE, anchor="w", font=("Microsoft YaHei UI", 20, "bold"))
        self.value.pack(fill="x", pady=(0, 2))
        self.title = tk.Label(body, bg=NAVY, fg=WHITE, anchor="w", font=("Microsoft YaHei UI", 9))
        self.title.pack(fill="x")
        self.source = tk.Label(body, bg=NAVY, fg="#8BCDF0", anchor="w", font=("Microsoft YaHei UI", 9))
        self.source.pack(fill="x", pady=(3, 0))
        self.time = tk.Label(body, bg=NAVY, fg="#B9D0DE", anchor="w", font=("Microsoft YaHei UI", 9))
        self.time.pack(fill="x", pady=(2, 0))
        self.status = tk.Label(body, bg=NAVY, fg="#FFD091", anchor="w", justify="left", wraplength=self.WIDTH - 30,
                               font=("Microsoft YaHei UI", 8))
        self.status.pack(fill="x", pady=(4, 0))
        for widget in (body, self.label, self.value, self.title, self.source, self.time, self.status):
            widget.bind("<Double-Button-1>", lambda event: on_open())
        self.position()
        self.render(None)

    def render(self, event_or_none, now=None, paused=False):
        view = forecast_event_view(event_or_none, now)
        if paused:
            view.update(countdown="预告提醒已关闭", countdownLabel="公开活动预告", status="采集与提醒已关闭；可在面板中重新开启")
        self.label.configure(text=view["countdownLabel"])
        self.value.configure(text=view["countdown"])
        self.title.configure(text=view["title"][:27] + ("…" if len(view["title"]) > 27 else ""))
        self.source.configure(text=view["source"][:22] + " · " + view["confidence"] if event_or_none else "双击打开面板查看已检查来源")
        self.time.configure(text=view["time"] + ("（北京时间）" if event_or_none else ""))
        self.status.configure(text=view["status"])

    def set_topmost(self, enabled):
        self.window.attributes("-topmost", bool(enabled))

    def position(self, bottom_offset=0):
        left, top, right, bottom = work_area(self.root)
        offset = max(0, int(bottom_offset or 0))
        x = max(left, right - self.WIDTH - self.MARGIN)
        y = max(top, bottom - self.HEIGHT - self.MARGIN - offset)
        self.window.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    def close(self):
        if self.window.winfo_exists():
            self.window.destroy()

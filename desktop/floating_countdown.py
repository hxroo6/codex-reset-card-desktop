"""Automatic-use countdown and an optional desktop corner window."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import timedelta
import os
import tkinter as tk

from panel_logic import clock_text, remaining_text, utcnow
from app_config import MIN_EXPIRY_RESERVE_SECONDS


def automatic_use_view(model, now=None):
    now = now or utcnow()
    view = {"value": "等待状态", "detail": "正在读取后台计划", "status": "状态待确认", "tone": "warning",
            "expiresAt": None, "postponeEnabled": False, "postponeReason": "尚无可确认的自动使用计划"}
    status = model.status
    cards = model.cards(now)
    if cards:
        view["expiresAt"] = cards[0]["expires"].isoformat()
    if status.get("armed") is False:
        return dict(view, value="已停用", detail="未授权自动使用卡片", status="自动使用未启用")
    service = status.get("service", {})
    if service.get("installed") is False:
        return dict(view, value="未安装", detail="尚未检测到自动使用服务")
    if service.get("running") is False:
        return dict(view, value="后台未运行", detail="后台恢复后才会尝试使用")
    if status.get("armed") is not True or service.get("running") is not True:
        return view
    if status.get("timingReady") is False:
        return dict(view, detail="本地计划尚未读取成功", status="计划待确认 · 暂不可推迟",
                    postponeReason="本地计划尚未读取成功，请等待状态刷新")
    planned = [card for card in cards if card["scheduled"] and card["trigger"] is not None
               and card["outcome"] not in ("reset", "alreadyRedeemed", "noCredit")]
    if not planned:
        source, data, _ = model.source()
        if model.stale(now):
            return dict(view, detail="暂无可确认的自动使用计划")
        if cards:
            if any(card["recorded"] for card in cards):
                return dict(view, value="暂无待执行计划", detail="卡片已有结束结果，等待后台更新", status="暂无已确认计划", tone="muted")
            return dict(view, value="等待后台发现", detail="新卡尚未纳入后台计划", status="暂无已确认计划")
        count = data.get("availableCount")
        if isinstance(count, int) and not isinstance(count, bool) and count == 0:
            return dict(view, value="暂无待用卡", detail="发现新卡后显示倒计时", status="暂无自动使用计划", tone="muted")
        return dict(view, value="等待计划详情", detail="尚无可确认的触发时间")
    card = min(planned, key=lambda item: item["trigger"])
    seconds = (card["trigger"] - now).total_seconds()
    local_old = not model.local_at or now - model.local_at > timedelta(seconds=90)
    stale = model.stale(now) or local_old
    view.update(value=remaining_text(seconds) if seconds > 0 else "等待后台结果",
                detail="计划 " + clock_text(card["trigger"]),
                status="上次计划 · 状态待确认" if stale else "距计划自动使用" if seconds > 0 else "已到使用时间 · 等待后台结果",
                tone="warning" if stale or seconds <= 0 else "normal",
                triggerAt=card["trigger"].isoformat(), fingerprint=card["fingerprint"],
                expiresAt=card["expires"].isoformat())
    if stale:
        reason = "状态待确认，刷新后可推迟"
    elif card.get("attemptInProgress"):
        reason = "后台正在处理此卡，暂不可推迟"
    elif card.get("outcome"):
        reason = "此卡已有执行结果，暂不可推迟"
    elif seconds <= 30:
        reason = "距离自动使用不足 30 秒，暂不可推迟"
    elif card["trigger"] + timedelta(minutes=2) > card["expires"] - timedelta(seconds=MIN_EXPIRY_RESERVE_SECONDS):
        reason = "再推迟将超过到期前 2 分钟"
    else:
        reason = ""
    view.update(postponeEnabled=not reason, postponeReason=reason)
    if seconds <= 0 and card["outcome"] == "nothingToReset":
        view.update(value="等待后台复查", status="状态待确认 · 重试时间未知" if stale else "当前无需重置 · 重试时间未知")
    return view


def work_area(root):
    """Use the desktop work area so neither window covers the taskbar."""
    if os.name == "nt":
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight() - 60


class FloatingCountdown:
    WIDTH, HEIGHT, MARGIN = 326, 225, 16

    def __init__(self, root, on_hide, on_open, topmost=True, on_postpone=None):
        self.root = root
        self.on_postpone = on_postpone
        self.window = tk.Toplevel(root)
        self.window.title("Reset Card 自动使用倒计时")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", bool(topmost))
        self.window.configure(bg="#008A81", padx=1, pady=1)
        self.window.protocol("WM_DELETE_WINDOW", on_hide)
        body = tk.Frame(self.window, bg="#102C40", padx=16, pady=10)
        body.pack(fill="both", expand=True)
        top = tk.Frame(body, bg="#102C40")
        top.pack(fill="x")
        title = tk.Label(top, text="自动使用倒计时", bg="#102C40", fg="#64DECA", font=("Microsoft YaHei UI", 10, "bold"))
        title.pack(side="left")
        tk.Button(top, text="×", command=on_hide, bg="#102C40", fg="#B9D0DE", relief="flat",
                  borderwidth=0, cursor="hand2", font=("Segoe UI", 13), padx=4, pady=0).pack(side="right")
        self.value = tk.Label(body, text="等待状态", bg="#102C40", fg="#FFFFFF", font=("Segoe UI", 23, "bold"), anchor="w")
        self.value.pack(fill="x", pady=(3, 1))
        self.detail = tk.Label(body, bg="#102C40", fg="#B9D0DE", font=("Microsoft YaHei UI", 9), anchor="w")
        self.detail.pack(fill="x")
        self.expiry = tk.Label(body, bg="#102C40", fg="#FFD091", font=("Microsoft YaHei UI", 9), anchor="w")
        self.expiry.pack(fill="x", pady=(2, 0))
        self.status = tk.Label(body, bg="#102C40", fg="#64DECA", font=("Microsoft YaHei UI", 9), anchor="w")
        self.status.pack(fill="x", pady=(3, 0))
        self.postpone_button = tk.Button(body, text="推迟 2 分钟再使用", command=on_postpone, state="disabled",
                                        bg="#008A81", fg="#FFFFFF", disabledforeground="#8DAAB8",
                                        activebackground="#00726B", activeforeground="#FFFFFF", relief="flat",
                                        font=("Microsoft YaHei UI", 10), cursor="hand2", pady=5)
        self.postpone_button.pack(fill="x", pady=(8, 0))
        for widget in (self.window, body, top, title, self.value, self.detail, self.expiry, self.status):
            widget.bind("<Double-Button-1>", lambda event: on_open())
        self.position()

    def position(self):
        left, top, right, bottom = work_area(self.root)
        x, y = max(left, right - self.WIDTH - self.MARGIN), max(top, bottom - self.HEIGHT - self.MARGIN)
        self.window.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    def render(self, view):
        self.value.configure(text=view["value"])
        self.detail.configure(text=view["detail"])
        self.expiry.configure(text="有效期到期 " + clock_text(view.get("expiresAt")))
        reason = view.get("postponeReason", "")
        status = view.get("controlMessage") or (reason if view.get("triggerAt") and reason else view["status"])
        self.status.configure(text=status[:31] + "…" if len(status) > 32 else status,
                              fg="#FFD091" if view["tone"] == "warning" or reason else "#64DECA")
        self.postpone_button.configure(text="正在推迟…" if view.get("postponeBusy") else "推迟 2 分钟再使用",
                                       state="normal" if self.on_postpone and view.get("postponeEnabled") else "disabled")
        self.position()

    def set_topmost(self, enabled):
        self.window.attributes("-topmost", bool(enabled))

    def close(self):
        if self.window.winfo_exists():
            self.window.destroy()

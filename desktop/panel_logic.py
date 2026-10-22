"""Read-only Codex Reset Card panel model. Python standard library only."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))
UTC = timezone.utc
OUTCOMES = {
    "reset": "已成功重置额度", "alreadyRedeemed": "此前已兑换（并非本次新重置）",
    "nothingToReset": "当前无需重置，原服务稍后再检查", "noCredit": "已无可用卡",
    "verificationFailed": "兑换前校验失败", "detailsUnavailable": "缺少卡片详情",
    "dryRun": "仅模拟，未兑换",
}


def utcnow():
    return datetime.now(UTC)


def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else None
    except ValueError:
        return None


def clock_text(value):
    dt = parse_time(value) if isinstance(value, str) else value
    return dt.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S") if dt else "未知"


def remaining_text(seconds):
    if seconds <= 0:
        return "已到期"
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (f"{days}天 " if days else "") + f"{hours:02}:{minutes:02}:{seconds:02}"


def safe_error(value):
    text = str(value)
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [已隐藏]", text)
    text = re.sub(r'(?i)(access_token|refresh_token|id_token|authorization)[\s\"\']*[:=][\s\"\']*[^\s\",;]+', r"\1=[已隐藏]", text)
    return text[:600]


def clean_status(value):
    """Only cache fields that status explicitly exposes; never credentials."""
    if not isinstance(value, dict):
        raise ValueError("状态格式不是 JSON 对象")
    result = {k: copy.deepcopy(value[k]) for k in (
        "armed", "leadTimeSeconds", "cacheReady", "lastDiscoveryAt", "nextDiscoveryAt",
        "availableCount", "detailsAvailable", "timingReady", "timingReadError") if k in value}
    service = value.get("service")
    result["service"] = {k: service[k] for k in ("installed", "running", "manager") if k in service} if isinstance(service, dict) else {}
    cards = value.get("cards")
    cards = cards if isinstance(cards, list) else []
    result["cards"] = [{k: card[k] for k in ("fingerprint", "expiresAt", "triggerAt", "lastOutcome", "nextAttemptAt", "attemptInProgress") if k in card}
                       for card in cards if isinstance(card, dict)]
    action = value.get("lastAction")
    if isinstance(action, dict):
        result["lastAction"] = {k: safe_error(action[k]) for k in ("at", "fingerprint", "kind", "outcome", "message") if k in action}
    live = value.get("live")
    if isinstance(live, dict):
        result["live"] = {k: live[k] for k in ("availableCount", "detailsAvailable", "fetchedAt") if k in live}
        credits = live.get("credits")
        credits = credits if isinstance(credits, list) else []
        result["live"]["credits"] = [{k: card[k] for k in ("fingerprint", "status", "expiresAt") if k in card}
                                           for card in credits if isinstance(card, dict)]
    return result


def default_binary():
    # runtimepaths.Default(): Windows os.UserConfigDir == APPDATA (Roaming).
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "codex-reset-card/bin/codex-reset-card.exe"


def child_proxy_env():
    """Go honors environment proxies, not Windows Internet Settings by default."""
    env = os.environ.copy()
    if env.get("HTTPS_PROXY") or env.get("https_proxy"):
        return env
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            raw = winreg.QueryValueEx(key, "ProxyServer")[0]
        if not enabled or not isinstance(raw, str) or not raw.strip():
            return env
        proxies = {}
        if "=" in raw:
            for part in raw.split(";"):
                if "=" in part:
                    scheme, address = part.split("=", 1)
                    proxies[scheme.strip().lower()] = address.strip()
        else:
            proxies = {"http": raw.strip(), "https": raw.strip()}
        for scheme in ("http", "https"):
            address = proxies.get(scheme) or proxies.get("http")
            if address and not env.get(scheme.upper() + "_PROXY") and not env.get(scheme + "_proxy"):
                env[scheme.upper() + "_PROXY"] = address if "://" in address else "http://" + address
    except (ImportError, OSError):
        pass
    return env


def read_status(refresh=False):
    binary = default_binary()
    if not binary.is_file():
        raise RuntimeError(f"未找到原工具：{binary}")
    args = [str(binary), "status"]
    if refresh:
        args.append("--refresh")
    args.append("--json")
    process = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=75, env=child_proxy_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if process.returncode:
        raise RuntimeError(safe_error(process.stderr.strip() or f"原工具退出码 {process.returncode}"))
    from schedule_control import add_schedule_timing
    return clean_status(add_schedule_timing(clean_status(json.loads(process.stdout))))


def load_json(path, default):
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else copy.deepcopy(default)
    except (OSError, ValueError):
        return copy.deepcopy(default)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


class PanelModel:
    def __init__(self, saved=None):
        saved = saved or {}
        self.status = saved.get("status", {})
        self.local_at = parse_time(saved.get("localAt"))
        self.live = saved.get("live")
        self.live_error = saved.get("liveError", "")
        self.local_error = ""
        self.loaded_from_disk = bool(self.status)

    def accept(self, status, now=None):
        self.status = clean_status(status)
        self.local_at = now or utcnow()
        self.local_error = ""
        self.loaded_from_disk = False
        if isinstance(status.get("live"), dict):
            self.live = clean_status(status)["live"]
            self.live_error = ""

    def saved(self):
        return {"status": self.status, "localAt": self.local_at.isoformat() if self.local_at else None,
                "live": self.live, "liveError": self.live_error}

    def source(self):
        # Prefer the freshest known snapshot. A newer daemon cache may supersede live.
        live_at = parse_time((self.live or {}).get("fetchedAt"))
        cached_at = parse_time(self.status.get("lastDiscoveryAt"))
        if self.live is not None and (not cached_at or (live_at and live_at >= cached_at)):
            return "在线只读快照", self.live, live_at
        if self.status.get("cacheReady") is True:
            return "原服务缓存", self.status, cached_at
        return "尚未成功查询", {}, None

    def stale(self, now=None):
        now = now or utcnow()
        source, _, at = self.source()
        age_limit = 600 if source == "在线只读快照" else 86400
        return bool(self.loaded_from_disk or self.local_error or self.live_error or not at or (now - at).total_seconds() > age_limit)

    def cards(self, now=None):
        now = now or utcnow()
        source, data, _ = self.source()
        if data.get("detailsAvailable") is not True:
            return []
        lead = self.status.get("leadTimeSeconds")
        lead = lead if isinstance(lead, (float, int)) and not isinstance(lead, bool) else None
        cached = {x.get("fingerprint"): x for x in self.status.get("cards", [])}
        result = []
        for card in data.get("credits" if source == "在线只读快照" else "cards", []):
            if source == "在线只读快照" and card.get("status") != "available":
                continue
            expires = parse_time(card.get("expiresAt"))
            if not expires or expires <= now:
                continue
            fingerprint = str(card.get("fingerprint", "未知"))
            original = cached.get(fingerprint, {})
            # Live expiry may have changed while daemon cache is old.
            # Never attach the old cache trigger to a newly observed expiry.
            trigger = expires - timedelta(seconds=lead) if lead is not None else None
            recorded = bool(original) and parse_time(original.get("expiresAt")) == expires and parse_time(original.get("triggerAt")) == trigger
            next_attempt = parse_time(original.get("nextAttemptAt")) if recorded else None
            if trigger is not None and next_attempt is not None:
                trigger = max(trigger, next_attempt)
            scheduled = recorded and self.status.get("timingReady", True) is not False and self.status.get("armed") is True and self.status.get("service", {}).get("running") is True
            result.append({"fingerprint": fingerprint, "expires": expires, "trigger": trigger,
                           "recorded": recorded, "scheduled": scheduled,
                           "attemptInProgress": bool(original.get("attemptInProgress")),
                           "outcome": original.get("lastOutcome", "") if recorded else ""})
        return sorted(result, key=lambda c: c["expires"])


class ReminderEngine:
    def __init__(self, saved=None):
        self.state = saved or {"seen": {}, "service": "", "error": ""}
        self.state.setdefault("seen", {})

    def evaluate(self, model, now=None):
        now = now or utcnow()
        notes = []
        seen = self.state["seen"]
        # A stale snapshot cannot justify a fresh expiry alert.
        if not model.stale(now):
            for card in model.cards(now):
                remaining = (card["expires"] - now).total_seconds()
                due = [t for t in (86400, 3600, 600) if remaining <= t]
                if not due:
                    continue
                base = f"expiry:{card['fingerprint']}:{card['expires'].astimezone(UTC).isoformat()}:"
                unseen = [t for t in due if base + str(t) not in seen]
                if unseen:
                    for t in due:
                        seen[base + str(t)] = now.isoformat()
                    threshold = min(unseen)
                    label = {86400: "24 小时", 3600: "1 小时", 600: "10 分钟"}[threshold]
                    notes.append(("卡片到期提醒", f"卡 {card['fingerprint']} 已进入最后 {label}；距到期 {remaining_text(remaining)}。"))
        service = model.status.get("service", {})
        signature = "running" if service.get("running") is True else ("stopped" if service.get("installed") is True else "missing" if service.get("installed") is False else "unknown")
        if not model.local_error and not model.loaded_from_disk and signature != "unknown":
            if signature != "running" and signature != self.state.get("service"):
                notes.append(("原服务异常", "自动兑换服务未运行，请在原工具中检查。" if signature == "stopped" else "尚未检测到原工具的后台服务。"))
            self.state["service"] = signature
        error = model.live_error or model.local_error
        digest = hashlib.sha256(error.encode()).hexdigest() if error else ""
        if error and digest != self.state.get("error"):
            notes.append(("读取状态失败", error + "；保留上次成功快照并标记陈旧。"))
        self.state["error"] = digest
        action = model.status.get("lastAction") or {}
        if action.get("kind") in ("consume", "verification"):
            key = "action:" + json.dumps(action, sort_keys=True, ensure_ascii=False)
            at = parse_time(action.get("at"))
            if key not in seen:
                seen[key] = now.isoformat()
                if at and timedelta(0) <= now - at <= timedelta(hours=24):
                    outcome = action.get("outcome") or action.get("message") or "结果未知"
                    notes.append(("兑换结果" if action.get("kind") == "consume" else "兑换前检查", OUTCOMES.get(outcome, outcome)))
        # Keep finite state, retaining 180 days of dedupe history.
        self.state["seen"] = {k: v for k, v in seen.items() if (parse_time(v) or now) >= now - timedelta(days=180)}
        return notes


def demo_status(now=None):
    now = now or utcnow()
    expires = [now + timedelta(minutes=9, seconds=35), now + timedelta(hours=20), now + timedelta(days=3, hours=6)]
    cards = [{"fingerprint": fp, "expiresAt": at.isoformat(), "triggerAt": (at - timedelta(minutes=5)).isoformat()}
             for fp, at in zip(("DEMO-A7831", "DEMO-B9512", "DEMO-C6408"), expires)]
    return {"armed": True, "leadTimeSeconds": 300, "service": {"installed": True, "running": True, "manager": "Task Scheduler"},
            "cacheReady": True, "lastDiscoveryAt": (now - timedelta(minutes=15)).isoformat(),
            "nextDiscoveryAt": (now + timedelta(hours=23, minutes=45)).isoformat(),
            "availableCount": 3, "detailsAvailable": True, "cards": cards,
            "lastAction": {"at": (now - timedelta(minutes=15)).isoformat(), "kind": "discovery", "message": "查询成功，可用卡=3"},
            "live": {"availableCount": 3, "detailsAvailable": True, "fetchedAt": now.isoformat(),
                     "credits": [{"fingerprint": c["fingerprint"], "expiresAt": c["expiresAt"], "status": "available"} for c in cards]}}

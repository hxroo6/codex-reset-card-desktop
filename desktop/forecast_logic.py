"""Persistent display, grouping and conservative reminders for public forecasts."""
from __future__ import annotations

import copy
import hashlib
from datetime import timedelta

from forecast_sources import BEIJING, SOURCES, canonical_url, iso, parse_time, utcnow

# Two hours allows brief network/proxy interruptions but prevents an old copied
# announcement from indefinitely triggering fresh activity alerts.
STALE_AFTER_SECONDS = 2 * 3600
CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}
TIME_LABELS = {"exact": "预告时间", "deadline": "预告截止", "window": "时间窗口待核实", "unknown": "时间未明确", "estimate": "站点估算"}
TRUST_INACTIVE = {"completed", "delivered", "cancelled", "withdrawn", "expired", "historical", "archived", "retracted", "recorded"}


def _trust_candidate(row, now):
    """An explicit user choice may use an estimate, never invent its time."""
    if row.get("trustBlockedStatus") or row.get("status") in TRUST_INACTIVE:
        return None, "此记录已完成、取消、到期或属于历史记录，不能开启手动倒计时"
    times = [parse_time(row.get(key)) for key in ("resetAt", "estimateAt")]
    candidate = next((stamp for stamp in times if stamp and stamp > now), None)
    if candidate is None:
        reason = "记录中的具体时间已过，不能开启手动倒计时" if any(times) else "没有具体时间，不能开启手动倒计时"
        return None, reason
    reason = "手动采信仅锁定所选时间，不提高可信度，也不代表个人账户已重置"
    if row.get("stale"):
        reason = "缓存陈旧；" + reason
    return candidate, reason


def _stale(source, now):
    last = parse_time(source.get("lastSuccessAt"))
    snapshot = parse_time(source.get("snapshotAt"))
    return (not last or bool(source.get("error")) or source.get("upstreamStale") is True
            or (now - last).total_seconds() > STALE_AFTER_SECONDS
            or (snapshot is not None and (now - snapshot).total_seconds() > STALE_AFTER_SECONDS))


class ForecastStore:
    def __init__(self, data=None):
        self.data = copy.deepcopy(data) if isinstance(data, dict) else {}
        self.data.setdefault("schema", 1)
        if not isinstance(self.data.get("sources"), list):
            self.data["sources"] = []
        if not isinstance(self.data.get("seen"), dict):
            self.data["seen"] = {}

    def update(self, result, now=None):
        if not isinstance(result, dict) or not isinstance(result.get("sources"), list):
            raise ValueError("公开预告结果格式无效")
        now = now or utcnow()
        old = {s.get("id"): s for s in self.data["sources"] if isinstance(s, dict)}
        for source in result["sources"]:
            if not isinstance(source, dict) or not source.get("id"):
                continue
            previous = old.get(source["id"], {})
            merged = {**copy.deepcopy(previous), **copy.deepcopy(source)}
            # Also tolerate a caller reporting only failure metadata.
            if source.get("error") and not source.get("events") and previous.get("events"):
                merged["events"] = copy.deepcopy(previous["events"])
            old[source["id"]] = merged
        self.data.update(schema=1, fetchedAt=result.get("fetchedAt") or iso(now), sources=list(old.values()))
        # Persist an explicit end/cancellation while it is fresh. A subsequent
        # network failure must not silently resume a paused manual selection.
        self.trusted_event(now)

    def saved(self):
        return copy.deepcopy(self.data)

    def source_rows(self, now=None):
        now = now or utcnow()
        saved_sources = {s.get("id"): s for s in self.data["sources"] if isinstance(s, dict) and s.get("id")}
        # An upgraded installation must show newly configured sites before the
        # first fetch. Fictional demos remain isolated from the real source list.
        demo = any(str(key).startswith("demo-") for key in saved_sources)
        defaults = {} if demo else {s["id"]: s for s in SOURCES}
        defaults.update(saved_sources)
        rows = []
        for key, source in defaults.items():
            events = [e for e in source.get("events", []) if isinstance(e, dict)]
            stale = _stale(source, now)
            def priority(event):
                reset = parse_time(event.get("resetAt"))
                upcoming = event.get("status") in ("upcoming", "scheduled")
                if upcoming and reset and reset > now and event.get("timeKind") in ("exact", "deadline"):
                    return (0, reset.timestamp())
                return (1 if upcoming else 2, -(parse_time(event.get("publishedAt")) or now.replace(year=2000)).timestamp())
            ordered = sorted(events, key=priority)
            site_event = ordered[0] if ordered else {}
            confidence = "medium" if site_event.get("confidence") in ("medium", "high") else "low"
            reset = parse_time(site_event.get("resetAt"))
            confidence_reason = site_event.get("confidenceReason") or "社区网站转述，未直接核实官方原帖；相同原帖的镜像不增加可信度"
            if source.get("sourceGroupReason"):
                confidence_reason += "；采集链路：" + source["sourceGroupReason"]
            if source.get("freshnessNote"):
                confidence_reason += "；" + source["freshnessNote"]
            rows.append({"id": key, "sourceId": key, "name": source.get("name", key), "sourceName": source.get("name", key),
                         "url": source.get("url", ""), "requestUrl": source.get("requestUrl", ""), "lastSuccessAt": source.get("lastSuccessAt"),
                         "fetchedAt": source.get("fetchedAt"), "nextAttemptAt": source.get("nextAttemptAt"), "error": source.get("error", ""),
                         "failureKind": source.get("failureKind"), "httpStatus": source.get("httpStatus"),
                         "errorDetail": source.get("errorDetail", ""), "lastManualAttemptAt": source.get("lastManualAttemptAt"),
                         "stale": stale, "confidence": confidence, "confidenceLabel": CONFIDENCE_LABELS[confidence], "count": len(events),
                         "status": "读取失败·缓存陈旧" if source.get("error") else "尚未采集" if not source.get("lastSuccessAt") else "缓存陈旧" if stale else "已更新",
                         "confidenceReason": confidence_reason,
                         "sourceGroup": source.get("sourceGroup"), "sourceGroupReason": source.get("sourceGroupReason", ""),
                         "events": copy.deepcopy(events), "resetAt": site_event.get("resetAt"), "timeKind": site_event.get("timeKind", "unknown"),
                         "timeText": reset.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S 北京时间") if reset else "时间待核实",
                         "timeReason": site_event.get("timeReason", ""), "title": site_event.get("title", ""),
                         "originalUrl": site_event.get("originalUrl", ""), "estimateAt": site_event.get("estimateAt"),
                         "claimedTimeText": site_event.get("claimedTimeText", ""), "eventStatus": site_event.get("status", "unknown")})
        return rows

    def rows(self, now=None):
        now = now or utcnow()
        grouped = {}
        for source in self.data["sources"]:
            if not isinstance(source, dict):
                continue
            for event in source.get("events", [])[:100]:
                if not isinstance(event, dict):
                    continue
                original = canonical_url(event.get("originalUrl"))
                # Without a primary URL a source's local IDs are not globally
                # meaningful. Do not accidentally merge unrelated tracker rows.
                key = original or str(source.get("id")) + ":" + str(event.get("id"))
                grouped.setdefault(key, []).append((source, event))
        rows = []
        for key, copies in grouped.items():
            current = [(s, e) for s, e in copies if not _stale(s, now)]
            candidates = current or copies
            # Prefer a clear stated time, then a recent publication. This is a
            # display choice only: mirrors never elevate the confidence rating.
            candidates = sorted(candidates, key=lambda pair: (pair[1].get("timeKind") in ("exact", "deadline"), pair[1].get("publishedAt") or ""), reverse=True)
            source, event = candidates[0]
            row = copy.deepcopy(event)
            row["groupId"] = hashlib.sha256(key.encode()).hexdigest()[:24]
            row["sourceIds"] = [s.get("id") for s, _ in copies]
            row["sourceNames"] = [s.get("name", "") for s, _ in copies]
            row["sourceName"] = " / ".join(dict.fromkeys(row["sourceNames"]))
            row["sources"] = [{"id": s.get("id"), "name": s.get("name"), "url": s.get("url"), "stale": _stale(s, now)} for s, _ in copies]
            row["stale"] = not bool(current)
            times = {iso(parse_time(e.get("resetAt"))) for _, e in current if parse_time(e.get("resetAt")) and e.get("timeKind") in ("exact", "deadline")}
            cancelled = any(e.get("status") in ("cancelled", "withdrawn") for _, e in current)
            completed = any(e.get("status") in ("completed", "delivered") for _, e in current)
            if cancelled:
                row.update(status="cancelled", resetAt=None, confidence="low", timeReason="有来源报告取消，停止活动倒计时和自动提醒")
            elif completed:
                row.update(status="completed", resetAt=None, timeReason="有来源报告活动已完成，停止预告倒计时；未确认个人到账")
            elif len(times) > 1:
                row.update(resetAt=None, timeKind="unknown", confidence="low", confidenceReason="同一原帖被换算为不同时间，未核实前不倒计时", timeReason="来源时间冲突")
            elif any(e.get("timeKind") == "window" and "歧义" in e.get("timeReason", "") for _, e in current):
                row.update(resetAt=None, timeKind="window", confidence="low", timeReason="原帖时区存在歧义，多个镜像不能消除歧义")
            # Every current configured source is a community mirror. Saved data
            # or upstream 'high' claims cannot turn that into official proof.
            if row.get("confidence") not in ("medium", "low"):
                row["confidence"] = "medium"
            reset = parse_time(row.get("resetAt"))
            row["reset"] = reset
            row["canCountdown"] = bool(not row["stale"] and not cancelled and reset and row.get("timeKind") in ("exact", "deadline")
                                       and row.get("confidence") in ("medium", "high") and row.get("status") in ("upcoming", "scheduled")
                                       and row.get("eventKind") in ("forecast", "reset", "grant") and reset > now)
            row["confidenceLabel"] = CONFIDENCE_LABELS[row["confidence"]]
            row["timeLabel"] = TIME_LABELS.get(row.get("timeKind"), "时间未明确")
            row["timeText"] = reset.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S 北京时间") if reset else "时间待核实"
            row["label"] = row["timeLabel"]
            row["remainingSeconds"] = (reset - now).total_seconds() if reset else None
            terminal = next((e.get("status") for _, e in current if e.get("status") in TRUST_INACTIVE), None)
            row["trustBlockedStatus"] = terminal or (row.get("status") if row.get("status") in TRUST_INACTIVE else None)
            candidate, reason = _trust_candidate(row, now)
            trusted = self.data.get("trustedForecast")
            row.update(canTrust=candidate is not None, trustReason=reason, manualCandidateAt=iso(candidate),
                       manualTrusted=isinstance(trusted, dict) and trusted.get("groupId") == row["groupId"])
            rows.append(row)
        return sorted(rows, key=lambda r: (0 if r["canCountdown"] else 1, r.get("resetAt") if r["canCountdown"] else "", -(parse_time(r.get("publishedAt")) or now.replace(year=2000)).timestamp()))

    def trust(self, group_id, now=None):
        """Lock one displayed forecast's concrete time until explicitly changed."""
        now = now or utcnow()
        row = next((item for item in self.rows(now) if item["groupId"] == group_id), None)
        if row is None:
            raise ValueError("所选预告已不在列表中，请重新选择")
        if not row["canTrust"]:
            raise ValueError(row["trustReason"])
        fields = ("id", "title", "url", "originalUrl", "publishedAt", "resetAt", "estimateAt", "timeKind",
                  "eventKind", "status", "confidence", "confidenceLabel", "confidenceReason", "timeReason",
                  "sourceName", "sourceNames", "sourceIds", "sources", "claimedTimeText")
        snapshot = {key: copy.deepcopy(row[key]) for key in fields if key in row}
        self.data["trustedForecast"] = {"groupId": group_id, "targetAt": row["manualCandidateAt"],
                                        "selectedAt": iso(now), "event": snapshot}
        return self.trusted_event(now)

    def clear_trust(self):
        self.data.pop("trustedForecast", None)

    def trusted_event(self, now=None):
        """Return a locked choice, including its ended/paused display state."""
        now = now or utcnow()
        trusted = self.data.get("trustedForecast")
        if not isinstance(trusted, dict) or not isinstance(trusted.get("groupId"), str):
            return None
        target = parse_time(trusted.get("targetAt"))
        if target is None or not isinstance(trusted.get("event"), dict):
            return None
        current = next((item for item in self.rows(now) if item["groupId"] == trusted["groupId"]), None)
        terminal = current.get("trustBlockedStatus") if current is not None and not current["stale"] else None
        if terminal and not trusted.get("pausedAt"):
            trusted.update(pausedAt=iso(now), terminalStatus=terminal,
                           pauseReason="来源明确报告活动已完成、取消或归入历史，手动倒计时已暂停")
        paused = bool(trusted.get("pausedAt"))
        ended = target <= now
        event = copy.deepcopy(trusted["event"])
        event.update(groupId=trusted["groupId"], manualTrusted=True, resetAt=iso(target), reset=target,
                     selectedAt=trusted.get("selectedAt"), remainingSeconds=(target-now).total_seconds(),
                     timeText=target.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S 北京时间"),
                     canCountdown=not paused and not ended, trustEnded=ended, trustPaused=paused,
                     stale=current["stale"] if current is not None else True,
                     trustReason=trusted.get("pauseReason") if paused else "手动采信并锁定的时间；刷新不会自动改期")
        if paused:
            event["status"] = trusted.get("terminalStatus", "cancelled")
        if event["stale"]:
            event["trustReason"] += "；当前缓存陈旧，继续按用户锁定时间显示"
        event["confidenceLabel"] = CONFIDENCE_LABELS.get(event.get("confidence"), "低")
        return event

    def next_event(self, now=None):
        trusted = self.trusted_event(now)
        if trusted is not None:
            return trusted
        return next((row for row in self.rows(now) if row["canCountdown"]), None)

    def reminders(self, now=None):
        now = now or utcnow()
        seen = self.data["seen"]
        notes = []
        trusted = self.trusted_event(now)
        if trusted is not None and not trusted["trustPaused"]:
            remaining = trusted["remainingSeconds"]
            base = "manual-forecast:" + trusted["groupId"] + ":" + trusted["resetAt"] + ":"
            due = [threshold for threshold in (3600, 600, 120, 0) if remaining <= threshold]
            unseen = [threshold for threshold in due if base + str(threshold) not in seen]
            for threshold in due:
                seen[base + str(threshold)] = iso(now)
            if remaining >= -60 and unseen:
                threshold = min(unseen)
                label = {3600: "1 小时内", 600: "10 分钟内", 120: "2 分钟内", 0: "已到采信时间"}[threshold]
                title = "手动采信预告到点" if threshold == 0 else "手动采信重置预告"
                stale = "；当前缓存陈旧" if trusted["stale"] else ""
                notes.append((title, f"{trusted.get('title', 'Codex 公开活动')} · {label}\n手动采信时间：{trusted['timeText']}\n来源：{trusted.get('sourceName', '未知来源')} · 原可信度{trusted['confidenceLabel']}{stale}。手动采信不代表个人账户已到账。"))
        for row in self.rows(now):
            if trusted is not None and row["groupId"] == trusted["groupId"]:
                continue  # One selected event must not produce two notifications.
            reset = row.get("reset")
            if (row["stale"] or reset is None or row.get("timeKind") not in ("exact", "deadline")
                    or row.get("confidence") not in ("medium", "high") or row.get("status") not in ("upcoming", "scheduled")
                    or row.get("eventKind") not in ("forecast", "reset", "grant")):
                continue
            remaining = (reset - now).total_seconds()
            # A due notification is allowed only within one minute of the stated
            # time. Restarting hours later must not replay old milestones.
            base = "forecast:" + row["groupId"] + ":" + iso(reset) + ":"
            due = [t for t in (3600, 600, 120, 0) if remaining <= t]
            if not due:
                continue
            unseen = [t for t in due if base + str(t) not in seen]
            for threshold in due:
                seen[base + str(threshold)] = iso(now)
            if remaining < -60 or not unseen:
                continue
            threshold = min(unseen)
            label = {3600: "1 小时内", 600: "10 分钟内", 120: "2 分钟内", 0: "已到预告时间"}[threshold]
            title = "公开重置活动预告" if threshold else "公开活动预告到点"
            wording = "原帖预告的最迟截止" if row["timeKind"] == "deadline" else "预计重置时间"
            notes.append((title, f"{row.get('title', 'Codex 公开活动')} · {label}\n{wording}：{row['timeText']}\n来源：{row['sourceName']} · 可信度{row['confidenceLabel']}。预告不代表个人账户已到账。"))
        self.data["seen"] = {key: stamp for key, stamp in seen.items() if (parse_time(stamp) or now) >= now - timedelta(days=30)}
        return notes


def demo_forecasts(now=None):
    """Fictional-only fixtures; the .invalid links are never fetched."""
    now = now or utcnow()
    future = now + timedelta(minutes=9, seconds=30)
    sources = []
    definitions = (("demo-official-mirror", "示例预告站 A", "deadline", iso(future), "upcoming", "medium", now),
                   ("demo-community", "示例预告站 B", "unknown", None, "upcoming", "low", now),
                   ("demo-stale", "示例缓存站 C", "exact", iso(now + timedelta(minutes=5)), "upcoming", "medium", now - timedelta(hours=3)))
    for index, (key, name, kind, reset, status, confidence, success) in enumerate(definitions):
        url = f"https://source-{index + 1}.example.invalid/"
        event = {"id": f"DEMO-EVENT-{index + 1}", "title": "演示：公开活动预告（虚构）", "sourceId": key, "sourceName": name,
                 "url": url, "originalUrl": url + "demo-announcement", "publishedAt": iso(now - timedelta(minutes=20)), "resetAt": reset,
                 "timeKind": kind, "eventKind": "forecast", "status": status, "confidence": confidence,
                 "confidenceReason": "演示数据，不能视为真实活动；镜像不提高可信度", "timeReason": "虚构演示；未知时间不补造时刻",
                 "text": "仅展示公开活动倒计时、未知时间与陈旧缓存的界面，不访问真实账户或公开网站。"}
        sources.append({"id": key, "name": name, "url": url, "requestUrl": url, "fetchedAt": iso(success), "lastSuccessAt": iso(success),
                        "nextAttemptAt": iso(now + timedelta(minutes=15)), "error": "", "events": [event]})
    return {"schema": 1, "fetchedAt": iso(now), "sources": sources}

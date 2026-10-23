"""Read-only public reset announcements. No account or reset-card access.

Each source endpoint is polled at most every 15 minutes. The sources are
community trackers, often mirroring the same original post; neither agreement
nor a site's own confidence label is evidence of independent verification.
"""
from __future__ import annotations

import copy
import hashlib
import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))
FETCH_TIMEOUT_SECONDS = 12
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
POLL_SECONDS = 15 * 60
MAX_EVENTS_PER_SOURCE = 100
SOURCES = (
    {"id": "codex-radar", "name": "Codex Reset Radar", "url": "https://codex-reset.com/",
     "requestUrl": "https://codex-reset.com/api/feed", "adapter": "radar"},
    {"id": "codex-resets", "name": "Codex Resets", "url": "https://codex-resets.com/",
     "requestUrl": "https://codex-resets.com/api/v1/status", "adapter": "resets"},
    {"id": "whenreset", "name": "WhenReset", "url": "https://whenreset.app/",
     "requestUrl": "https://whenreset.app/api/resets.json", "adapter": "whenreset"},
    {"id": "skill2web", "name": "Skill2Web", "url": "https://reset.skill2web.com/zh/codex/",
     "requestUrl": "https://reset.skill2web.com/zh/codex/", "adapter": "skill2web"},
    {"id": "codexreset-dev", "name": "CodexReset.dev", "url": "https://codexreset.dev/",
     "requestUrl": "https://codexreset.dev/api/status?lang=en", "adapter": "codexreset-dev",
     "sourceGroup": "codex-resets", "sourceGroupReason": "部分记录转存 Codex Resets；历史归档不代表新增预告或独立验证"},
    {"id": "tibo-watch", "name": "Tibo Watch", "url": "https://tibo.watch/",
     "requestUrl": "https://tibo.watch/api/signals", "adapter": "tibo-watch",
     "sourceGroup": "community-aggregate", "sourceGroupReason": "聚合 Codex Resets、When Next Reset 等公共记录；同一原帖不重复加分"},
    {"id": "whennextreset", "name": "When Next Reset", "url": "https://whennextreset.com/",
     "requestUrl": "https://whennextreset.com/feed.xml", "adapter": "whennextreset",
     "sourceGroup": "codex-resets", "sourceGroupReason": "RSS 为 Codex Resets 历史转述；没有原帖链接时保持低可信度"},
    {"id": "codex-reset-app", "name": "Codex Reset App", "url": "https://codex-reset.app/",
     "requestUrl": "https://codex-reset.app/", "adapter": "codex-reset-app",
     "sourceGroup": "codex-resets", "sourceGroupReason": "公开页面转述上游公告；主页推算时点不作为原文准确时间"},
    {"id": "codexclock", "name": "CodexClock", "url": "https://codexclock.com/",
     "requestUrl": "https://codexclock.com/assets/content.json", "adapter": "codexclock",
     "sourceGroup": "public-post-archive", "sourceGroupReason": "静态整理的原帖与历史记录；以数据更新时间判断陈旧，不把历史模型概率当预告"},
    {"id": "whenreset-cn", "name": "WhenReset 中国", "url": "https://whenreset.com.cn/",
     "requestUrl": "https://whenreset.com.cn/api/prediction-v2", "adapter": "whenreset-cn",
     "sourceGroup": "public-post-and-community", "sourceGroupReason": "公开原帖转述与社区观察；模型窗口、社区到账记录不算官方准确预告"},
    {"id": "whenreset-uk", "name": "WhenReset UK", "url": "https://whenreset.uk/",
     "requestUrl": "https://whenreset.uk/api/radar.json", "adapter": "whenreset-uk",
     "sourceGroup": "public-post-and-reference", "sourceGroupReason": "原帖转述及公共参考源；未审核的候选信号和历史推算不构成准确预告"},
    {"id": "ai-resets", "name": "AI Resets", "url": "https://ai-resets.com/",
     "requestUrl": "https://ai-resets.com/", "adapter": "ai-resets",
     "sourceGroup": "codex-resets", "sourceGroupReason": "公开中文网页转述；概率、翻译及页面记录时间未独立核实，不因转载增加可信度"},
)


def utcnow():
    return datetime.now(UTC)


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(UTC) if dt.tzinfo else None
    except (ValueError, OverflowError):
        return None


def iso(value):
    return value.astimezone(UTC).isoformat() if value else None


def safe_url(value):
    """Only public HTTPS links may be shown or requested; never file/user URLs."""
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parts = urllib.parse.urlsplit(html.unescape(value.strip()))
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host or parts.username or parts.password or parts.port not in (None, 443):
            return ""
        if host in ("localhost",) or host.endswith((".localhost", ".local")):
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        return urllib.parse.urlunsplit(("https", host, parts.path or "/", parts.query, ""))
    except ValueError:
        return ""


def canonical_url(value):
    value = safe_url(value)
    if not value:
        return ""
    parts = urllib.parse.urlsplit(value)
    if parts.hostname in ("x.com", "twitter.com", "www.x.com", "www.twitter.com"):
        match = re.search(r"/([^/]+)/status/(\d+)", parts.path)
        if match:
            return "https://x.com/" + match[1].lower() + "/status/" + match[2]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") or "/", "", ""))


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def clean_text(value, limit=1600):
    if not isinstance(value, str):
        return ""
    parser = _PlainText()
    parser.feed(value[:16000])
    return re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()[:limit]


def event_timing(text, published_at=None):
    """Extract only a time actually stated for an activity, never its post time.

    Ambiguous Pacific abbreviations in summer and vague 'EOD/tomorrow' remain
    unknown. Relative durations are deadlines, not the exact execution time.
    """
    text = clean_text(text)
    lower = text.lower()
    unknown = {"resetAt": None, "timeKind": "unknown", "timeReason": "未给出明确活动时间和时区"}
    if re.search(r"有效期|到期|过期|expires?\b|expiry|expiration|valid until", lower):
        return dict(unknown, timeReason="卡片有效期不是公开活动的重置时间")
    if re.search(r"\b(?:pst|pdt|pt)\b", lower):
        # Trackers currently disagree by one hour when converting literal PST
        # during daylight-saving months. Do not choose a site's interpretation.
        return {"resetAt": None, "timeKind": "window", "timeReason": "PST/PT 与夏令时换算存在歧义，待核实原帖时区"}
    deadline = bool(re.search(r"\b(?:before|by|within|no later than)\b|之前|前到|最迟|截至|以内|内到", lower))
    # A complete date with a numerical UTC offset has no locale/DST ambiguity.
    match = re.search(r"\b(20\d\d-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2}))\b", text, re.I)
    target = parse_time(match[1].replace(" ", "T")) if match else None
    if target is None:
        match = re.search(r"(20\d\d)[年/-](\d{1,2})[月/-](\d{1,2})日?\s*(?:at\s*)?(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(UTC(?:\s*[+-]\s*\d{1,2}(?::\d{2})?)?|GMT|北京时间|中国标准时间)", text, re.I)
        if match:
            try:
                year, month, day, hour, minute = (int(match[i]) for i in range(1, 6))
                zone = match[7].upper().replace(" ", "")
                offset = 8 * 60 if zone in ("北京时间", "中国标准时间") else 0
                off = re.search(r"([+-])(\d{1,2})(?::(\d{2}))?", zone)
                if off:
                    offset = (int(off[2]) * 60 + int(off[3] or 0)) * (1 if off[1] == "+" else -1)
                target = datetime(year, month, day, hour, minute, int(match[6] or 0), tzinfo=timezone(timedelta(minutes=offset))).astimezone(UTC)
            except (ValueError, OverflowError):
                pass
    if target is not None:
        return {"resetAt": iso(target), "timeKind": "deadline" if deadline else "exact", "timeReason": "原文明确日期、时间与时区；属于预告，不代表个人账户到账"}
    published = parse_time(published_at)
    relative = re.search(r"within\s+(?:the\s+)?(?:next\s+)?(\d{1,2})\s*(hours?|minutes?)\b", lower)
    if published and relative:
        count = int(relative[1])
        if 0 < count <= 48:
            target = published + timedelta(**{"hours" if relative[2].startswith("hour") else "minutes": count})
            return {"resetAt": iso(target), "timeKind": "deadline", "timeReason": "按原帖明确时长计算的最迟时间，实际可能提前；未核实个人到账"}
    if re.search(r"tomorrow|today|soon|later|eod|明天|今天|稍后|近期|预计|计划", lower):
        return dict(unknown, timeKind="window", timeReason="仅有模糊窗口，未补造具体时刻")
    return unknown


def _event(source, item, *, text="", published=None, status="unknown", event_kind="related", context="", timing=None, original=None):
    text = clean_text(text)
    original = safe_url(original or item.get("url") or item.get("source_url"))
    timed = timing or event_timing(text, published)
    link_is_original = bool(re.match(r"https://x\.com/(?:thsottiaux|openaidevs)/status/\d+", canonical_url(original)))
    confidence = "medium" if link_is_original else "low"
    reason = "社区转述且有原帖链接；本程序未直接核实官方原帖，多个镜像不重复加分" if link_is_original else "社区记录或观察，缺少可直接核实的官方原帖"
    if timed.get("timeKind") in ("window", "estimate"):
        confidence = "low"
        reason += "；" + timed.get("timeReason", "预估时间未证实")
    event_id = str(item.get("id") or item.get("tweet_id") or hashlib.sha256((original + text).encode()).hexdigest()[:20])
    return {"id": event_id[:150], "title": clean_text(item.get("title"), 140) or ("重置卡发放活动" if event_kind == "grant" else "Codex 公开重置活动"),
            "sourceId": source["id"], "sourceName": source["name"], "url": source["url"], "originalUrl": original,
            "sourceGroup": source.get("sourceGroup", source["id"]), "sourceGroupReason": source.get("sourceGroupReason", ""),
            "publishedAt": iso(parse_time(published)), "resetAt": timed.get("resetAt"), "timeKind": timed.get("timeKind", "unknown"),
            "timeReason": timed.get("timeReason", ""), "eventKind": event_kind, "status": status,
            "confidence": confidence, "confidenceReason": reason, "text": text, "context": clean_text(context, 600),
            "claimedTimeText": clean_text(text, 220) if timed.get("timeKind") == "window" else ""}


def _activity(text):
    return bool(re.search(r"reset|banked|credits|重置|额度|发放", text, re.I))


def _forecast(text):
    return bool(re.search(r"\b(?:will|landing|lands|before|within|tomorrow|upcoming|scheduled)\b|预告|将于|预计|计划|之前|最迟", text, re.I))


def _cancelled(text):
    text = re.sub(r"\bnot\s+(?:(?:been|yet|being|be)\s+)*cancel(?:led|ed)?\b"
                  r"|\b(?:isn['’]t|wasn['’]t|aren['’]t|weren['’]t)\s+cancel(?:led|ed)?\b"
                  r"|(?:尚未|没有|不会|未|不)(?:被)?取消", "", text, flags=re.I)
    return bool(re.search(r"\bcancel(?:led|ed)?\b|取消|撤回", text, re.I))


def _completed(text):
    """Completion language takes precedence over quoted earlier plans."""
    text = re.sub(r"\b(?:will|shall|to|be)\s+(?:(?:be|fully|all|soon|automatically)\s+)*(?:completed|delivered|propagated|done)\b"
                  r"|\b(?:not(?:\s+yet)?|never)\s+(?:completed|delivered|done|propagated)\b", "", text, flags=re.I)
    return bool(re.search(r"\b(?:completed|delivered|propagated|all landed|confirmed landed)\b"
                          r"|^(?:it['’]s\s+|it is\s+|all\s+)?done(?: and dusted)?(?:[.!\s]|$)"
                          r"|\bit(?:['’]s|\s+is)\s+done\b|\breset\s+(?:(?:is|now|all|already)\s+)*done\b"
                          r"|\b(?:has|have|had)\s+(?:(?:already|now|just|been)\s+)*(?:reset|landed|credited)\b"
                          r"|\b(?:was|were)\s+(?:already\s+)?(?:reset|delivered|completed)\b"
                          r"|(?:已|已经)(?:全部|完成|成功)?(?:重置|发放|到账|完成)", text, re.I))


def _public_status(text, declared="", *, historical=False, published=None, timing=None, now=None):
    """A fresh feed does not turn an archive, model prediction or hint into a plan."""
    declared = str(declared).lower()
    if _cancelled(text) or declared in ("cancelled", "canceled", "withdrawn"):
        return "cancelled"
    if _completed(text) or declared in ("completed", "delivered", "confirmed"):
        return "completed"
    if historical or declared in ("recorded", "observed", "archive", "archived"):
        return "recorded"
    if declared in ("hint", "note", "summary", "credit", "unknown"):
        return "unknown"
    if not _forecast(text):
        return "unknown"
    now = now or utcnow()
    timing = timing or event_timing(text, published)
    stated = parse_time(timing.get("resetAt"))
    posted = parse_time(published)
    if stated and stated <= now:
        return "recorded"
    if not stated and posted and now - posted > timedelta(days=2):
        # Re-publishing a feed containing an old "tomorrow/soon" quote must not
        # make that quote a current forecast again.
        return "recorded"
    return "upcoming"


def _history_timing():
    return {"resetAt": None, "timeKind": "unknown", "timeReason": "历史或已完成活动记录；发布、观察及归档时间不是未来重置时间"}


class _FeedScript(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "public-feed-data":
            self.inside = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


class _ResetAppPage(HTMLParser):
    """Read only visible event rows, never hero estimates, scripts or paging."""
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.stack = []
        self.row = None
        self.row_depth = None
        self.rows = []
        self.synced_at = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        if tag not in self._VOID:
            self.stack.append((tag, classes))
        if tag == "article" and "event-row" in classes and self.row is None:
            self.row = {"title": [], "text": [], "badge": [], "context": [], "links": []}
            self.row_depth = len(self.stack)
        if tag == "time":
            if self.row is not None and "event-date" in classes:
                self.row["published"] = attrs.get("datetime")
            if any("reset-overview-footer" in frame[1] for frame in self.stack):
                self.synced_at = attrs.get("datetime")
        if self.row is not None and tag == "a":
            url = safe_url(urllib.parse.urljoin(self.page_url, attrs.get("href", "")))
            if url:
                self.row["links"].append(url)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if index is None:
            return
        if self.row is not None and self.row_depth is not None and index < self.row_depth and tag == "article":
            if len(self.rows) < MAX_EVENTS_PER_SOURCE:
                self.rows.append(self.row)
            self.row = None
            self.row_depth = None
        del self.stack[index:]

    def handle_data(self, data):
        if self.row is None:
            return
        if any(tag in ("script", "style") for tag, _ in self.stack):
            return
        if any(tag == "h3" for tag, _ in self.stack):
            self.row["title"].append(data)
        elif any("badge" in classes for _, classes in self.stack):
            self.row["badge"].append(data)
        elif any(tag == "p" for tag, _ in self.stack):
            self.row["text"].append(data)
        elif any("calibration-label" in classes for _, classes in self.stack):
            self.row["context"].append(data)


def _parse_reset_app(source, payload, now):
    parser = _ResetAppPage(source["url"])
    parser.feed(payload)
    if not parser.rows:
        raise ValueError("公开页面缺少 event-row，采集格式可能已更改")
    if not parse_time(parser.synced_at):
        raise ValueError("公开页面缺少有效 Last synced，采集格式可能已更改")
    events = []
    for row in parser.rows:
        title, text, badge, context = (clean_text(" ".join(row[key])) for key in ("title", "text", "badge", "context"))
        if not title or not badge or not parse_time(row.get("published")):
            raise ValueError("公开页面活动行字段发生变化")
        if not _activity(title + " " + text):
            continue
        links = row["links"]
        original = next((url for url in links if re.match(r"https://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/\d+", url)), "")
        detail = next((url for url in links if urllib.parse.urlsplit(url).hostname == "codex-reset.app"), "")
        # This page supplies a tracker summary, not the quoted primary post.
        # Even a numerical hero time may only be an upstream conversion.
        timing = {"resetAt": None, "timeKind": "window", "timeReason": "公开页面为第三方摘要；主页推算时点未核实原帖，不生成准确倒计时"}
        lower_badge = badge.lower()
        declared = ("cancelled" if _cancelled(badge) else "completed" if "completion" in lower_badge or "completed" in lower_badge
                    else "recorded" if "observation" in lower_badge else "planned" if "announced" in lower_badge else "unknown")
        status = _public_status(text, declared, published=row.get("published"), timing=timing, now=now)
        if status != "upcoming":
            timing = _history_timing() if status in ("completed", "recorded") else dict(timing, timeKind="unknown")
        event = _event(source, {"id": detail or original, "title": title}, text=text, published=row.get("published"),
                       status=status, event_kind="forecast" if status == "upcoming" else "related", timing=timing,
                       original=original or detail, context=" · ".join(value for value in (badge, context) if value))
        event.update(confidence="low", confidenceReason="第三方页面摘要，未取得原帖正文；观察、自动发布及推算时点均不构成准确预告")
        events.append(event)
    return events, {"upstreamStale": False, "snapshotAt": iso(parse_time(parser.synced_at))}


def parse_source(source, payload, now=None):
    """Adapt actual public endpoint schemas; malformed schemas fail visibly."""
    adapter = source["adapter"]
    now = now or utcnow()
    if adapter in ("codexclock", "whenreset-cn", "whenreset-uk", "ai-resets"):
        from forecast_extra_sites import parse_extra_site
        return parse_extra_site(source, payload, now)
    if adapter == "codex-reset-app":
        return _parse_reset_app(source, payload, now)
    if adapter == "skill2web":
        parser = _FeedScript()
        parser.feed(payload)
        if not parser.parts:
            raise ValueError("页面缺少 public-feed-data，采集格式可能已更改")
        data = json.loads("".join(parser.parts))
    elif adapter in ("rss", "whennextreset"):
        return parse_rss(source, payload, now=now)
    else:
        data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("来源返回的不是 JSON 对象")
    events = []
    meta = {"upstreamStale": False}
    if adapter == "radar":
        if not isinstance(data.get("events"), list) or not isinstance(data.get("tweets"), list):
            raise ValueError("雷达 API 字段发生变化")
        meta.update(upstreamStale=data.get("stale") is True, snapshotAt=iso(parse_time(data.get("fetched_at"))))
        tweets = {str(t.get("id")): t for t in data["tweets"] if isinstance(t, dict)}
        for item in data["events"][:MAX_EVENTS_PER_SOURCE]:
            if not isinstance(item, dict) or item.get("type") not in ("reset", "credits"):
                continue
            tweet = tweets.get(str(item.get("id")), {})
            text = tweet.get("text") or item.get("summary") or item.get("text") or ""
            parent = tweets.get(str(tweet.get("in_reply_to_tweet_id")), {})
            if not _activity(text + " " + parent.get("text", "")) and item.get("type") != "reset":
                continue
            future = _forecast(text)
            status = "cancelled" if _cancelled(text) else "upcoming" if future else "recorded" if item.get("source") in ("archive", "observed", "operator-observed") else "unknown"
            if status != "cancelled" and (_completed(text) or item.get("observation_result") == "reset_observed" or item.get("banked_state") == "available"
                                           or item.get("reset_verification_status") in ("delivered", "confirmed", "verified")):
                status = "completed"
            # announced_at and the feed's 'announcement / confirmation time'
            # are not execution timestamps, even if a site calls confidence high.
            event = _event(source, item, text=text, published=tweet.get("at") or item.get("announced_at"), status=status,
                           event_kind="grant" if item.get("type") == "credits" else "forecast" if future else "reset", context=parent.get("text", ""))
            events.append(event)
    elif adapter == "resets":
        content = data.get("data")
        if not isinstance(content, dict) or "scheduled_reset" not in content:
            raise ValueError("Codex Resets API 字段发生变化")
        meta["snapshotAt"] = iso(parse_time((data.get("meta") or {}).get("generated_at")))
        for key in ("scheduled_reset", "latest_reset"):
            item = content.get(key)
            if not isinstance(item, dict):
                continue
            rawsource = item.get("source") if isinstance(item.get("source"), dict) else {}
            text = item.get("text") or ""
            scheduled = key == "scheduled_reset"
            timing = event_timing(text, item.get("announced_at"))
            # scheduled_for is a community conversion, not primary evidence.
            # Preserve disagreement/ambiguity rather than trusting its ISO shape.
            estimate = parse_time(item.get("scheduled_for"))
            stated = parse_time(timing.get("resetAt"))
            if estimate and stated and abs((estimate - stated).total_seconds()) > 60:
                timing = {"resetAt": None, "timeKind": "window", "timeReason": "站点换算与原文时间不一致，暂不倒计时"}
            event = _event(source, item, text=text, published=item.get("announced_at"),
                           status="cancelled" if item.get("status") in ("cancelled", "canceled", "withdrawn") or _cancelled(text) else "completed" if item.get("status") in ("completed", "delivered") or _completed(text) else "upcoming" if scheduled else "recorded",
                           event_kind="grant" if item.get("reset_type") == "banked" else "forecast" if scheduled else "reset", timing=timing, original=rawsource.get("url"))
            if rawsource.get("type") == "observed":
                event.update(resetAt=None, timeKind="unknown", confidence="low", confidenceReason="站点监测账户的观察记录，不代表个人到账或未来活动时间")
            if scheduled and estimate:
                event["estimateAt"] = iso(estimate)
                event["claimedTimeText"] = estimate.astimezone(BEIJING).strftime("站点推算：%Y-%m-%d %H:%M:%S 北京时间（未核实）")
            events.append(event)
    elif adapter == "whenreset":
        products = data.get("products")
        codex = products.get("codex") if isinstance(products, dict) else None
        if not isinstance(codex, dict) or not isinstance(codex.get("events"), list):
            raise ValueError("WhenReset API 字段发生变化")
        # updated_at is the content revision, not a freshness heartbeat.
        meta["contentUpdatedAt"] = iso(parse_time(data.get("updated_at")))
        for item in codex["events"][:MAX_EVENTS_PER_SOURCE]:
            if not isinstance(item, dict) or item.get("type") != "reset":
                continue
            text = item.get("summary") or item.get("summary_zh") or ""
            future = _forecast(text)
            completed = parse_time(item.get("delivered_at"))
            event = _event(source, item, text=text, published=item.get("ts"),
                           status="cancelled" if _cancelled(text) else "completed" if completed or _completed(text) else "upcoming" if future else "recorded",
                           event_kind="grant" if item.get("kind") == "banked" else "forecast" if future else "reset")
            if completed:
                event.update(deliveredAt=iso(completed), deliveredSourceUrl=safe_url(item.get("delivered_source_url")))
            events.append(event)
    elif adapter == "skill2web":
        content = data.get("codex")
        if not isinstance(content, dict) or not isinstance(content.get("events"), list):
            raise ValueError("Skill2Web API 字段发生变化")
        meta.update(upstreamStale=content.get("stale") is True, snapshotAt=iso(parse_time(content.get("lastSyncedAt"))))
        for item in content["events"][:MAX_EVENTS_PER_SOURCE]:
            if not isinstance(item, dict):
                continue
            text = item.get("text") or ""
            parent = (item.get("context") or {}).get("parent") or {}
            if not _activity(text + " " + parent.get("text", "")):
                continue
            status = item.get("status") if item.get("status") in ("upcoming", "cancelled", "recorded", "completed", "delivered") else "unknown"
            if status == "delivered":
                status = "completed"
            status = "cancelled" if _cancelled(text) else "completed" if _completed(text) else status
            event = _event(source, item, text=text, published=item.get("at"), status=status,
                           event_kind="forecast" if status == "upcoming" else "related", context=parent.get("text", ""))
            if parse_time(item.get("estimatedResetText")):
                event["estimateAt"] = iso(parse_time(item["estimatedResetText"]))
                event["claimedTimeText"] = parse_time(item["estimatedResetText"]).astimezone(BEIJING).strftime("站点推算：%Y-%m-%d %H:%M:%S 北京时间（未核实）")
            # corroboratedBy are mirrors, not independently checked evidence.
            events.append(event)
    elif adapter == "codexreset-dev":
        if not isinstance(data.get("events"), list) or not isinstance(data.get("status"), str):
            raise ValueError("CodexReset.dev API 字段发生变化")
        dependencies = [s for s in data.get("sources", []) if isinstance(s, dict) and s.get("id") == "codex-resets"] if isinstance(data.get("sources"), list) else []
        meta.update(upstreamStale=not parse_time(data.get("resetArchiveSyncedAt")) or data.get("status") == "unknown" or any(s.get("status") != "online" for s in dependencies),
                    snapshotAt=iso(parse_time(data.get("resetArchiveSyncedAt"))),
                    contentUpdatedAt=iso(parse_time(data.get("generatedAt"))))
        for item in data["events"][:MAX_EVENTS_PER_SOURCE]:
            if not isinstance(item, dict) or item.get("type") not in ("global_reset", "banked_reset"):
                continue
            # effective_at/time_precision may describe an announcement or an
            # account observation. Forecast probability is never a timestamp.
            text = item.get("evidence_text") or ""
            published = item.get("announced_at")
            timing = event_timing(text, published)
            status = _public_status(text, item.get("status", "unknown"), published=published, timing=timing, now=now)
            observed = (item.get("time_basis") == "observation" or str(item.get("source_record_id", "")).startswith("observed-")
                        or "observation" in str(item.get("source_author", "")).lower())
            if observed:
                status = "cancelled" if status == "cancelled" else "recorded"
            if status in ("recorded", "completed"):
                timing = _history_timing()
            event = _event(source, item, text=text, published=published, status=status, timing=timing,
                           event_kind="grant" if item.get("type") == "banked_reset" else "forecast" if status == "upcoming" else "reset",
                           original=item.get("source_url"), context=item.get("notes") or item.get("summary") or "")
            if observed:
                event.update(confidence="low", confidenceReason="上游社区账户观察记录，未独立核实官方原帖；不代表个人到账或未来活动时间")
            events.append(event)
    elif adapter == "tibo-watch":
        if data.get("schemaVersion") != 2 or not isinstance(data.get("events"), list):
            raise ValueError("Tibo Watch API 字段发生变化")
        meta.update(upstreamStale=data.get("stale") is True, snapshotAt=iso(parse_time(data.get("snapshotAt"))))
        for item in data["events"][:MAX_EVENTS_PER_SOURCE]:
            if not isinstance(item, dict) or item.get("kind") == "auxiliary" or item.get("stage") == "auxiliary":
                continue
            text = item.get("quote") or ""
            if not item.get("contextReset") is True and not _activity(text):
                continue
            published = item.get("publishedAt")
            timing = event_timing(text, published)
            status = _public_status(text, item.get("stage", "unknown"), published=published, timing=timing, now=now)
            if status in ("recorded", "completed"):
                timing = _history_timing()
            event = _event(source, item, text=text, published=published, status=status, timing=timing,
                           event_kind="grant" if item.get("kind") == "credit" or item.get("stage") == "credit" else "forecast" if status == "upcoming" else "related",
                           original=item.get("originalUrl") or item.get("sourceUrl"), context="聚合站转述原帖；分类和预测窗口不代表独立验证或个人到账")
            events.append(event)
    else:
        raise ValueError("未支持的公开来源适配器")
    return events, meta


def parse_rss(source, payload, now=None):
    """RSS/Atom adapter for future explicitly configured public feeds."""
    if "<!DOCTYPE" in payload.upper() or "<!ENTITY" in payload.upper():
        raise ValueError("不接受带实体声明的 Feed")
    root = ET.fromstring(payload)
    if root.tag not in ("rss", "{http://www.w3.org/2005/Atom}feed"):
        raise ValueError("来源返回的不是 RSS/Atom Feed")
    events = []
    entries = root.findall(".//item") or root.findall("{http://www.w3.org/2005/Atom}entry")
    for entry in entries[:MAX_EVENTS_PER_SOURCE]:
        values = {}
        for child in entry:
            key = child.tag.rsplit("}", 1)[-1]
            values[key] = child.attrib.get("href") if key == "link" and child.attrib.get("href") else "".join(child.itertext())
        text = clean_text((values.get("title") or "") + " " + (values.get("description") or values.get("content") or values.get("summary") or ""))
        if not _activity(text):
            continue
        published = values.get("published") or values.get("updated") or values.get("pubDate")
        if not parse_time(published) and published:
            try:
                parsed = parsedate_to_datetime(published)
                published = iso(parsed) if parsed.tzinfo else None
            except (TypeError, ValueError, OverflowError):
                published = None
        historical = source.get("adapter") == "whennextreset"
        timing = event_timing(text, published)
        status = _public_status(text, historical=historical, published=published, timing=timing, now=now)
        if historical or status in ("recorded", "completed"):
            timing = _history_timing()
        events.append(_event(source, {"id": values.get("guid") or values.get("id"), "title": values.get("title"), "url": values.get("link")},
                             text=text, published=published, status=status, timing=timing,
                             event_kind="grant" if re.search(r"credit|banked|卡片", text, re.I) else "forecast" if status == "upcoming" else "related"))
    return events, {"upstreamStale": False}


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = safe_url(urllib.parse.urljoin(req.full_url, newurl))
        # Feed requests may redirect only within their observed source hostname.
        if not newurl or urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            raise ValueError("来源重定向到未授权地址")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(source):
    url = safe_url(source["requestUrl"])
    if not url:
        raise ValueError("公开来源 URL 必须为安全 HTTPS")
    # Explicitly reuse Windows system proxy discovery used by the card panel.
    # No credentials, account identifiers or local card data enter this request.
    try:
        from panel_logic import child_proxy_env
        env = child_proxy_env()
        proxies = {scheme: env.get(scheme.upper() + "_PROXY") or env.get(scheme + "_proxy") for scheme in ("http", "https")}
        proxies = {k: v for k, v in proxies.items() if v}
    except ImportError:
        proxies = urllib.request.getproxies()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), _SafeRedirect())
    req = urllib.request.Request(url, headers={"User-Agent": "CodexResetCardDesktop/PublicAnnouncements (+https://github.com/hxroo6/codex-reset-card-desktop)",
                                             "Accept": "application/json,application/rss+xml,application/atom+xml,text/html;q=0.8", "Accept-Encoding": "identity"})
    with opener.open(req, timeout=FETCH_TIMEOUT_SECONDS) as response:
        if not safe_url(response.geturl()):
            raise ValueError("来源返回了不安全地址")
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_RESPONSE_BYTES:
            raise ValueError("来源响应超出 2 MiB 限制")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("来源响应超出 2 MiB 限制")
    return payload.decode("utf-8-sig", errors="strict")


def _source_result(source, previous, now):
    old = copy.deepcopy(previous) if isinstance(previous, dict) else {}
    result = {**old, **{k: source[k] for k in ("id", "name", "url", "requestUrl", "sourceGroup", "sourceGroupReason") if k in source}}
    next_attempt = parse_time(old.get("nextAttemptAt"))
    if next_attempt and now < next_attempt:
        return result
    result["fetchedAt"] = iso(now)
    try:
        payload = _fetch(source)
        events, meta = parse_source(source, payload, now=now)
        result.update(events=events, lastSuccessAt=iso(now), error="", failures=0, nextAttemptAt=iso(now + timedelta(seconds=POLL_SECONDS)), **meta)
    except Exception as error:
        # One failed source cannot delete its cached events or block the others.
        failures = min(8, int(old.get("failures", 0)) + 1)
        code = error.code if isinstance(error, urllib.error.HTTPError) else None
        delay = min(6 * 3600, POLL_SECONDS * 2 ** (failures - 1))
        if code in (401, 403):
            delay = max(delay, 3600)
        elif code == 429:
            delay = max(delay, 1800)
            retry = error.headers.get("Retry-After") if error.headers else None
            if retry and retry.isdigit():
                delay = min(24 * 3600, max(delay, int(retry)))
            elif retry:
                try:
                    retry_at = parsedate_to_datetime(retry)
                    if retry_at.tzinfo:
                        delay = min(24 * 3600, max(delay, int((retry_at - now).total_seconds())))
                except (TypeError, ValueError, OverflowError):
                    pass
        if code:
            message = f"HTTP {code}；已退避，保留上次成功缓存"
        elif isinstance(error, (TimeoutError, socket.timeout)):
            message = "公开来源请求超时；保留上次成功缓存"
        elif isinstance(error, (json.JSONDecodeError, ET.ParseError, UnicodeError, ValueError)):
            message = "来源格式无法解析或不安全；保留上次成功缓存"
        else:
            message = "公开来源连接失败；保留上次成功缓存"
        result.update(error=message, failures=failures, nextAttemptAt=iso(now + timedelta(seconds=delay)))
        result.setdefault("events", [])
    return result


def collect_forecasts(previous=None, now=None):
    now = now or utcnow()
    previous = previous if isinstance(previous, dict) else {}
    old_sources = previous.get("sources", [])
    old_sources = {s.get("id"): s for s in old_sources if isinstance(s, dict)} if isinstance(old_sources, list) else {}
    # Four workers bound concurrent requests; only the first page of each fixed
    # endpoint is read. Each endpoint has its own persistent polling budget.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="public-forecast") as pool:
        futures = [pool.submit(_source_result, s, old_sources.get(s["id"]), now) for s in SOURCES]
        results = [future.result() for future in futures]
    return {"schema": 1, "fetchedAt": iso(now), "sources": results}

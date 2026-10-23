"""Read-only public reset announcements. No account or reset-card access.

Each independent source is polled at most every 15 minutes. The sources are
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
            "publishedAt": iso(parse_time(published)), "resetAt": timed.get("resetAt"), "timeKind": timed.get("timeKind", "unknown"),
            "timeReason": timed.get("timeReason", ""), "eventKind": event_kind, "status": status,
            "confidence": confidence, "confidenceReason": reason, "text": text, "context": clean_text(context, 600),
            "claimedTimeText": clean_text(text, 220) if timed.get("timeKind") == "window" else ""}


def _activity(text):
    return bool(re.search(r"reset|banked|credits|重置|额度|发放", text, re.I))


def _forecast(text):
    return bool(re.search(r"\b(?:will|landing|lands|before|within|tomorrow|upcoming|scheduled)\b|预告|将于|预计|计划|之前|最迟", text, re.I))


def _cancelled(text):
    return bool(re.search(r"\bcancel(?:led|ed)?\b|取消|撤回", text, re.I))


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


def parse_source(source, payload):
    """Adapt actual public endpoint schemas; malformed schemas fail visibly."""
    adapter = source["adapter"]
    if adapter == "skill2web":
        parser = _FeedScript()
        parser.feed(payload)
        if not parser.parts:
            raise ValueError("页面缺少 public-feed-data，采集格式可能已更改")
        data = json.loads("".join(parser.parts))
    elif adapter == "rss":
        return parse_rss(source, payload)
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
            if status != "cancelled" and (item.get("observation_result") == "reset_observed" or item.get("banked_state") == "available"
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
                           status="cancelled" if item.get("status") == "cancelled" or _cancelled(text) else "upcoming" if scheduled else "recorded",
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
                           status="cancelled" if _cancelled(text) else "completed" if completed else "upcoming" if future else "recorded",
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
            event = _event(source, item, text=text, published=item.get("at"), status=status,
                           event_kind="forecast" if status == "upcoming" else "related", context=parent.get("text", ""))
            if parse_time(item.get("estimatedResetText")):
                event["estimateAt"] = iso(parse_time(item["estimatedResetText"]))
                event["claimedTimeText"] = parse_time(item["estimatedResetText"]).astimezone(BEIJING).strftime("站点推算：%Y-%m-%d %H:%M:%S 北京时间（未核实）")
            # corroboratedBy are mirrors, not independently checked evidence.
            events.append(event)
    else:
        raise ValueError("未支持的公开来源适配器")
    return events, meta


def parse_rss(source, payload):
    """RSS/Atom adapter for future explicitly configured public feeds."""
    if "<!DOCTYPE" in payload.upper() or "<!ENTITY" in payload.upper():
        raise ValueError("不接受带实体声明的 Feed")
    root = ET.fromstring(payload)
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
        events.append(_event(source, {"id": values.get("guid") or values.get("id"), "title": values.get("title"), "url": values.get("link")},
                             text=text, published=published, status="cancelled" if _cancelled(text) else "upcoming" if _forecast(text) else "unknown",
                             event_kind="forecast" if _forecast(text) else "related"))
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
    result = {**old, **{k: source[k] for k in ("id", "name", "url", "requestUrl")}}
    next_attempt = parse_time(old.get("nextAttemptAt"))
    if next_attempt and now < next_attempt:
        return result
    result["fetchedAt"] = iso(now)
    try:
        payload = _fetch(source)
        events, meta = parse_source(source, payload)
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
    # Four independent workers bound normal latency to one source timeout rather
    # than four sequential timeouts. Each endpoint has its own persistent budget.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="public-forecast") as pool:
        futures = [pool.submit(_source_result, s, old_sources.get(s["id"]), now) for s in SOURCES]
        results = [future.result() for future in futures]
    return {"schema": 1, "fetchedAt": iso(now), "sources": results}

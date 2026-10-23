"""Four user-selected public sites. Parse data only; never execute page scripts."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser

from forecast_sources import (BEIJING, MAX_EVENTS_PER_SOURCE, _activity, _event,
                              _forecast, _history_timing, _public_status,
                              canonical_url, clean_text, event_timing, iso, parse_time)


def _estimate(event, value):
    estimate = parse_time(value)
    if estimate:
        event["estimateAt"] = iso(estimate)
        event["claimedTimeText"] = estimate.astimezone(BEIJING).strftime("站点预计：%Y-%m-%d %H:%M:%S 北京时间（待核实）")
        stated = parse_time(event.get("resetAt"))
        if stated and abs((stated - estimate).total_seconds()) > 60:
            event.update(resetAt=None, timeKind="window", confidence="low",
                         timeReason="网站换算与所存原文时间冲突，停止准确倒计时",
                         confidenceReason="第三方原帖转述与站点预计时间不一致，需重新核实")
    return event


def _json(payload):
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("公开来源应返回 JSON 对象")
    return data


def _negative(text):
    return bool(re.search(r"\b(?:will|shall|would)\s+not\b[^.!?]{0,80}\breset\b|\bno\s+(?:codex\s+)?reset\b|\b(?:not|never)\s+(?:going\s+to\s+)?reset\b|不会[^。！？]{0,30}重置|并非重置", text, re.I))


def _clock(source, data, now):
    if not isinstance(data.get("config"), dict) or not isinstance(data.get("posts"), list) or not isinstance(data.get("resetEvents"), list):
        raise ValueError("CodexClock 数据结构已变化")
    updated = parse_time(data["config"].get("dataUpdatedAt"))
    events = []
    seen = set()
    for item in data["posts"][:MAX_EVENTS_PER_SOURCE]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("status", ""))
        # Ordinary product posts may say "not a reset". Only the site's reset
        # and related-signal categories enter this read-only announcement list.
        if label not in ("确认重置", "相关信号", "重置预告") and item.get("category") != "重置":
            continue
        text = " · ".join(filter(None, (item.get("title"), item.get("summary"))))
        posted = item.get("at")
        event = _event(source, item, text=text, published=posted, status="recorded" if label == "确认重置" else "unknown",
                       event_kind="related", timing=_history_timing(), original=item.get("url"))
        # These are curated summaries, not a directly checked primary quote.
        event.update(confidence="low", confidenceReason="静态整理的中文摘要，未独立读取原帖；历史概率不提供未来重置时间")
        events.append(event)
        seen.add(canonical_url(event["originalUrl"]))
    sources = data.get("eventSources", {})
    if not isinstance(sources, dict):
        raise ValueError("CodexClock 历史来源结构已变化")
    for day, item in sources.items():
        if not isinstance(item, dict) or canonical_url(item.get("url")) in seen:
            continue
        event = _event(source, dict(item, id=day), text=item.get("title", ""), status="recorded", event_kind="reset",
                       timing=_history_timing(), original=item.get("url"), context="网页显示时间：" + str(item.get("time", "未知")))
        event.update(confidence="low", confidenceReason="网页历史摘要；仅有日期或不完整时间，不补造原帖发布时间")
        events.append(event)
    return events[:MAX_EVENTS_PER_SOURCE], {"snapshotAt": iso(updated), "upstreamStale": updated is None}


def _cn(source, data, now):
    if data.get("schemaVersion") != 2 or not isinstance(data.get("posts"), list) or not isinstance(data.get("events"), list):
        raise ValueError("WhenReset 中国公开 API 结构已变化")
    sync = data.get("sync") if isinstance(data.get("sync"), dict) else {}
    checked = parse_time(sync.get("lastSuccess"))
    announcement = data.get("announcement") if isinstance(data.get("announcement"), dict) else {}
    announced_id = str(announcement.get("sourcePostId", ""))
    events, seen = [], set()
    for item in data["posts"][:MAX_EVENTS_PER_SOURCE]:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("bodyEn") or ""
        context = item.get("summaryZh") or item.get("summaryEn") or ""
        if not _activity(text + " " + context) and str(item.get("id")) != announced_id:
            continue
        published = item.get("created_at")
        timing = event_timing(text, published)
        declared = "planned" if item.get("displayState") == "announced" else ""
        status = _public_status(text, declared, published=published, timing=timing, now=now)
        negative = _negative(text)
        if negative:
            status, timing = "unknown", {"resetAt": None, "timeKind": "unknown", "timeReason": "原文包含否定重置的表述，不生成准确倒计时"}
        if status in ("recorded", "completed"):
            timing = _history_timing()
        if str(item.get("id")) == announced_id and announcement.get("status") in ("cancelled", "withdrawn", "completed", "delivered"):
            status = "cancelled" if announcement["status"] in ("cancelled", "withdrawn") else "completed"
            timing = _history_timing()
        event = _event(source, item, text=text, published=published, status=status,
                       event_kind="forecast" if status == "upcoming" else "related", timing=timing,
                       original=item.get("source"), context=context)
        if str(item.get("id")) == announced_id:
            target = announcement.get("target") if isinstance(announcement.get("target"), dict) else {}
            _estimate(event, target.get("at"))
        if negative:
            event.update(confidence="low", confidenceReason="原文含否定重置表述，不将关键词或网站标题当作肯定预告")
        events.append(event)
        seen.add(canonical_url(event["originalUrl"]))
    # Account observations and curated baseline history are displayed as records,
    # not promoted to a new primary-source completion or forecast.
    for item in data["events"][:MAX_EVENTS_PER_SOURCE]:
        if not isinstance(item, dict):
            continue
        original = item.get("source") or item.get("announcementSource")
        if canonical_url(original) and canonical_url(original) in seen:
            continue
        event = _event(source, item, text=item.get("summary", ""), status="recorded", event_kind="grant" if item.get("type") == "card" else "reset",
                       timing=_history_timing(), original=original, context="站点观察/历史时间：" + str(item.get("timestamp", "未知")))
        event.update(confidence="low", confidenceReason="站点历史或社区观察，未核实官方完成证据；记录时间不是原帖或下一次重置时间")
        events.append(event)
    return events[:MAX_EVENTS_PER_SOURCE], {"snapshotAt": iso(checked), "upstreamStale": checked is None or sync.get("status") not in ("fresh", "ok")}


def _uk(source, data, now):
    if data.get("version") != 1 or not isinstance(data.get("events"), list) or not isinstance(data.get("sync"), dict):
        raise ValueError("WhenReset UK 公开 API 结构已变化")
    sync = data["sync"]
    checked = parse_time(sync.get("lastSuccessAt"))
    events = []
    for item in data["events"][:MAX_EVENTS_PER_SOURCE]:
        if not isinstance(item, dict) or item.get("status") == "dismissed":
            continue
        text = item.get("text") or ""
        if item.get("type") not in ("reset", "card", "positive") and not _activity(text):
            continue
        published = item.get("publishedAt")
        timing = event_timing(text, published)
        classification = item.get("classification") if isinstance(item.get("classification"), dict) else {}
        uncertain = item.get("status") == "candidate" or classification.get("phase") in ("uncertain", "hint")
        declared = item.get("status") if item.get("status") in ("cancelled", "withdrawn", "completed", "delivered") else ""
        status = _public_status(text, declared, published=published, timing=timing, now=now)
        if _negative(text):
            status, uncertain = "unknown", True
        if uncertain:
            status = "unknown"
            timing = dict(timing, resetAt=None, timeReason="网站仍将此条标为未审核候选，不能据此提供准确倒计时")
        elif status in ("completed", "recorded"):
            timing = _history_timing()
        event = _event(source, item, text=text, published=published, status=status,
                       event_kind="grant" if item.get("type") == "card" else "forecast" if status == "upcoming" else "related",
                       timing=timing, original=item.get("sourceUrl"), context=item.get("summary", ""))
        _estimate(event, item.get("suggestedResetAt") or item.get("resetAt"))
        if uncertain:
            event.update(confidence="low", confidenceReason="规则识别的未审核候选；网站建议时间和模型推算不构成官方准确时间")
        events.append(event)
    return events, {"snapshotAt": iso(checked), "upstreamStale": checked is None or sync.get("status") != "ok"}


class _Node:
    def __init__(self, tag, attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()

    def has(self, name):
        return name in self.attrs.get("class", "").split()

    def text(self):
        return clean_text(" ".join(child.text() if isinstance(child, _Node) else child for child in self.children))

    def find(self, *, tag=None, css=None):
        return next((node for node in self.walk() if (tag is None or node.tag == tag) and (css is None or node.has(css))), None)


class _Page(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, payload):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]
        self.count = 0
        self.feed(payload)

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 30000:
            raise ValueError("公开页面节点超出限制")
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack)-1, 0, -1) if self.stack[i].tag == tag), None)
        if index is not None:
            del self.stack[index:]

    def handle_data(self, value):
        if not any(node.tag in ("script", "style") for node in self.stack):
            self.stack[-1].children.append(value)


def _primary(node):
    return next((child.attrs.get("href") for child in node.walk() if child.tag == "a" and
                 re.match(r"https://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/\d+", child.attrs.get("href", ""))), "")


def _ai(source, payload, now):
    page = _Page(payload).root
    articles = [node for node in page.walk() if node.tag == "article" and node.has("announcement")]
    if not articles:
        raise ValueError("AI Resets 页面缺少公开 announcement 记录，结构可能已变化")
    events = []
    panel = page.find(css="signal-panel")
    if panel and panel.find(css="signal-watch-summary"):
        detail = panel.find(tag="details")
        quote = detail.find(tag="p") if detail else None
        original = _primary(panel)
        if quote is None or not quote.text() or not original:
            raise ValueError("AI Resets 预告原文或来源字段发生变化")
        text = quote.text()
        summary = panel.find(css="signal-watch-summary").text()
        timing = {"resetAt": None, "timeKind": "window", "timeReason": "第三方网页预告；PST/当地时区及页面换算待核实，概率不代表准确时间"}
        event = _event(source, {"id": original}, text=text, status="upcoming" if _forecast(text) and not _negative(text) else "unknown",
                       event_kind="forecast", timing=timing, original=original, context=summary)
        match = re.search(r"北京时间\s*(20\d{2})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})", summary)
        if match:
            try:
                estimate = datetime(*(int(part) for part in match.groups()), tzinfo=BEIJING)
                _estimate(event, estimate.isoformat())
                if estimate < now - timedelta(days=2):
                    event["status"] = "recorded"
            except ValueError:
                pass
        event.update(confidence="low", confidenceReason="公开中文页面转述，未独立核实原帖与换算；页面概率不作为证据等级")
        events.append(event)
    for article in articles[:MAX_EVENTS_PER_SOURCE]:
        title, time, detail = article.find(tag="h3"), article.find(tag="time"), article.find(tag="details")
        quote = detail.find(tag="p") if detail else None
        listed = parse_time(time.attrs.get("datetime")) if time else None
        original = _primary(article)
        if title is None or quote is None or not listed or not original:
            raise ValueError("AI Resets 公告标题、原文、时间或来源字段发生变化")
        # Its catalogue mixes announcement, converted and observed times. Keep
        # that visible in context instead of mislabelling it as publication.
        event = _event(source, {"id": original + ":catalogue", "title": title.text()}, text=quote.text(),
                       status="recorded", event_kind="reset", timing=_history_timing(), original=original,
                       context="页面记录时间（非已核实发布时间/生效）：" + iso(listed))
        event.update(confidence="low", confidenceReason="第三方历史页面，中文摘要与所存英文可能不一致；仅保留为待核实记录")
        events.append(event)
    return events[:MAX_EVENTS_PER_SOURCE], {"snapshotAt": None, "upstreamStale": False,
                                           "freshnessNote": "公开页面未提供可核对的源生成时间，仅记录本次成功读取时间"}


def parse_extra_site(source, payload, now):
    if source["adapter"] == "ai-resets":
        return _ai(source, payload, now)
    data = _json(payload)
    return {"codexclock": _clock, "whenreset-cn": _cn, "whenreset-uk": _uk}[source["adapter"]](source, data, now)

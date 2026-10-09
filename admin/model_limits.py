"""Limit recovery times. All returned datetimes are naive UTC, as stored in SQL."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

CN_TIME = timezone(timedelta(hours=8))
DAILY_CODES = frozenset({"6004", "6008"})


def parse_time(value, *, now: datetime) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    try:
        number = float(text)
        if math.isfinite(number) and number > 0:
            # Epoch seconds / milliseconds, never interpret a duration as epoch.
            when = datetime.fromtimestamp(number / 1000 if number >= 1e11 else number,
                                          timezone.utc).replace(tzinfo=None)
            return when if when > now else None
    except (ValueError, OverflowError, OSError):
        pass
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=CN_TIME)
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
        return when if when > now else None
    except ValueError:
        return None


def reset_from_body(body: str, now: datetime | None = None) -> datetime | None:
    """Read structured reset fields, localized wall clocks and relative reset text.

    Only call for an upstream error, never for generated model content.
    """
    now = now or datetime.utcnow()
    if not body:
        return None
    body = body[:65536]
    texts = [body]
    candidates = [body] + [line[5:].strip() for line in body.splitlines()
                           if line.startswith("data:")][:64]

    def visit(obj, depth=0):
        if not isinstance(obj, dict) or depth > 4:
            return None
        for key in ("reset_at", "resetAt", "reset_time", "resetTime", "retryAt"):
            when = parse_time(obj.get(key), now=now)
            if when:
                return when
        for key in ("msg", "message", "detail", "details", "zh", "en"):
            if isinstance(obj.get(key), str):
                texts.append(obj[key])
        for key in ("error", "data", "displayMsg", "details"):
            when = visit(obj.get(key), depth + 1)
            if when:
                return when
        return None

    for candidate in candidates:
        try:
            when = visit(json.loads(candidate))
            if when:
                return when
        except (ValueError, TypeError):
            pass
    text = " ".join(texts)
    for match in re.finditer(r"20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}[日\sT]+\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?", text):
        clock = re.sub(r"[年/月]", "-", match.group()).replace("日", " ").strip()
        when = parse_time(clock, now=now)
        if when:
            return when
    if re.search(r"reset|retry|重置|恢复|后重试", text, re.I):
        match = re.search(r"(?:in\s+|(?:将在|等待)\s*)?(\d+(?:\.\d+)?)\s*(小时|分钟|秒|hours?|minutes?|seconds?)", text, re.I)
        if match:
            units = match.group(2).lower()
            factor = 3600 if units.startswith("hour") or units == "小时" else 60 if units.startswith("minute") or units == "分钟" else 1
            seconds = float(match.group(1)) * factor
            if 0 < seconds <= 7 * 86400:
                return now + timedelta(seconds=seconds)
    return None


def reset_from_headers(headers, now: datetime | None = None) -> datetime | None:
    now = now or datetime.utcnow()
    if not headers:
        return None
    normalized = {str(k).lower(): str(v).strip() for k, v in headers.items()}
    for key in ("retry-after", "anthropic-ratelimit-unified-reset", "x-ratelimit-reset"):
        raw = normalized.get(key)
        if not raw:
            continue
        when = None
        if key == "retry-after":
            try:
                seconds = float(raw)
                if math.isfinite(seconds) and 0 < seconds <= 7 * 86400:
                    when = now + timedelta(seconds=seconds)
            except ValueError:
                pass
        else:
            when = parse_time(raw, now=now)
        if when is None:
            try:
                when = parsedate_to_datetime(raw).astimezone(timezone.utc).replace(tzinfo=None)
            except (ValueError, TypeError, OverflowError):
                pass
        if when and when > now:
            return when
    return None


def recovery_time(headers, body: str, *, daily=False) -> datetime | None:
    now = datetime.utcnow()
    header = reset_from_headers(headers, now)
    message = reset_from_body(body, now)
    # A short generic Retry-After must not override an explicit daily reset clock.
    if daily:
        return max((t for t in (header, message) if t), default=None)
    return header or message

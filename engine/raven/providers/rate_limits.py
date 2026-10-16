"""When a provider's rate limit lifts: read from the response headers, else from the error's own words.

Every vendor says it differently, and most say it somewhere:

- ``Retry-After`` -- seconds or an HTTP date (RFC 9110), ``retry-after-ms``;
- OpenAI's ``x-ratelimit-reset-requests`` / ``-tokens`` -- durations like ``6m0s``;
- Anthropic's ``anthropic-ratelimit-*-reset`` -- an RFC 3339 instant;
- a generic ``x-ratelimit-reset`` -- epoch seconds, or seconds from now;
- Gemini's ``"retryDelay": "33s"`` in the error body;
- the message text: "try again in 6m12s", "retry after 30 seconds", the Claude
  CLI's ``usage limit reached|<epoch>``, "resets at 2026-10-14T19:00:00Z",
  "resets 3pm", and the Codex CLI's "try again in 2 days 3 hours" and
  "Try again at Oct 15th, 2026 3:04 PM".

The answer is seconds from now, or None when nothing says. :func:`limit_reset`
asks the narrower question an agent's failure needs: was it a usage or rate
limit at all, and if so when does it lift.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

# Followed by anything but a letter, not by a word boundary: in "6m12s" the "m" touches a digit.
_DURATION_PART = re.compile(
    r"(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|d|days?)(?![a-z])", re.I
)
_TRY_AGAIN = re.compile(
    r"(?:try again|retry|retrying|wait|available again|resets?)\s+(?:in|after)\s+((?:\d+(?:\.\d+)?\s*[a-z]+\s*)+)", re.I
)
_RETRY_DELAY = re.compile(r'"?retryDelay"?\s*[:=]\s*"?(\d+(?:\.\d+)?)s', re.I)
_EPOCH_PIPE = re.compile(r"limit reached\|(\d{10})")
_ISO = re.compile(r"resets?\s+(?:at\s+)?(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)?)", re.I)
_CLOCK = re.compile(r"(?:resets?(?:\s+at)?|try again at)\s+(\d{1,2})(?::(\d\d))?\s*(am|pm)\b", re.I)
# The Codex CLI past today: "Try again at Oct 15th, 2026 3:04 PM" (the year optional).
_DATED_CLOCK = re.compile(
    r"(?:resets?|try again)\s+(?:at|on)\s+([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(?:(\d{4}),?\s+)?"
    r"(?:at\s+)?(\d{1,2}):(\d\d)\s*(am|pm)\b",
    re.I,
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# What makes an agent's failure a usage or rate limit rather than anything else that
# happens to say "retry in": the vendors' own words for it.
_LIMIT_WORDS = re.compile(
    r"usage limit|rate[ _-]?limit|\b429\b|quota|too many requests|limit reached|hit your (?:\w+ )?limit|"
    r"resource[ _]exhausted|limit will reset",
    re.I,
)


def _unit(unit: str) -> float:
    """Seconds in one ``unit``: ms / s / m / h / d and their spelled-out forms."""
    unit = unit.lower()
    if unit == "ms" or unit.startswith("milli"):
        return 0.001
    return {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}.get(unit[:1], 0.0)


def parse_duration(text: str) -> float | None:
    """``6m12s``, ``1h 2m``, ``20 seconds``, ``850ms``, ``2 days 3 hours`` as seconds; None when no part reads as one."""
    total, found = 0.0, False
    for amount, unit in _DURATION_PART.findall(text or ""):
        scale = _unit(unit)
        if scale:
            total += float(amount) * scale
            found = True
    return total if found else None


def _instant(text: str, now: float) -> float | None:
    value = text.strip().replace(" ", "T", 1)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp() - now


def _header_delay(headers: Any, now: float) -> float | None:
    if not headers:
        return None
    try:
        items = {str(k).lower(): str(v) for k, v in dict(headers).items()}
    except (TypeError, ValueError):
        return None
    if (ms := items.get("retry-after-ms")) and ms.replace(".", "", 1).isdigit():
        return float(ms) / 1000
    if retry := items.get("retry-after"):
        if retry.replace(".", "", 1).isdigit():
            return float(retry)
        try:
            return parsedate_to_datetime(retry).timestamp() - now
        except (TypeError, ValueError):
            pass
    waits = []
    for key, value in items.items():
        if key.startswith("anthropic-ratelimit-") and key.endswith("-reset"):
            if (delay := _instant(value, now)) is not None:
                waits.append(delay)
        elif key in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
            if (delay := parse_duration(value)) is not None:
                waits.append(delay)
        elif key == "x-ratelimit-reset":
            try:
                number = float(value)
            except ValueError:
                continue
            waits.append(number - now if number > 1_000_000_000 else number)
    # Every limit named must lift before a retry can pass, so the latest one counts.
    return max(waits) if waits else None


def _headers_of(exc: BaseException | None) -> Any:
    for holder in (exc, getattr(exc, "response", None)):
        for name in ("headers", "response_headers", "litellm_response_headers"):
            value = getattr(holder, name, None)
            if value:
                return value
    return None


def _text_delay(text: str, now: float) -> float | None:
    if not text:
        return None
    if match := _EPOCH_PIPE.search(text):
        return float(match.group(1)) - now
    if match := _ISO.search(text):
        if (delay := _instant(match.group(1), now)) is not None:
            return delay
    if match := _RETRY_DELAY.search(text):
        return float(match.group(1))
    if match := _TRY_AGAIN.search(text):
        if (delay := parse_duration(match.group(1))) is not None:
            return delay
    if (match := _DATED_CLOCK.search(text)) and match.group(1).lower() in _MONTHS:
        month, day, year, hour, minute, half = match.groups()
        local = datetime.fromtimestamp(now).astimezone()
        hour24 = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
        try:
            target = local.replace(
                year=int(year) if year else local.year,
                month=_MONTHS.index(month.lower()) + 1,
                day=int(day),
                hour=hour24,
                minute=int(minute),
                second=0,
                microsecond=0,
            )
        except ValueError:
            return None
        if not year and target.timestamp() < now - 86400:
            # "Jan 2" read in late December is next year's.
            target = target.replace(year=target.year + 1)
        return target.timestamp() - now
    if match := _CLOCK.search(text):
        hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "pm" else 0)
        local = datetime.fromtimestamp(now).astimezone()
        target = local.replace(hour=hour, minute=int(match.group(2) or 0), second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp() - now
    return None


def reset_delay(
    exc: BaseException | None = None, content: str | None = None, *, now: float | None = None
) -> float | None:
    """Seconds until the limit lifts, from the exception's headers or the error text; None when nothing says."""
    now = time.time() if now is None else now
    delay = _header_delay(_headers_of(exc), now)
    if delay is None:
        delay = _text_delay(content if content is not None else (str(exc) if exc is not None else ""), now)
    if delay is None:
        return None
    return max(0.0, round(delay, 3))


def limit_reset(text: str, *, now: float | None = None) -> float | None:
    """Seconds until a usage or rate limit lifts, when ``text`` is one and says when; else None.

    For a failure that arrives as words only -- a CLI or ACP agent's last line, an
    agent endpoint's error -- where :func:`reset_delay` alone would also read a
    "retrying in 5s" about a dropped connection as a limit's reset.
    """
    if not text or not _LIMIT_WORDS.search(text):
        return None
    return reset_delay(None, text, now=now)


__all__ = ["limit_reset", "parse_duration", "reset_delay"]

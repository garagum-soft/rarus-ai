"""Operator notifications: rules route events that need a person to the channels that reach them.

An event is something the operator should hear about now -- in practice a
problem raised in the attention queue (``raven.proactive_engine.attention``):
an approval, a question, a credential the agent asked for, a stint question,
the host guard stopping the agents. Each carries its kind, a priority, a title
and a key that says which problem it is.

Rules (``gateway.notifications``) say which events go where. An event's names
are its kind, ``escalation`` when it is raised at high or critical, and ``*``;
a rule matches when one of its ``on`` names is among them and the event is at
least its ``min_priority``. The channels are a webhook (generic JSON, Slack or
Discord), ntfy (a push to a phone), a sound and a desktop notification.

What keeps it worth reading, taken from ECC's notifier and AutoGPT's alert
engine and applied per channel:

- a channel that is switched off sends nothing, whatever the rules say;
- the same problem (by key) is not sent twice on a channel within
  ``repeat_after_s``;
- at most ``daily_cap`` deliveries per channel per day -- past that the
  problem still waits in the attention queue, it just stops buzzing;
- quiet hours hold everything but critical;
- a problem already solved by the time the delivery runs is not sent.

Every delivery and every reason for not delivering goes to
``notifications.jsonl`` under the raven home, so "why didn't I hear about it"
has an answer.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.atomic_io import locked_append

PRIORITIES = ("critical", "high", "normal", "low")
_RANK = {p: i for i, p in enumerate(PRIORITIES)}


@dataclass(frozen=True)
class Event:
    kind: str
    priority: str
    title: str
    key: str
    detail: str = ""
    session_key: str = ""
    source: str = ""

    def names(self) -> set[str]:
        out = {self.kind, "*"}
        if _RANK.get(self.priority, 3) <= _RANK["high"]:
            out.add("escalation")
        return out


@dataclass
class Delivery:
    channel: str
    sent: bool
    reason: str = ""


@dataclass
class Settings:
    enabled: bool = False
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    rules: list[dict[str, Any]] = field(default_factory=list)
    repeat_after_s: float = 24 * 3600
    daily_cap: int = 50
    quiet_start_hour: int = 0
    quiet_end_hour: int = 0

    @classmethod
    def from_config(cls, cfg: Any) -> Settings:
        """From ``gateway.notifications`` (the pydantic model, snake_case)."""
        return cls(
            enabled=cfg.enabled,
            channels={name: ch.model_dump() for name, ch in cfg.channels.items()},
            rules=[r.model_dump() for r in cfg.rules],
            repeat_after_s=cfg.repeat_after_s,
            daily_cap=cfg.daily_cap,
            quiet_start_hour=cfg.quiet_start_hour,
            quiet_end_hour=cfg.quiet_end_hour,
        )

    def quiet(self, hour: int) -> bool:
        start, end = self.quiet_start_hour, self.quiet_end_hour
        if start == end:
            return False
        return start <= hour < end if start < end else hour >= start or hour < end


# -- the channels -------------------------------------------------------------

Sender = Callable[[dict[str, Any], Event], None]


def _post(
    url: str, *, json_body: Any = None, data: bytes | None = None, headers: Mapping[str, str] | None = None
) -> None:
    import httpx

    response = httpx.post(url, json=json_body, content=data, headers=dict(headers or {}), timeout=10.0)
    response.raise_for_status()


def _message(event: Event) -> str:
    where = f" [{event.session_key}]" if event.session_key else ""
    detail = f"\n{event.detail}" if event.detail else ""
    return f"[raven:{event.priority}] {event.title}{where}{detail}"


def webhook_payload(channel: Mapping[str, Any], event: Event) -> dict[str, Any]:
    fmt = channel.get("format") or "generic"
    if fmt == "slack":
        return {"text": _message(event)}
    if fmt == "discord":
        # No pings: an agent's text must not mention @everyone on its way through.
        return {"content": _message(event), "allowed_mentions": {"parse": []}}
    return {"source": "raven", **asdict(event), "message": _message(event), "time": time.time()}


def send_webhook(channel: Mapping[str, Any], event: Event) -> None:
    url = str(channel.get("url") or "")
    if not url.startswith(("https://", "http://")):
        raise ValueError("a webhook channel needs an http(s) url")
    _post(url, json_body=webhook_payload(channel, event))


_NTFY_PRIORITY = {"critical": "5", "high": "4", "normal": "3", "low": "2"}


def send_ntfy(channel: Mapping[str, Any], event: Event) -> None:
    topic = str(channel.get("topic") or "").strip("/")
    if not topic:
        raise ValueError("an ntfy channel needs a topic")
    server = str(channel.get("server") or "https://ntfy.sh").rstrip("/")
    headers = {"Title": event.title[:200], "Priority": _NTFY_PRIORITY.get(event.priority, "3"), "Tags": event.kind}
    if channel.get("token"):
        headers["Authorization"] = f"Bearer {channel['token']}"
    _post(f"{server}/{topic}", data=(event.detail or event.title).encode("utf-8"), headers=headers)


def send_sound(channel: Mapping[str, Any], event: Event) -> None:
    import sys

    if sys.platform == "win32":
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION if event.priority == "critical" else winsound.MB_OK)
        return
    import subprocess

    if sys.platform == "darwin":
        subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], timeout=10, check=False)  # noqa: S603,S607
        return
    sys.stdout.write("\a")
    sys.stdout.flush()


def send_desktop(channel: Mapping[str, Any], event: Event) -> None:
    import subprocess
    import sys

    body = (event.detail or "")[:300]
    if sys.platform == "darwin":
        script = 'display notification "{}" with title "{}"'.format(
            body.replace('"', "'"), event.title.replace('"', "'")[:120]
        )
        subprocess.run(["osascript", "-e", script], timeout=10, check=True)  # noqa: S603,S607
    elif sys.platform.startswith("linux"):
        subprocess.run(["notify-send", "--app-name", "Raven", event.title[:120], body], timeout=10, check=True)  # noqa: S603,S607
    else:
        raise RuntimeError("desktop notifications are not available on this platform; use sound or ntfy")


SENDERS: dict[str, Sender] = {
    "webhook": send_webhook,
    "ntfy": send_ntfy,
    "sound": send_sound,
    "desktop": send_desktop,
}


# -- the engine -----------------------------------------------------------------


def default_log_path() -> Path:
    from raven.home import raven_home

    return raven_home() / "notifications.jsonl"


class Notifier:
    def __init__(
        self,
        settings: Settings,
        *,
        senders: Mapping[str, Sender] | None = None,
        log_path: Path | None = None,
        still_open: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.senders = dict(senders or SENDERS)
        self.log_path = log_path or default_log_path()
        self._still_open = still_open
        self._clock = clock
        self._last: dict[tuple[str, str], float] = {}
        self._today: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()

    def _log(self, event: Event, delivery: Delivery) -> None:
        row = {"time": self._clock(), "channel": delivery.channel, "sent": delivery.sent, "reason": delivery.reason}
        row.update({"kind": event.kind, "priority": event.priority, "key": event.key, "title": event.title})
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            locked_append(self.log_path, [json.dumps(row, ensure_ascii=False)])
        except OSError as exc:
            logger.debug("notify: could not log a delivery: {}", exc)

    def _targets(self, event: Event) -> list[str]:
        names = event.names()
        rank = _RANK.get(event.priority, 3)
        out: list[str] = []
        for rule in self.settings.rules:
            if not names & set(rule.get("on") or []):
                continue
            if rank > _RANK.get(rule.get("min_priority") or "low", 3):
                continue
            channel = rule.get("channel") or ""
            if channel and channel not in out:
                out.append(channel)
        return out

    def dispatch(self, event: Event) -> list[Delivery]:
        """Route one event; every channel a rule names gets a delivery or a reason it did not."""
        if not self.settings.enabled:
            return []
        now = self._clock()
        out: list[Delivery] = []
        for name in self._targets(event):
            delivery = self._deliver(name, event, now)
            self._log(event, delivery)
            out.append(delivery)
        return out

    def _deliver(self, name: str, event: Event, now: float) -> Delivery:
        channel = self.settings.channels.get(name)
        if channel is None:
            return Delivery(name, False, "no such channel")
        if not channel.get("enabled", True):
            return Delivery(name, False, "channel is switched off")
        if event.priority != "critical" and self.settings.quiet(datetime.fromtimestamp(now).hour):
            return Delivery(name, False, "quiet hours")
        if self._still_open is not None and not self._still_open(event.key):
            return Delivery(name, False, "already resolved")
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        with self._lock:
            last = self._last.get((name, event.key))
            if last is not None and now - last < self.settings.repeat_after_s:
                return Delivery(name, False, "sent recently")
            seen_day, count = self._today.get(name, (day, 0))
            count = count if seen_day == day else 0
            if count >= self.settings.daily_cap:
                return Delivery(name, False, "daily cap reached")
            self._last[(name, event.key)] = now
            self._today[name] = (day, count + 1)
        sender = self.senders.get(str(channel.get("type")))
        if sender is None:
            return Delivery(name, False, f"unknown channel type {channel.get('type')!r}")
        try:
            sender(channel, event)
        except Exception as exc:  # noqa: BLE001 - a failed channel is reported, the others still go
            with self._lock:
                self._last.pop((name, event.key), None)
            return Delivery(name, False, f"failed: {exc}")
        return Delivery(name, True)


# -- the process-wide notifier, fed by the attention queue --------------------

_notifier: Notifier | None = None
_loaded_from: Any = None


def _current() -> Notifier | None:
    """The notifier for the config as it is now (rebuilt when the config file changes)."""
    global _notifier, _loaded_from
    try:
        from raven.config.loader import get_config_path, load_config

        path = get_config_path()
        stamp = (str(path), path.stat().st_mtime_ns) if path.is_file() else (str(path), None)
        if _notifier is None or stamp != _loaded_from:
            from raven.proactive_engine import attention

            _notifier = Notifier(
                Settings.from_config(load_config().gateway.notifications),
                still_open=lambda key: any(i.id == key for i in attention.queue().items()),
            )
            _loaded_from = stamp
    except Exception as exc:  # noqa: BLE001 - notifications never break what raised the event
        logger.debug("notify: config unavailable: {}", exc)
        return None
    return _notifier if _notifier.settings.enabled else None


def emit(event: Event) -> None:
    """Route ``event`` on a thread of its own: a webhook must not hold up the request that raised it."""
    notifier = _current()
    if notifier is None:
        return

    def run() -> None:
        try:
            notifier.dispatch(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("notify: dispatch failed: {}", exc)

    # Not a daemon: a short-lived process (a CLI command that raised the
    # problem) must not exit with the notification still unsent. Each send is
    # bounded by its own timeout, so this holds an exit for seconds at most.
    threading.Thread(target=run, name="raven-notify", daemon=False).start()


__all__ = ["SENDERS", "Delivery", "Event", "Notifier", "Settings", "emit", "webhook_payload"]

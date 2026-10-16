"""Message retention: old messages leave a transcript, but nothing a kept message depends on does.

A message older than its retention is removed from the session file. How old
is too old depends on whether it was read: what came before the session's
``last_read_at`` has been seen and keeps for ``read_days``, what came after it
has not and keeps for ``unread_days`` (a session never marked read counts as
unread throughout, the safe side). Three things keep an expired message
anyway, checked until nothing changes: a tool call and its result go or stay
together, a message another kept message refers to (``reply_to`` / ``refs``
naming its ``id``) stays, and a pinned message stays. A message with no
timestamp is never judged old.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class RetentionPolicy:
    read_days: float
    unread_days: float

    def __post_init__(self) -> None:
        if self.read_days <= 0 or self.unread_days <= 0:
            raise ValueError("retention periods must be positive")


@dataclass
class RetentionPlan:
    keep: list[int] = field(default_factory=list)
    drop: list[int] = field(default_factory=list)
    kept_for: dict[int, str] = field(default_factory=dict)


def _when(message: dict[str, Any]) -> datetime | None:
    raw = message.get("timestamp")
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return when.replace(tzinfo=None)


def plan(
    messages: Sequence[dict[str, Any]],
    policy: RetentionPolicy,
    *,
    now: datetime,
    last_read_at: datetime | None = None,
) -> RetentionPlan:
    """Which messages stay and which go, and why each expired one that stays was kept."""
    now = now.replace(tzinfo=None)
    read_mark = last_read_at.replace(tzinfo=None) if last_read_at else None
    keep: set[int] = set()
    for index, message in enumerate(messages):
        when = _when(message)
        if when is None:
            keep.add(index)
            continue
        read = read_mark is not None and when <= read_mark
        days = policy.read_days if read else policy.unread_days
        if when >= now - timedelta(days=days):
            keep.add(index)
    result = RetentionPlan()
    calls: dict[str, int] = {}
    for index, message in enumerate(messages):
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id"):
                calls[str(call["id"])] = index
    changed = True
    while changed:
        changed = False
        for index, message in enumerate(messages):
            if index in keep:
                continue
            reason = ""
            if message.get("pinned"):
                reason = "pinned"
            call_id = message.get("tool_call_id")
            if not reason and call_id and calls.get(str(call_id)) in keep:
                reason = "its tool call is kept"
            if not reason:
                owned = {str(c.get("id")) for c in message.get("tool_calls") or [] if isinstance(c, dict)}
                if owned and any(
                    str(m.get("tool_call_id")) in owned
                    for i, m in enumerate(messages)
                    if i in keep and m.get("tool_call_id")
                ):
                    reason = "a kept result answers its tool call"
            if not reason and message.get("id"):
                own = str(message["id"])
                for i in keep:
                    other = messages[i]
                    refs = [other.get("reply_to"), *(other.get("refs") or [])]
                    if own in {str(r) for r in refs if r}:
                        reason = "a kept message refers to it"
                        break
            if reason:
                keep.add(index)
                result.kept_for[index] = reason
                changed = True
    result.keep = sorted(keep)
    result.drop = [i for i in range(len(messages)) if i not in keep]
    return result


def apply(manager: Any, key: str, policy: RetentionPolicy, *, now: datetime, dry_run: bool = False) -> RetentionPlan:
    """Plan retention for one session and, unless ``dry_run``, rewrite its transcript without the dropped messages."""
    session = manager.get_or_create(key)
    raw_mark = session.metadata.get("last_read_at")
    mark = datetime.fromisoformat(raw_mark) if raw_mark else None
    outcome = plan(session.messages, policy, now=now, last_read_at=mark)
    if outcome.drop and not dry_run:
        session.messages = [session.messages[i] for i in outcome.keep]
        session.last_consolidated = min(session.last_consolidated, len(session.messages))
        manager.save(session)
    return outcome


def mark_read(manager: Any, key: str, *, at: datetime) -> None:
    """Everything in the session up to ``at`` has been read."""
    manager.append_metadata_patch(key, {"last_read_at": at.isoformat()})


__all__ = ["RetentionPlan", "RetentionPolicy", "apply", "mark_read", "plan"]

"""The attention queue: everything that is waiting on a person, in one place, by priority.

A tool approval, an ask_user question, a credential the agent asked for, the
host guard stopping the agents -- each of these is a problem only a person can
clear, and each used to surface only where it happened (one card in one
conversation's composer, one line in a log). The queue is the list of them
across every conversation, ordered by how much they need the operator now.

Kept on the server, in one JSON file under the raven home, so it outlives a
page reload and a restart. Written under the file's lock: the gateway and a
``raven tui`` process both raise items.

Rules, borrowed from Gas City's attention registry and taken further:

- one item per problem, keyed by a stable id; raising it again updates the
  item rather than adding a second;
- priority is ``critical`` > ``high`` > ``normal`` > ``low``, and an item that
  has waited past its ``escalate_after_s`` counts one level higher -- a
  question nobody answered stops being quiet;
- order is effective priority, then oldest first;
- an item belongs to the process that raised it when its answer lives in that
  process (a pending approval's future): if that process is gone, nobody can
  answer it any more, and :meth:`AttentionQueue.reap` closes it as orphaned
  instead of leaving it on top of the queue forever.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.atomic_io import atomic_replace, write_transaction

PRIORITIES = ("critical", "high", "normal", "low")
_RANK = {p: i for i, p in enumerate(PRIORITIES)}
#: Resolved items are kept this long, for the record, then dropped.
KEEP_RESOLVED_S = 7 * 24 * 3600


@dataclass
class AttentionItem:
    id: str
    kind: str
    priority: str
    title: str
    detail: str = ""
    session_key: str = ""
    source: str = ""
    created: float = 0.0
    updated: float = 0.0
    escalate_after_s: float | None = None
    owner_pid: int | None = None
    status: str = "open"
    resolution: str = ""
    resolved: float | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def effective_priority(self, now: float) -> str:
        """The priority it counts at now: one level up once it has waited past its escalation age."""
        rank = _RANK.get(self.priority, _RANK["normal"])
        if self.escalate_after_s is not None and now - self.created >= self.escalate_after_s and rank > 0:
            rank -= 1
        return PRIORITIES[rank]

    def to_json(self, now: float | None = None) -> dict[str, Any]:
        out = asdict(self)
        out["effective_priority"] = self.effective_priority(time.time() if now is None else now)
        return out


def default_path() -> Path:
    from raven.home import raven_home

    return raven_home() / "attention.json"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class AttentionQueue:
    def __init__(self, path: Path | None = None, *, pid_alive: Any = _pid_alive) -> None:
        self.path = path or default_path()
        self._alive = pid_alive

    # -- storage -------------------------------------------------------------

    def _load(self) -> list[AttentionItem]:
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        known = set(AttentionItem.__dataclass_fields__)
        return [AttentionItem(**{k: v for k, v in row.items() if k in known}) for row in rows if isinstance(row, dict)]

    def _store(self, items: list[AttentionItem], now: float) -> None:
        keep = [i for i in items if i.status == "open" or (i.resolved or now) > now - KEEP_RESOLVED_S]
        atomic_replace(self.path, json.dumps([asdict(i) for i in keep], ensure_ascii=False, indent=2))

    def _transaction(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return write_transaction(self.path)

    # -- verbs ---------------------------------------------------------------

    def raise_item(
        self,
        id: str,
        *,
        kind: str,
        priority: str = "normal",
        title: str,
        detail: str = "",
        session_key: str = "",
        source: str = "",
        escalate_after_s: float | None = None,
        owned: bool = False,
        data: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> AttentionItem:
        """Add a problem, or update the open one with this id (keeping when it was first raised)."""
        if priority not in _RANK:
            raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}, not {priority!r}")
        when = time.time() if now is None else now
        announce = False
        with self._transaction():
            items = self._load()
            current = next((i for i in items if i.id == id and i.status == "open"), None)
            if current is None:
                current = AttentionItem(id=id, kind=kind, priority=priority, title=title, created=when)
                items.append(current)
                announce = True
            elif _RANK[priority] < _RANK.get(current.priority, 2):
                current.priority = priority
                announce = True  # it got worse: worth hearing about again
            current.title, current.detail = title, detail or current.detail
            current.session_key = session_key or current.session_key
            current.source = source or current.source
            current.escalate_after_s = escalate_after_s
            current.owner_pid = os.getpid() if owned else None
            current.data = {**current.data, **(data or {})}
            current.updated = when
            self._store(items, when)
        if announce:
            self._announce(current)
        return current

    def _announce(self, item: AttentionItem) -> None:
        """A new (or worse) problem is an event for the operator's notification rules."""
        try:
            from raven.proactive_engine import notify

            notify.emit(
                notify.Event(
                    kind=item.kind,
                    priority=item.priority,
                    title=item.title,
                    key=item.id,
                    detail=item.detail,
                    session_key=item.session_key,
                    source=item.source,
                )
            )
        except Exception as exc:  # noqa: BLE001 - the queue already holds it; a notification is extra
            logger.debug("attention: could not announce {}: {}", item.id, exc)

    def resolve(self, id: str, resolution: str = "resolved", *, now: float | None = None) -> bool:
        """Close the open item with this id; False when there is none."""
        when = time.time() if now is None else now
        with self._transaction():
            items = self._load()
            hit = False
            for item in items:
                if item.id == id and item.status == "open":
                    item.status, item.resolution, item.resolved, item.updated = "resolved", resolution, when, when
                    hit = True
            if hit:
                self._store(items, when)
        return hit

    def reap(self, *, now: float | None = None) -> list[str]:
        """Close the items whose owning process has died: nobody can answer them any more."""
        when = time.time() if now is None else now
        closed: list[str] = []
        with self._transaction():
            items = self._load()
            for item in items:
                if item.status == "open" and item.owner_pid is not None and not self._alive(item.owner_pid):
                    item.status, item.resolution, item.resolved, item.updated = "resolved", "orphaned", when, when
                    closed.append(item.id)
            if closed:
                self._store(items, when)
        return closed

    def items(
        self, *, session_key: str | None = None, include_resolved: bool = False, now: float | None = None
    ) -> list[AttentionItem]:
        """Open items (and resolved ones if asked), most pressing first, then oldest first."""
        when = time.time() if now is None else now
        rows = [
            i
            for i in self._load()
            if (include_resolved or i.status == "open") and (session_key is None or i.session_key == session_key)
        ]
        return sorted(rows, key=lambda i: (i.status != "open", _RANK[i.effective_priority(when)], i.created))

    def summary(self, *, now: float | None = None) -> dict[str, int]:
        when = time.time() if now is None else now
        counts = dict.fromkeys(PRIORITIES, 0)
        for item in self.items(now=when):
            counts[item.effective_priority(when)] += 1
        return counts


_queue: AttentionQueue | None = None


def queue() -> AttentionQueue:
    """The queue under this raven home (re-resolved if the home moved, as tests move it)."""
    global _queue
    path = default_path()
    if _queue is None or _queue.path != path:
        _queue = AttentionQueue(path)
    return _queue


def raise_quietly(id: str, **kw: Any) -> None:
    """``raise_item`` for a caller whose own job must not fail because the queue could not be written."""
    with suppress(Exception):
        queue().raise_item(id, **kw)
        return
    logger.debug("attention: could not record {}", id)


def resolve_quietly(id: str, resolution: str = "resolved") -> None:
    try:
        queue().resolve(id, resolution)
    except Exception as exc:  # noqa: BLE001 - the queue is a view; the request it mirrors already ended
        logger.debug("attention: could not resolve {}: {}", id, exc)


__all__ = [
    "PRIORITIES",
    "AttentionItem",
    "AttentionQueue",
    "queue",
    "raise_quietly",
    "resolve_quietly",
]

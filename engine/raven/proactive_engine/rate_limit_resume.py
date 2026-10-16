"""Continuing a conversation by itself once the model's rate limit lifts.

A turn the provider's rate limit stopped, with the provider saying when it
lifts (``ErrorClassification.retry_after_s``) and ``agents.defaults.
rateLimitResume`` on, leaves an entry here: which conversation, where its
replies go, when, and what was being asked. The gateway's resumer submits a
turn into that same conversation when the time comes -- a system turn, like a
supersede notice, so the conversation's own history carries the work forward
and the reply reaches the channel the user was talking on.

One pending resume per conversation (a later limit replaces it), none further
ahead than a day, and at most :data:`MAX_CHAIN` in a row: a resumed turn that
hits the limit again schedules once more, then stops and says so.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.atomic_io import atomic_replace, write_transaction

MAX_AHEAD_S = 24 * 3600
MARGIN_S = 30
MAX_CHAIN = 3
SENDER = "rate_limit_resume"
"""The sender of a resume turn, which is how a resumed turn knows it is one."""
TICK_S = 15.0


def default_path() -> Path:
    from raven.home import raven_home

    return raven_home() / "rate_limit_resume.json"


@dataclass
class PendingResume:
    id: str
    conversation: str
    channel: str
    chat_id: str
    due_at: float
    request: str
    created_at: float
    chain: int = 1
    """How many resumes in a row this conversation has had, this one included."""


class ResumeStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()

    def _read(self) -> list[PendingResume]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        known = set(PendingResume.__dataclass_fields__)
        return [PendingResume(**{k: v for k, v in row.items() if k in known}) for row in raw if isinstance(row, dict)]

    def _write(self, entries: list[PendingResume]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(self.path, json.dumps([asdict(e) for e in entries], ensure_ascii=False, indent=2))

    def pending(self) -> list[PendingResume]:
        return sorted(self._read(), key=lambda e: e.due_at)

    def put(self, entry: PendingResume) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.path):
            self._write([e for e in self._read() if e.conversation != entry.conversation] + [entry])

    def take_due(self, now: float) -> list[PendingResume]:
        if not self.path.exists():
            return []
        with write_transaction(self.path):
            entries = self._read()
            due = [e for e in entries if e.due_at <= now]
            if due:
                self._write([e for e in entries if e.due_at > now])
        return due

    def cancel(self, conversation: str) -> bool:
        if not self.path.exists():
            return False
        with write_transaction(self.path):
            entries = self._read()
            kept = [e for e in entries if e.conversation != conversation]
            if len(kept) != len(entries):
                self._write(kept)
                return True
        return False


def schedule(
    *,
    conversation: str,
    channel: str,
    chat_id: str,
    delay_s: float,
    request: str,
    previous_chain: int = 0,
    now: float | None = None,
    store: ResumeStore | None = None,
) -> PendingResume | None:
    """Leave a resume for ``conversation`` when the limit lifts; None when it is too far off or the chain is spent."""
    now = time.time() if now is None else now
    if delay_s > MAX_AHEAD_S or previous_chain >= MAX_CHAIN:
        return None
    entry = PendingResume(
        id=uuid.uuid4().hex[:12],
        conversation=conversation,
        channel=channel,
        chat_id=chat_id,
        due_at=now + delay_s + MARGIN_S,
        request=(request or "")[:2000],
        created_at=now,
        chain=previous_chain + 1,
    )
    (store or ResumeStore()).put(entry)
    return entry


def resume_text(entry: PendingResume) -> str:
    stopped = time.strftime("%H:%M", time.localtime(entry.created_at))
    return (
        f"[Resumed by Raven, {entry.chain} of {MAX_CHAIN}] The model provider's rate limit stopped this "
        f"conversation at {stopped}; it has lifted now. Continue the work that was interrupted, from where it "
        f"stood. The request that was being worked on:\n\n{entry.request}\n\nIf it is already complete, say so "
        "in a sentence."
    )


_CHAIN = re.compile(r"^\[Resumed by Raven, (\d+) of \d+\]")


def chain_of(sender_id: str, text: str) -> int:
    """How many resumes in a row led to this turn: 0 for a turn a person or anything else sent."""
    if sender_id != SENDER:
        return 0
    match = _CHAIN.match(text or "")
    return int(match.group(1)) if match else 0


class RateLimitResumer:
    """The gateway's side: submits each resume when it falls due."""

    def __init__(self, submit: Any, store: ResumeStore | None = None, *, tick_s: float = TICK_S) -> None:
        self.submit = submit
        self.store = store or ResumeStore()
        self.tick_s = tick_s
        self._task: asyncio.Task | None = None

    def resume_due(self, now: float | None = None) -> list[PendingResume]:
        from raven.spine import ChatType, Origin, Source, TurnRequest

        due = self.store.take_due(time.time() if now is None else now)
        for entry in due:
            if time.time() - entry.due_at > MAX_AHEAD_S:
                logger.info("rate-limit resume for {} is a day stale; dropped", entry.conversation)
                continue
            logger.info("rate limit lifted: resuming {} (resume {} in a row)", entry.conversation, entry.chain)
            self.submit(
                TurnRequest(
                    origin=Origin.SENTINEL,
                    source=Source(
                        channel=entry.channel, chat_id=entry.chat_id, sender_id=SENDER, chat_type=ChatType.DM
                    ),
                    text=resume_text(entry),
                    conversation=entry.conversation,
                )
            )
        return due

    async def _loop(self) -> None:
        while True:
            try:
                self.resume_due()
            except Exception:  # noqa: BLE001 -- one bad pass is logged; the next one runs
                logger.exception("rate-limit resume pass failed")
            await asyncio.sleep(self.tick_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="rate-limit-resume")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


__all__ = [
    "MAX_CHAIN",
    "SENDER",
    "PendingResume",
    "RateLimitResumer",
    "ResumeStore",
    "chain_of",
    "default_path",
    "resume_text",
    "schedule",
]

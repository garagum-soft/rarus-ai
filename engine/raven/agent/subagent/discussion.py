"""Group technical discussions between agents, with a saved protocol.

Several agents take turns in one thread. A discussion runs in rounds: every
participant speaks once per round, in a fixed order, and each speaker is shown
the whole thread so far. It stops at the round limit, or earlier when every
participant of one round ends with the consensus marker, or when a whole round
failed. What was said is kept as a protocol: a JSON record plus a Markdown
rendering, written atomically beside the session's other sub-agent records.

The speaker is injected as a coroutine so the loop knows nothing about
transport; ``DiscussTool`` binds it to ``SubagentManager.chat``.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from raven.utils.atomic_io import atomic_replace

CONSENSUS_MARKER = "[[AGREED]]"
MAX_ROUNDS_CEILING = 10
DEFAULT_MAX_ROUNDS = 3
MIN_PARTICIPANTS = 2

Status = Literal["round_limit", "consensus", "failed"]
Speak = Callable[[str, str], Awaitable[str]]

_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class DiscussionTurn:
    round: int
    speaker: str
    text: str
    error: bool = False


@dataclass
class DiscussionProtocol:
    discussion_id: str
    topic: str
    participants: list[str]
    max_rounds: int
    turns: list[DiscussionTurn] = field(default_factory=list)
    status: Status = "round_limit"
    rounds_run: int = 0
    created_at: float = field(default_factory=time.time)

    def render(self) -> str:
        lines = [
            f"# Discussion {self.discussion_id}",
            "",
            f"Topic: {self.topic}",
            f"Participants: {', '.join(self.participants)}",
            f"Rounds: {self.rounds_run} of {self.max_rounds}; ended by {self.status}",
        ]
        current = 0
        for turn in self.turns:
            if turn.round != current:
                current = turn.round
                lines += ["", f"## Round {current}"]
            tag = " (failed)" if turn.error else ""
            lines += ["", f"**{turn.speaker}**{tag}:", turn.text.strip()]
        return "\n".join(lines) + "\n"


def _thread_text(protocol: DiscussionProtocol) -> str:
    if not protocol.turns:
        return "(nobody has spoken yet)"
    return "\n\n".join(f"[round {t.round}] {t.speaker}: {t.text.strip()}" for t in protocol.turns)


def _prompt(protocol: DiscussionProtocol, round_no: int, speaker: str) -> str:
    return (
        f"You are {speaker} in a group technical discussion with "
        f"{', '.join(p for p in protocol.participants if p != speaker)}.\n"
        f"Topic: {protocol.topic}\n"
        f"This is round {round_no} of at most {protocol.max_rounds}.\n\n"
        f"The thread so far:\n{_thread_text(protocol)}\n\n"
        "Reply with your next contribution only: answer what others raised, add what is missing, "
        f"and be concrete. If you agree the discussion has reached a conclusion, end your reply with {CONSENSUS_MARKER}."
    )


def _clean_participants(participants: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for name in participants:
        name = name.strip()
        if name and name not in seen:
            seen.append(name)
    if len(seen) < MIN_PARTICIPANTS:
        raise ValueError(f"a discussion needs at least {MIN_PARTICIPANTS} distinct participants")
    return seen


async def run_discussion(
    topic: str,
    participants: Sequence[str],
    speak: Speak,
    *,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    discussion_id: str | None = None,
    on_turn: Callable[[DiscussionTurn], Awaitable[None]] | None = None,
) -> DiscussionProtocol:
    """Run the rounds and return the protocol; never raises for a speaker's failure."""
    topic = topic.strip()
    if not topic:
        raise ValueError("a discussion needs a topic")
    if not 1 <= max_rounds <= MAX_ROUNDS_CEILING:
        raise ValueError(f"max_rounds must be between 1 and {MAX_ROUNDS_CEILING}")
    discussion_id = discussion_id or uuid.uuid4().hex[:8]
    if not _ID.match(discussion_id):
        raise ValueError("discussion_id may hold only letters, digits, '_' and '-'")
    protocol = DiscussionProtocol(
        discussion_id=discussion_id,
        topic=topic,
        participants=_clean_participants(participants),
        max_rounds=max_rounds,
    )
    for round_no in range(1, max_rounds + 1):
        round_turns: list[DiscussionTurn] = []
        for speaker in protocol.participants:
            try:
                reply = await speak(speaker, _prompt(protocol, round_no, speaker))
                turn = DiscussionTurn(round_no, speaker, str(reply))
            except Exception as exc:
                turn = DiscussionTurn(round_no, speaker, f"{type(exc).__name__}: {exc}", error=True)
            protocol.turns.append(turn)
            round_turns.append(turn)
            if on_turn is not None:
                await on_turn(turn)
        protocol.rounds_run = round_no
        if all(t.error for t in round_turns):
            protocol.status = "failed"
            break
        if all(not t.error and t.text.rstrip().endswith(CONSENSUS_MARKER) for t in round_turns):
            protocol.status = "consensus"
            break
    else:
        protocol.status = "round_limit"
    return protocol


class ProtocolStore:
    """Writes and reads protocols under one directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _json(self, discussion_id: str) -> Path:
        if not _ID.match(discussion_id):
            raise ValueError("bad discussion id")
        return self.root / f"{discussion_id}.json"

    def save(self, protocol: DiscussionProtocol) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._json(protocol.discussion_id)
        atomic_replace(path, json.dumps(asdict(protocol), ensure_ascii=False, indent=2))
        atomic_replace(path.with_suffix(".md"), protocol.render())
        return path

    def load(self, discussion_id: str) -> DiscussionProtocol:
        data = json.loads(self._json(discussion_id).read_text(encoding="utf-8"))
        data["turns"] = [DiscussionTurn(**t) for t in data["turns"]]
        return DiscussionProtocol(**data)

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json")) if self.root.is_dir() else []


__all__ = [
    "CONSENSUS_MARKER",
    "DEFAULT_MAX_ROUNDS",
    "MAX_ROUNDS_CEILING",
    "DiscussionProtocol",
    "DiscussionTurn",
    "ProtocolStore",
    "run_discussion",
]

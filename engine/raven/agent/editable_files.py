"""The agent's instruction and memory files a person may edit from the web page.

A fixed list, never a path the caller names: the page edits what the agent is
told every turn (``soul.md``, ``agent.md``, ``TOOLS.md``) and what it remembers
about the person (``user.md``, ``attention.md``, ``behaviors.md``, the
``HEARTBEAT.md`` task list) -- and nothing else under agent home.

Every save is checked and recorded:

- **Against a lost update.** The caller sends the hash of the text it started
  from; a file that changed since (the agent edited it, the consolidator
  rewrote the profile) is refused with the current hash, so nobody's change is
  overwritten unseen.
- **Under the writers' own lock** where the file has one: ``user.md``,
  ``attention.md`` and ``behaviors.md`` are rewritten by the consolidator,
  the personaliser and the sentinel under their sidecar locks, and a save here
  takes the same lock.
- **As a revision** in the instruction history (``raven.agent.instructions``),
  so ``rarus instructions``-style log, diff and rollback work for every file
  the page can change. A change made on disk since the last revision is
  recorded first, so the history never skips a version the file really had.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from raven.agent.instructions import InstructionError, InstructionHistory

Kind = Literal["instruction", "memory"]

MAX_BYTES = 256 * 1024
"""A file the prompt renders every turn; a quarter megabyte is already far past useful."""


@dataclass(frozen=True)
class EditableFile:
    key: str
    kind: Kind
    lock: str | None = None
    """The sidecar lock the file's other writers hold, relative to agent home."""


EDITABLE: tuple[EditableFile, ...] = (
    EditableFile("agent_memory/profile/soul.md", "instruction"),
    EditableFile("agent_memory/profile/agent.md", "instruction"),
    EditableFile("TOOLS.md", "instruction"),
    EditableFile("user_memory/profile/user.md", "memory", lock="user_memory/profile/user.md.lock"),
    EditableFile("user_memory/attention.md", "memory", lock="user_memory/attention.md.lock"),
    EditableFile("user_memory/behaviors.md", "memory", lock="user_memory/behaviors.md.lock"),
    EditableFile("HEARTBEAT.md", "memory"),
)
_BY_KEY = {f.key: f for f in EDITABLE}


class EditError(ValueError):
    """A file that is not editable, or a text that cannot be saved."""


class EditConflictError(Exception):
    """The file changed since the caller read it."""

    def __init__(self, key: str, current_sha256: str) -> None:
        super().__init__(f"{key} changed since it was opened; reload it and apply the edit again")
        self.key = key
        self.current_sha256 = current_sha256


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file(key: str) -> EditableFile:
    found = _BY_KEY.get(key)
    if found is None:
        raise EditError(f"{key!r} is not an editable file; editable: {', '.join(_BY_KEY)}")
    return found


def history(workspace: Path) -> InstructionHistory:
    return InstructionHistory(workspace, tracked=tuple(_BY_KEY))


def _read(workspace: Path, key: str) -> str:
    path = Path(workspace) / key
    return path.read_text(encoding="utf-8") if path.is_file() else ""


@contextlib.contextmanager
def _held(workspace: Path, spec: EditableFile) -> Iterator[None]:
    if spec.lock is None:
        yield
        return
    from raven.utils.portable_lock import file_lock

    lock_path = Path(workspace) / spec.lock
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(lock_path):
        yield


def _revisions(hist: InstructionHistory, key: str, limit: int = 30) -> list[dict]:
    rows = [asdict(r) for r in hist.log(key)]
    return list(reversed(rows))[:limit]


def describe(workspace: Path) -> list[dict]:
    """Every editable file: what it is, whether it exists, its hash and how many revisions it has."""
    hist = history(workspace)
    out = []
    for spec in EDITABLE:
        path = Path(workspace) / spec.key
        text = _read(workspace, spec.key)
        out.append(
            {
                "key": spec.key,
                "kind": spec.kind,
                "exists": path.is_file(),
                "size": len(text.encode("utf-8")),
                "sha256": sha256(text),
                "head": hist.head(spec.key),
                "revisions": len(hist.log(spec.key)),
            }
        )
    return out


def read(workspace: Path, key: str) -> dict:
    spec = _file(key)
    text = _read(workspace, spec.key)
    hist = history(workspace)
    return {
        "key": spec.key,
        "kind": spec.kind,
        "text": text,
        "sha256": sha256(text),
        "head": hist.head(spec.key),
        "revisions": _revisions(hist, spec.key),
    }


def save(
    workspace: Path,
    key: str,
    text: str,
    *,
    expected_sha256: str,
    author: str = "web",
    note: str = "",
) -> dict:
    """Write ``text`` if the file still holds what the caller read; record it as a revision."""
    spec = _file(key)
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise EditError(f"{key} would be {len(text.encode('utf-8'))} bytes; the limit is {MAX_BYTES}")
    if "\x00" in text:
        raise EditError("the text holds a NUL character")
    hist = history(workspace)
    with _held(workspace, spec):
        current = _read(workspace, spec.key)
        if sha256(current) != expected_sha256:
            raise EditConflictError(spec.key, sha256(current))
        if current == text:
            return {"key": spec.key, "sha256": sha256(text), "head": hist.head(spec.key), "saved": False}
        if hist.log(spec.key):
            # Changed on disk since the last revision (the agent, the consolidator):
            # kept, so the history holds every version the file really had.
            hist.record(spec.key, author="disk", note="changed outside the editor")
        revision = hist.save(spec.key, text, author=author, note=note)
    return {
        "key": spec.key,
        "sha256": sha256(text),
        "head": revision.rev if revision is not None else hist.head(spec.key),
        "saved": True,
    }


def revision_text(workspace: Path, key: str, rev: int) -> str:
    spec = _file(key)
    try:
        return history(workspace).text_at(spec.key, rev)
    except InstructionError as exc:
        raise EditError(str(exc)) from None


def diff(workspace: Path, key: str, a: int, b: int | None = None) -> str:
    spec = _file(key)
    try:
        return history(workspace).diff(spec.key, a, b)
    except InstructionError as exc:
        raise EditError(str(exc)) from None


def rollback(workspace: Path, key: str, rev: int, *, expected_sha256: str, author: str = "web") -> dict:
    """Put revision ``rev`` back, if the file still holds what the caller read."""
    spec = _file(key)
    hist = history(workspace)
    with _held(workspace, spec):
        current = _read(workspace, spec.key)
        if sha256(current) != expected_sha256:
            raise EditConflictError(spec.key, sha256(current))
        try:
            hist.text_at(spec.key, rev)
        except InstructionError as exc:
            raise EditError(str(exc)) from None
        hist.record(spec.key, author="disk", note="the text before the rollback")
        revision = hist.rollback(spec.key, rev, author=author)
    restored = _read(workspace, spec.key)
    return {"key": spec.key, "sha256": sha256(restored), "head": revision.rev, "saved": True}


__all__ = [
    "EDITABLE",
    "MAX_BYTES",
    "EditConflictError",
    "EditError",
    "EditableFile",
    "describe",
    "diff",
    "history",
    "read",
    "revision_text",
    "rollback",
    "save",
    "sha256",
]

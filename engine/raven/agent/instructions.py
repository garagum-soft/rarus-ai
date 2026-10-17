"""Revisions of the agent's instruction files, with a head, a log and rollback.

The instruction files are the workspace files every turn renders into the
prompt (``agent_memory/profile/soul.md``, ``agent_memory/profile/agent.md``,
``TOOLS.md``). Each recorded state of one of them is a numbered revision kept
under ``<workspace>/.instructions/<key>/``; ``HEAD`` names the revision the file
holds, and ``log.jsonl`` says who changed it, when and why. A rollback never
rewrites history: it writes the old text back and records that as a new
revision, so the log keeps both the change and its undoing, and a reviewer can
diff any two revisions.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from raven.utils.atomic_io import atomic_replace

#: The files a turn renders as the agent's instructions, relative to the workspace.
INSTRUCTION_FILES: tuple[str, ...] = (
    "agent_memory/profile/soul.md",
    "agent_memory/profile/agent.md",
    "TOOLS.md",
)

STORE_DIR = ".instructions"


class InstructionError(ValueError):
    """A path that is not an instruction file, or a revision that does not exist."""


@dataclass(frozen=True)
class Revision:
    rev: int
    at: float
    author: str
    note: str
    sha256: str
    action: str = "save"
    restored_from: int | None = None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class InstructionHistory:
    """Revisions of every instruction file in one workspace.

    ``tracked`` widens or narrows which workspace files keep revisions; the
    default is the instruction files, which is what the agent's own file tools
    and ``rarus instructions`` keep. The web editor passes its larger set
    (``raven.agent.editable_files``) so a memory file saved there is
    revisioned in the same store.
    """

    def __init__(self, workspace: Path, *, tracked: tuple[str, ...] = INSTRUCTION_FILES) -> None:
        self.workspace = Path(workspace)
        self.root = self.workspace / STORE_DIR
        self.tracked = tracked

    def key_of(self, path: str | Path) -> str:
        """The instruction file a path names, as its workspace-relative spelling."""
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                candidate = candidate.resolve().relative_to(self.workspace.resolve())
            except ValueError:
                raise InstructionError(f"{path} is not inside the workspace") from None
        rel = candidate.as_posix()
        if rel not in self.tracked:
            raise InstructionError(f"{rel} is not an instruction file; tracked: {', '.join(self.tracked)}")
        return rel

    def is_tracked(self, path: str | Path) -> bool:
        try:
            self.key_of(path)
        except InstructionError:
            return False
        return True

    def _dir(self, key: str) -> Path:
        return self.root / key.replace("/", "__")

    def head(self, path: str | Path) -> int:
        """The revision the file holds, 0 when none was ever recorded."""
        marker = self._dir(self.key_of(path)) / "HEAD"
        return int(marker.read_text(encoding="utf-8").strip() or 0) if marker.is_file() else 0

    def log(self, path: str | Path) -> list[Revision]:
        journal = self._dir(self.key_of(path)) / "log.jsonl"
        if not journal.is_file():
            return []
        return [Revision(**json.loads(line)) for line in journal.read_text(encoding="utf-8").splitlines() if line]

    def text_at(self, path: str | Path, rev: int) -> str:
        stored = self._dir(self.key_of(path)) / f"{rev:04d}.md"
        if not stored.is_file():
            raise InstructionError(f"{self.key_of(path)} has no revision {rev}")
        return stored.read_text(encoding="utf-8")

    def _append(self, key: str, text: str, revision: Revision) -> None:
        folder = self._dir(key)
        folder.mkdir(parents=True, exist_ok=True)
        atomic_replace(folder / f"{revision.rev:04d}.md", text)
        with (folder / "log.jsonl").open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(asdict(revision), ensure_ascii=False) + "\n")
        atomic_replace(folder / "HEAD", str(revision.rev))

    def record(self, path: str | Path, *, author: str, note: str = "") -> Revision | None:
        """Record the file's current text as a revision; ``None`` when it equals the head."""
        key = self.key_of(path)
        current = self.workspace / key
        text = current.read_text(encoding="utf-8") if current.is_file() else ""
        entries = self.log(key)
        if entries and entries[-1].sha256 == _digest(text):
            return None
        revision = Revision(
            rev=(entries[-1].rev if entries else 0) + 1,
            at=time.time(),
            author=author,
            note=note,
            sha256=_digest(text),
        )
        self._append(key, text, revision)
        return revision

    def save(self, path: str | Path, text: str, *, author: str, note: str = "") -> Revision | None:
        """Write new instructions and record them. The prior text is recorded first if it never was."""
        key = self.key_of(path)
        target = self.workspace / key
        if target.is_file() and not self.log(key):
            self.record(key, author="baseline", note="the text before revisions were kept")
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(target, text)
        return self.record(key, author=author, note=note)

    def rollback(self, path: str | Path, rev: int, *, author: str, note: str = "") -> Revision:
        """Put revision ``rev`` back in the file, recorded as a new revision."""
        key = self.key_of(path)
        old = self.text_at(key, rev)
        target = self.workspace / key
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(target, old)
        entries = self.log(key)
        revision = Revision(
            rev=entries[-1].rev + 1,
            at=time.time(),
            author=author,
            note=note or f"roll back to revision {rev}",
            sha256=_digest(old),
            action="rollback",
            restored_from=rev,
        )
        self._append(key, old, revision)
        return revision

    def diff(self, path: str | Path, a: int, b: int | None = None) -> str:
        """A unified diff from revision ``a`` to ``b`` (the head when omitted), for review."""
        key = self.key_of(path)
        b = b if b is not None else self.head(key)
        before = self.text_at(key, a).splitlines(keepends=True)
        after = self.text_at(key, b).splitlines(keepends=True)
        return "".join(difflib.unified_diff(before, after, fromfile=f"{key}@{a}", tofile=f"{key}@{b}"))


__all__ = ["INSTRUCTION_FILES", "InstructionError", "InstructionHistory", "Revision"]

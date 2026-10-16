"""Orphaned agent processes and the sessions they leave behind: found by owner and tree, ended in two phases.

Raven starts agent processes of its own -- an ACP server per pooled
connection, a CLI agent per turn -- each in a session of its own so it can be
stopped as a group. A gateway that exits cleanly stops them. One that crashes
or is killed does not, and since they were detached from its process group,
nothing else will: they keep running against the workspace, holding a model
seat, and the instance they served keeps reading ``running``.

So every such process is written down when it starts (``processes.json``
under the raven home): its pid and start time, and the pid and start time of
the process that started it. A record is an orphan when its owner is gone --
no process with that pid, or one that started at another time (a reused pid)
-- and the child is still the same process. Its tree is collected by parent
pid; the sweeping process and its ancestors are never part of it. A record
whose owner is alive is never touched, nor is one whose child already ended
(that record is just dropped).

The ending is two-phase: a polite request first (SIGTERM; a close request on
Windows), a grace period while the table is re-read, then a hard kill for
whatever is still there, then one more read to report survivors. The session
a CLI agent was serving is marked ``interrupted`` in the instance registry, so
it stops claiming to run.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils import proc_tree
from raven.utils.atomic_io import atomic_replace, write_transaction

Table = list[proc_tree.Proc]

#: (session_key, handle) of the run whose children are being started, so the
#: ledger can name the instance an orphan served. Set by the CLI backend's run.
serving: ContextVar[tuple[str, str]] = ContextVar("orphans_serving", default=("", ""))


@dataclass
class ChildRecord:
    pid: int
    started: float | None
    owner_pid: int
    owner_started: float | None
    kind: str
    command: str = ""
    session_key: str = ""
    agent: str = ""
    handle: str = ""
    recorded: float = 0.0


@dataclass
class Orphan:
    record: ChildRecord
    pids: set[int]


@dataclass
class SweepResult:
    found: list[Orphan] = field(default_factory=list)
    asked: list[int] = field(default_factory=list)
    killed: list[int] = field(default_factory=list)
    survivors: list[int] = field(default_factory=list)
    dropped: int = 0
    sessions: list[str] = field(default_factory=list)

    def report(self) -> str:
        if not self.found:
            return f"no orphaned agent processes ({self.dropped} stale record(s) dropped)"
        lines = [f"{len(self.found)} orphaned agent process tree(s)"]
        for orphan in self.found:
            r = orphan.record
            who = f" for {r.agent}/{r.handle}" if r.handle else ""
            lines.append(
                f"  pid {r.pid} ({r.kind}{who}, started by dead pid {r.owner_pid}): {len(orphan.pids)} process(es)"
            )
        lines.append(f"asked to end: {len(self.asked)}, killed after the grace period: {len(self.killed)}")
        if self.survivors:
            lines.append(f"still running: {', '.join(map(str, self.survivors))}")
        if self.sessions:
            lines.append(f"sessions marked interrupted: {', '.join(self.sessions)}")
        return "\n".join(lines)


def default_path() -> Path:
    from raven.home import raven_home

    return raven_home() / "processes.json"


class ProcessLedger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()

    def records(self) -> list[ChildRecord]:
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        known = set(ChildRecord.__dataclass_fields__)
        return [ChildRecord(**{k: v for k, v in r.items() if k in known}) for r in rows if isinstance(r, dict)]

    def _write(self, rows: list[ChildRecord]) -> None:
        atomic_replace(self.path, json.dumps([asdict(r) for r in rows], ensure_ascii=False, indent=2))

    def record(self, entry: ChildRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.path):
            rows = [r for r in self.records() if r.pid != entry.pid]
            rows.append(entry)
            self._write(rows)

    def forget(self, pids: set[int] | int) -> None:
        drop = {pids} if isinstance(pids, int) else set(pids)
        if not self.path.is_file():
            return
        with write_transaction(self.path):
            rows = self.records()
            kept = [r for r in rows if r.pid not in drop]
            if len(kept) != len(rows):
                self._write(kept)


def _self_started(table: Table | None) -> float | None:
    row = proc_tree.by_pid(table or []).get(os.getpid())
    return row.started if row else None


def note_child(
    pid: int,
    *,
    kind: str,
    command: str = "",
    session_key: str = "",
    agent: str = "",
    handle: str = "",
    ledger: ProcessLedger | None = None,
) -> None:
    """Write down a child this process just started. Never raises: bookkeeping must not fail a spawn."""
    try:
        table = proc_tree.read_table()
        index = proc_tree.by_pid(table or [])
        child = index.get(pid)
        (ledger or ProcessLedger()).record(
            ChildRecord(
                pid=pid,
                started=child.started if child else None,
                owner_pid=os.getpid(),
                owner_started=_self_started(table),
                kind=kind,
                command=command[:200],
                session_key=session_key,
                agent=agent,
                handle=handle,
                recorded=time.time(),
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("orphans: could not record child {}: {}", pid, exc)


def forget_child(pid: int, *, ledger: ProcessLedger | None = None) -> None:
    try:
        (ledger or ProcessLedger()).forget(pid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("orphans: could not forget child {}: {}", pid, exc)


def find_orphans(
    ledger: ProcessLedger, table: Table, *, me: int | None = None
) -> tuple[list[Orphan], list[ChildRecord]]:
    """(orphans, stale records): children whose owner is gone, and records whose child already ended."""
    index = proc_tree.by_pid(table)
    protected = proc_tree.ancestors(me or os.getpid(), table)
    orphans: list[Orphan] = []
    stale: list[ChildRecord] = []
    for record in ledger.records():
        child = index.get(record.pid)
        if not proc_tree.same_process(child, record.started):
            stale.append(record)
            continue
        if proc_tree.same_process(index.get(record.owner_pid), record.owner_started):
            continue  # its owner is alive: not ours to judge
        pids = proc_tree.descendants(record.pid, table) - protected
        if pids:
            orphans.append(Orphan(record=record, pids=pids))
    return orphans, stale


def sweep(
    *,
    ledger: ProcessLedger | None = None,
    read: Callable[[], Table | None] = proc_tree.read_table,
    signal_pid: Callable[..., bool] = proc_tree.signal_pid,
    mark_session: Callable[[ChildRecord], Any] | None = None,
    grace_s: float = 5.0,
    poll_s: float = 0.25,
    dry_run: bool = False,
    sleep: Callable[[float], Any] = time.sleep,
) -> SweepResult:
    """Find orphaned agent process trees and end them in two phases."""
    ledger = ledger or ProcessLedger()
    result = SweepResult()
    table = read()
    if table is None:
        logger.warning("orphans: the process table could not be read; nothing swept")
        return result
    orphans, stale = find_orphans(ledger, table)
    result.found = orphans
    if dry_run:
        return result
    if stale:
        ledger.forget({r.pid for r in stale})
        result.dropped = len(stale)
    if not orphans:
        return result
    targets = set().union(*(o.pids for o in orphans))
    # Phase one: ask. Deepest first, so a parent that would restart a child it
    # sees die has already been asked to stop by then.
    parent = {p.pid: p.ppid for p in table}

    def depth(pid: int) -> int:
        steps, seen = 0, set()
        while pid in parent and pid in targets and pid not in seen:
            seen.add(pid)
            pid = parent[pid]
            steps += 1
        return steps

    for pid in sorted(targets, key=lambda p: (-depth(p), p)):
        if signal_pid(pid, hard=False):
            result.asked.append(pid)
    deadline = time.monotonic() + grace_s
    left = targets
    while left and time.monotonic() < deadline:
        sleep(poll_s)
        now = read()
        if now is None:
            break
        left = _still_there(left, table, now)
    # Phase two: whatever did not go is ended.
    for pid in sorted(left):
        if signal_pid(pid, hard=True):
            result.killed.append(pid)
    final = read()
    result.survivors = sorted(_still_there(left, table, final)) if final is not None else sorted(left)
    ledger.forget({o.record.pid for o in orphans})
    for orphan in orphans:
        if orphan.record.handle and mark_session is not None:
            try:
                mark_session(orphan.record)
                result.sessions.append(f"{orphan.record.agent}/{orphan.record.handle}@{orphan.record.session_key}")
            except Exception as exc:  # noqa: BLE001 - the processes are gone either way
                logger.warning("orphans: could not mark {} interrupted: {}", orphan.record.handle, exc)
    return result


def _still_there(pids: set[int], before: Table, now: Table) -> set[int]:
    """The pids from ``before`` that are the same processes in ``now``."""
    was = proc_tree.by_pid(before)
    index = proc_tree.by_pid(now)
    return {pid for pid in pids if proc_tree.same_process(index.get(pid), was[pid].started if pid in was else None)}


_swept = False


def sweep_once_in_background() -> None:
    """One sweep per process, off the caller's thread: the grace period must not hold a start.

    ``RAVEN_ORPHAN_SWEEP=0`` turns it off (the test suite does: its processes
    are its own business).
    """
    global _swept
    if _swept or os.environ.get("RAVEN_ORPHAN_SWEEP") == "0":
        return
    _swept = True
    import threading

    def run() -> None:
        try:
            result = sweep(mark_session=mark_interrupted)
        except Exception as exc:  # noqa: BLE001 - a failed sweep leaves things as they were
            logger.warning("orphans: sweep failed: {}", exc)
            return
        if result.found:
            logger.warning("orphans: {}", result.report())

    threading.Thread(target=run, name="raven-orphan-sweep", daemon=True).start()


def mark_interrupted(record: ChildRecord) -> None:
    """The instance the orphan served stops reading ``running``."""
    import asyncio

    from raven.agent.subagent.instances import InstanceRegistry

    asyncio.run(InstanceRegistry().upsert_spawn(record.session_key, record.agent, record.handle, "interrupted"))


__all__ = [
    "ChildRecord",
    "Orphan",
    "ProcessLedger",
    "SweepResult",
    "find_orphans",
    "forget_child",
    "mark_interrupted",
    "note_child",
    "sweep",
]

"""The process table, read without psutil: who runs, who started them, and when.

Enough to find a process tree and to tell a process from a later one that
happens to reuse its pid. Read with ``CreateToolhelp32Snapshot`` and
``GetProcessTimes`` on Windows and with ``ps`` elsewhere. A read that fails
returns ``None`` -- "unknown", never "nobody is running": a caller deciding what
to kill must not take a failed read for an empty table.

Two rules borrowed from Superset's pty daemon (process-tree.ts) and kept here:
a tree is walked by parent pid, and the caller's own process and its ancestors
are never part of what gets signalled -- a tree member that shares a group with
the caller's shell must not take that shell down with it. Added here: a child
is only a child if its parent started before it, because the parent pid a dead
process leaves behind can be reused.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

#: Start times within this many seconds are the same process (ps rounds to a second).
SAME_START_S = 1.5


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    started: float | None
    name: str = ""
    pgid: int | None = None


# -- reading the table -------------------------------------------------------


def _read_windows() -> list[Proc] | None:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return None
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    rows: list[Proc] = []
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            rows.append(
                Proc(pid=pid, ppid=int(entry.th32ParentProcessID), started=_started_windows(pid), name=entry.szExeFile)
            )
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return rows


def _started_windows(pid: int) -> float | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 10_000_000 - 11_644_473_600  # FILETIME (1601, 100 ns) to the Unix epoch
    finally:
        kernel32.CloseHandle(handle)


def parse_ps(text: str) -> list[Proc]:
    """``ps -axo pid=,ppid=,pgid=,stat=,lstart=,comm=`` output; zombies left out (unkillable, childless)."""
    rows: list[Proc] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 9 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, ppid, pgid, stat = int(parts[0]), int(parts[1]), parts[2], parts[3]
        if stat.startswith("Z"):
            continue
        try:
            started = datetime.strptime(" ".join(parts[4:9]), "%a %b %d %H:%M:%S %Y").timestamp()
        except ValueError:
            started = None
        rows.append(
            Proc(
                pid=pid,
                ppid=ppid,
                started=started,
                name=" ".join(parts[9:]),
                pgid=int(pgid) if pgid.isdigit() else None,
            )
        )
    return rows


def _read_posix() -> list[Proc] | None:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            ["ps", "-axo", "pid=,ppid=,pgid=,stat=,lstart=,comm="],  # noqa: S607 - ps is found on PATH by design
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_ps(done.stdout) if done.returncode == 0 else None


def read_table() -> list[Proc] | None:
    """Every live process, or ``None`` when the table could not be read."""
    try:
        return _read_windows() if sys.platform == "win32" else _read_posix()
    except Exception:  # noqa: BLE001 - unknown, never empty
        return None


# -- walking it ---------------------------------------------------------------


def by_pid(table: Iterable[Proc]) -> dict[int, Proc]:
    return {p.pid: p for p in table}


def same_process(proc: Proc | None, started: float | None) -> bool:
    """Whether ``proc`` is the process that was recorded with ``started`` (not a reuse of its pid)."""
    if proc is None:
        return False
    if started is None or proc.started is None:
        return True
    return abs(proc.started - started) <= SAME_START_S


def descendants(root: int, table: Iterable[Proc]) -> set[int]:
    """``root`` and everything below it, by parent pid -- a child counts only if it started after its parent."""
    rows = list(table)
    index = by_pid(rows)
    children: dict[int, list[Proc]] = {}
    for p in rows:
        children.setdefault(p.ppid, []).append(p)
    found = {root}
    queue = [root]
    while queue:
        parent = index.get(queue.pop())
        for child in children.get(parent.pid if parent else -1, []):
            if child.pid in found or child.pid == child.ppid:
                continue
            if parent and parent.started and child.started and child.started + SAME_START_S < parent.started:
                continue  # an older process whose recorded parent pid was reused by ``parent``
            found.add(child.pid)
            queue.append(child.pid)
    return found


def ancestors(pid: int, table: Iterable[Proc]) -> set[int]:
    """``pid`` and every process above it: the chain nothing here may signal."""
    index = by_pid(table)
    chain: set[int] = set()
    cursor: int | None = pid
    while cursor and cursor not in chain:
        chain.add(cursor)
        row = index.get(cursor)
        cursor = row.ppid if row else None
    return chain


# -- signalling ---------------------------------------------------------------


def signal_pid(pid: int, *, hard: bool) -> bool:
    """Ask one process to end (``hard=False``), or end it. False when it was already gone."""
    try:
        if sys.platform == "win32":
            if hard:
                import ctypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
                if not handle:
                    return False
                try:
                    return bool(kernel32.TerminateProcess(handle, 1))
                finally:
                    kernel32.CloseHandle(handle)
            # The polite ask Windows has: a close request, which a console program
            # may ignore -- the hard pass is what guarantees the end.
            done = subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, timeout=10)  # noqa: S603,S607
            return done.returncode == 0
        os.kill(pid, signal.SIGKILL if hard else signal.SIGTERM)
        return True
    except (ProcessLookupError, OSError, subprocess.SubprocessError):
        return False


def kill_tree(pid: int) -> bool:
    """End ``pid`` and everything it started, where there is no process group to signal.

    Windows has none: ending a launcher -- ``cmd.exe`` under ``shell=True``, an
    npm ``.cmd`` shim -- leaves the program it started running, holding its
    pipes, its model slot and its workspace. So the tree is read off the process
    table by parent pid, ended from the top so nothing in it can start more, and
    read once more for a child started in between. This process and its
    ancestors are never in it. True when ``pid`` is gone afterwards.

    It never raises: it runs on the way out of a cancelled turn, where an
    exception would take the cancellation's place.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        table = read_table()
        if table is None:
            return signal_pid(pid, hard=True)
        keep = ancestors(os.getpid(), table)
        tree = descendants(pid, table) - keep
        for target in sorted(tree, key=lambda p: p != pid):
            signal_pid(target, hard=True)
        after = read_table() or []
        late = {row.pid for row in after if row.ppid in tree and row.pid not in tree} - keep
        while late:
            for target in late:
                signal_pid(target, hard=True)
            tree |= late
            late = {row.pid for row in after if row.ppid in late and row.pid not in tree} - keep
        return pid not in by_pid(read_table() or [])
    except Exception:
        return False


__all__ = [
    "Proc",
    "ancestors",
    "by_pid",
    "descendants",
    "kill_tree",
    "parse_ps",
    "read_table",
    "same_process",
    "signal_pid",
]

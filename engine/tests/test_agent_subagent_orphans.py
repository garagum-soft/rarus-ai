"""Orphaned agent processes: found by owner and tree, ended in two phases; live ones left alone."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from raven.agent.subagent.orphans import ChildRecord, ProcessLedger, find_orphans, note_child, sweep
from raven.utils.proc_tree import Proc, ancestors, descendants, parse_ps, read_table, same_process

ME = os.getpid()


def _table(*rows: tuple[int, int, float]) -> list[Proc]:
    return [Proc(pid=p, ppid=pp, started=s) for p, pp, s in rows]


def _record(pid: int, started: float, owner: int, owner_started: float, **kw) -> ChildRecord:
    return ChildRecord(pid=pid, started=started, owner_pid=owner, owner_started=owner_started, kind="cli", **kw)


@pytest.fixture
def ledger(tmp_path: Path) -> ProcessLedger:
    return ProcessLedger(tmp_path / "processes.json")


# -- the table ----------------------------------------------------------------


def test_a_tree_is_walked_by_parent_and_a_reused_parent_pid_is_not_a_parent() -> None:
    table = _table((10, 1, 100.0), (11, 10, 101.0), (12, 11, 102.0), (13, 10, 50.0))  # 13 is older than 10
    assert descendants(10, table) == {10, 11, 12}
    assert ancestors(12, table) == {12, 11, 10, 1}


def test_ps_output_is_read_and_zombies_are_left_out() -> None:
    text = (
        "  101     1   101 Ss   Mon Oct 13 10:00:00 2026 claude\n"
        "  102   101   101 Z    Mon Oct 13 10:00:01 2026 defunct\n"
        "  103   101   101 S    Mon Oct 13 10:00:02 2026 node worker.js\n"
    )
    rows = parse_ps(text)
    assert [(r.pid, r.ppid, r.pgid, r.name) for r in rows] == [
        (101, 1, 101, "claude"),
        (103, 101, 101, "node worker.js"),
    ]
    assert rows[1].started - rows[0].started == 2


def test_the_live_table_holds_this_process_and_its_start() -> None:
    table = read_table()
    assert table is not None
    me = next(p for p in table if p.pid == ME)
    assert me.started is not None and me.started <= time.time()


# -- finding orphans -----------------------------------------------------------


def test_an_orphan_is_a_live_child_whose_owner_is_gone_and_its_tree_comes_with_it(ledger: ProcessLedger) -> None:
    table = _table((ME, 1, 50.0), (20, 1, 100.0), (21, 20, 101.0), (30, 1, 100.0), (40, ME, 60.0))
    ledger.record(_record(20, 100.0, owner=999, owner_started=90.0))  # owner 999 is not running
    ledger.record(_record(40, 60.0, owner=ME, owner_started=50.0))  # owner alive: not ours to judge
    ledger.record(_record(55, 70.0, owner=999, owner_started=90.0))  # child already gone: stale

    orphans, stale = find_orphans(ledger, table, me=ME)

    assert [(o.record.pid, o.pids) for o in orphans] == [(20, {20, 21})]
    assert [r.pid for r in stale] == [55]


def test_a_reused_pid_is_neither_the_owner_nor_the_child(ledger: ProcessLedger) -> None:
    # pid 7 is running, but started long after the owner recorded as pid 7: the owner is gone.
    table = _table((ME, 1, 50.0), (7, 1, 500.0), (20, 1, 100.0), (21, 1, 900.0))
    ledger.record(_record(20, 100.0, owner=7, owner_started=90.0))
    ledger.record(_record(21, 100.0, owner=7, owner_started=90.0))  # pid 21 reused by a newer process

    orphans, stale = find_orphans(ledger, table, me=ME)

    assert [o.record.pid for o in orphans] == [20] and [r.pid for r in stale] == [21]


def test_the_sweeper_and_its_ancestors_are_never_in_a_tree(ledger: ProcessLedger) -> None:
    # A pathological record whose tree would contain this very process.
    table = _table((1, 0, 1.0), (5, 1, 10.0), (ME, 5, 20.0))
    ledger.record(_record(5, 10.0, owner=999, owner_started=5.0))
    orphans, _ = find_orphans(ledger, table, me=ME)
    assert orphans == [], "5 is this process's parent; its tree minus the protected chain is empty"


# -- ending them ---------------------------------------------------------------


def test_the_sweep_asks_first_then_kills_what_stayed_and_marks_the_session(ledger: ProcessLedger) -> None:
    # The child has the lower pid: the order must follow the tree, not the numbers.
    alive = {ME: 50.0, 20: 100.0, 15: 101.0, 40: 60.0}
    stubborn = {15}
    calls: list[tuple[int, bool]] = []

    def read() -> list[Proc]:
        parents = {20: 1, 15: 20, 40: ME, ME: 1}
        return [Proc(pid=p, ppid=parents[p], started=s) for p, s in alive.items()]

    def signal_pid(pid: int, *, hard: bool) -> bool:
        calls.append((pid, hard))
        if hard or pid not in stubborn:
            alive.pop(pid, None)
        return True

    ledger.record(_record(20, 100.0, owner=999, owner_started=90.0, session_key="tui:1", agent="codex", handle="fix"))
    ledger.record(_record(40, 60.0, owner=ME, owner_started=50.0))
    marked: list[str] = []

    result = sweep(
        ledger=ledger,
        read=read,
        signal_pid=signal_pid,
        mark_session=lambda r: marked.append(r.handle),
        grace_s=0.05,
        poll_s=0.01,
    )

    assert [c for c in calls if not c[1]] == [(15, False), (20, False)], "phase one: every member asked, deepest first"
    assert [c for c in calls if c[1]] == [(15, True)], "phase two: only what stayed"
    assert 40 in alive and all(pid != 40 for pid, _ in calls), "a live owner's child is never signalled"
    assert result.survivors == [] and marked == ["fix"] and [r.pid for r in ledger.records()] == [40]


def test_a_dry_run_changes_nothing(ledger: ProcessLedger) -> None:
    ledger.record(_record(20, 100.0, owner=999, owner_started=90.0))
    result = sweep(
        ledger=ledger,
        read=lambda: _table((ME, 1, 50.0), (20, 1, 100.0)),
        signal_pid=lambda pid, hard: pytest.fail("a dry run signalled"),
        dry_run=True,
    )
    assert [o.record.pid for o in result.found] == [20] and len(ledger.records()) == 1


def test_an_unreadable_table_sweeps_nothing(ledger: ProcessLedger) -> None:
    ledger.record(_record(20, 100.0, owner=999, owner_started=90.0))
    result = sweep(ledger=ledger, read=lambda: None, signal_pid=lambda pid, hard: pytest.fail("signalled blind"))
    assert result.found == [] and len(ledger.records()) == 1


@pytest.mark.slow  # real processes: a dead owner, an orphan with a grandchild, and a live owner's child
def test_real_orphaned_processes_are_ended_and_a_live_ones_child_is_not(ledger: ProcessLedger, tmp_path: Path) -> None:
    py = sys.executable
    # An owner that has already exited: its pid and start are recorded, then it is gone.
    gone = subprocess.Popen([py, "-c", "pass"])
    gone_started = next((p.started for p in read_table() or [] if p.pid == gone.pid), time.time())
    gone.wait()
    # The orphan: a process that starts a grandchild and then sleeps.
    marker = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time, pathlib; "
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
        f"pathlib.Path(r'{marker}').write_text(str(c.pid)); time.sleep(120)"
    )
    orphan = subprocess.Popen([py, "-c", code])
    keep = subprocess.Popen([py, "-c", "import time; time.sleep(120)"])
    try:
        deadline = time.time() + 30
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.1)
        grandchild = int(marker.read_text())
        table = read_table() or []
        started = {p.pid: p.started for p in table}
        ledger.record(_record(orphan.pid, started[orphan.pid], owner=gone.pid, owner_started=gone_started))
        note_child(keep.pid, kind="cli", ledger=ledger)  # owned by this live process

        result = sweep(ledger=ledger, grace_s=2.0)

        after = {p.pid: p for p in read_table() or []}
        assert not same_process(after.get(orphan.pid), started[orphan.pid])
        assert not same_process(after.get(grandchild), started.get(grandchild))
        assert keep.poll() is None, "the live owner's child is still running"
        assert set(result.asked) | set(result.killed) >= {orphan.pid, grandchild} and result.survivors == []
    finally:
        for p in (orphan, keep):
            if p.poll() is None:
                p.kill()
        if marker.exists():
            pid = int(marker.read_text())
            from raven.utils.proc_tree import signal_pid

            signal_pid(pid, hard=True)


async def test_a_cli_agent_run_is_in_the_ledger_while_it_runs_and_gone_after(tmp_path: Path) -> None:
    from raven.agent.subagent import orphans
    from raven.agent.subagent.backends.cli_agent import CliAgentBackend
    from raven.agent.subagent.instances import InstanceRegistry

    ledger = orphans.ProcessLedger()
    script = tmp_path / "agent.py"
    script.write_text(
        "import json, pathlib, sys\n"
        f"rows = json.loads(pathlib.Path(r'{ledger.path}').read_text())\n"
        "print(';'.join(f\"{r['kind']}:{r['agent']}:{r['handle']}:{r['session_key']}\" for r in rows))\n",
        encoding="utf-8",
    )
    py = sys.executable.replace("\\", "/")
    backend = CliAgentBackend(
        name="writer", command=f'"{py}" "{script.as_posix()}"', registry=InstanceRegistry(path=tmp_path / "inst.json")
    )

    seen = await backend.run(
        "task", task_id="t1", workspace=tmp_path, executor=None, session_key="tui:9", instance="fixer"
    )

    assert seen.strip() == "cli:writer:fixer:tui:9", "the running child saw itself in the ledger"
    assert ledger.records() == [], "and the record is gone once it ended"

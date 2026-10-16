"""The project watch: failing checks and red CI are found and filed as tasks with nobody asking."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.proactive_engine.attention import AttentionQueue
from raven.stint.project_watch import (
    ProjectWatch,
    WatchCheck,
    WatchedProject,
    WatchStore,
    file_findings,
    record_scan,
    scan,
)
from raven.stint import backlog as backlog_mod

PY = sys.executable.replace("\\", "/")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "state.txt").write_text("broken", encoding="utf-8")
    (root / "check_tests.py").write_text(
        "import pathlib, sys\n"
        "if pathlib.Path('state.txt').read_text() == 'broken':\n"
        "    print('collected 12 items'); print('2 failed, 10 passed', file=sys.stderr); sys.exit(1)\n"
        "print('12 passed')\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _watched(project: Path, **over) -> WatchedProject:
    checks = [
        WatchCheck(name="tests", run=f'"{PY}" check_tests.py', timeout_s=60),
        WatchCheck(name="lint", run=f'"{PY}" -c "pass"', timeout_s=60),
    ]
    return WatchedProject(path=str(project), checks=checks, **over)


@pytest.fixture
def queue(tmp_path: Path) -> AttentionQueue:
    return AttentionQueue(tmp_path / "attention.json")


def test_a_failing_check_is_filed_once_as_a_task_and_raised_for_attention(project: Path, queue, tmp_path: Path) -> None:
    watched = _watched(project)

    first = scan(watched, log_root=tmp_path / "logs")
    done = file_findings(watched, first, queue=queue)
    watched = record_scan(watched, first)
    again = scan(watched, force=True, log_root=tmp_path / "logs")
    redo = file_findings(watched, again, queue=queue)

    assert first.scanned and [f.key for f in first.findings] == ["check:tests"] and first.passing == ["check:lint"]
    assert "2 failed, 10 passed" in first.findings[0].detail
    assert Path(first.findings[0].link).is_file(), "the finding links the check's full log"
    assert done == {"filed": ["check:tests"], "already_filed": [], "cleared": []}
    assert redo["filed"] == [] and redo["already_filed"] == ["check:tests"]
    tasks = backlog_mod.load(project).tasks
    assert len(tasks) == 1, "a finding is filed once, however many scans see it"
    task = tasks[0]
    assert task.source == "raven-watch:check:tests" and task.state == "open" and task.severity == "normal"
    assert task.history and task.history[0].get("by") == "scanner"
    items = queue.items()
    assert len(items) == 1 and items[0].kind == "discovered_task" and items[0].data["task"] == task.id
    assert watched.failing == {"check:tests": first.findings[0].detail}


def test_a_check_that_passes_again_clears_its_attention_item_and_leaves_the_task(
    project: Path, queue, tmp_path: Path
) -> None:
    watched = _watched(project)
    first = scan(watched, log_root=tmp_path / "logs")
    file_findings(watched, first, queue=queue)
    watched = record_scan(watched, first)

    (project / "state.txt").write_text("fixed", encoding="utf-8")
    _git(project, "commit", "-q", "-am", "fix the tests")
    fixed = scan(watched, log_root=tmp_path / "logs")
    done = file_findings(watched, fixed, queue=queue)

    assert fixed.scanned and fixed.findings == [] and "check:tests" in fixed.passing
    assert done["cleared"] == ["check:tests"]
    assert queue.items() == [] and queue.items(include_resolved=True)[0].resolution == "passing again"
    assert backlog_mod.load(project).tasks[0].state == "open", "a green run is evidence, the backlog's owner decides"


def test_a_scan_runs_when_the_commit_moves_or_the_interval_passes(project: Path, tmp_path: Path) -> None:
    watched = record_scan(_watched(project, every_s=3600), scan(_watched(project), log_root=tmp_path / "l"), now=1000.0)

    same = scan(watched, now=1100.0, log_root=tmp_path / "l")
    later = scan(watched, now=1000.0 + 3600, log_root=tmp_path / "l")
    (project / "new.txt").write_text("x", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "move")
    moved = scan(watched, now=1100.0, log_root=tmp_path / "l")

    assert not same.scanned and "not due" in same.reason
    assert later.scanned and moved.scanned


def test_red_ci_on_the_branch_is_a_high_priority_finding_with_its_link(
    project: Path, monkeypatch, tmp_path: Path
) -> None:
    seen: list[tuple[str, dict]] = []

    def fake(url: str, headers: dict) -> dict:
        seen.append((url, headers))
        return {
            "check_runs": [
                {
                    "name": "build",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/o/r/runs/1",
                    "output": {"title": "3 tests failed"},
                },
                {"name": "lint", "status": "completed", "conclusion": "success"},
                {"name": "deploy", "status": "in_progress", "conclusion": None},
            ]
        }

    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    watched = WatchedProject(path=str(project), github="o/r", branch="main")

    result = scan(watched, fetch=fake, log_root=tmp_path / "l")

    assert seen[0][0] == "https://api.github.com/repos/o/r/commits/main/check-runs?per_page=100"
    assert seen[0][1]["Authorization"] == "Bearer t0ken"
    (ci,) = result.findings
    assert (
        ci.key == "ci:build" and ci.priority == "high" and ci.link.endswith("/runs/1") and ci.detail == "3 tests failed"
    )
    assert result.passing == ["ci:lint"], "a run still in progress is neither a failure nor a pass"


def test_ci_that_cannot_be_read_is_said_and_the_checks_still_run(project: Path, tmp_path: Path) -> None:
    from raven.stint.trackers import TrackerError

    def down(url: str, headers: dict) -> dict:
        raise TrackerError("GitHub answered 503")

    result = scan(_watched(project, github="o/r"), fetch=down, log_root=tmp_path / "l")

    assert "CI not read" in result.reason and [f.key for f in result.findings] == ["check:tests"]


def test_checks_run_without_the_hosts_secrets(project: Path, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    leak = WatchCheck(
        name="env", run=f'"{PY}" -c "import os,sys; sys.exit(1 if os.environ.get(\'OPENAI_API_KEY\') else 0)"'
    )

    result = scan(WatchedProject(path=str(project), checks=[leak]), log_root=tmp_path / "l")

    assert result.passing == ["check:env"]


def test_an_attention_item_from_the_watch_reaches_the_operator_notifications(
    project: Path, queue, monkeypatch, tmp_path: Path
) -> None:
    from raven.proactive_engine import notify

    events = []
    monkeypatch.setattr(notify, "emit", lambda event: events.append(event))
    watched = _watched(project)

    file_findings(watched, scan(watched, log_root=tmp_path / "l"), queue=queue)

    assert [(e.kind, e.priority) for e in events] == [("discovered_task", "normal")]


def test_nothing_is_built_or_run_until_a_project_is_registered(project: Path) -> None:
    from raven.core.proactive_stack import build_project_watch

    assert build_project_watch(None) is None
    WatchStore().put(_watched(project))
    assert isinstance(build_project_watch(None), ProjectWatch)


async def test_the_resident_watch_finds_and_files_on_its_own(project: Path) -> None:
    WatchStore().put(_watched(project))
    watch = ProjectWatch(tick_s=0.05)

    watch.start()
    try:
        for _ in range(200):
            # The scan is recorded after it is filed, so its record is the last thing to land.
            if WatchStore().projects()[0].last_scan_at:
                break
            await asyncio.sleep(0.05)
    finally:
        watch.stop()

    tasks = backlog_mod.load(project).tasks
    assert [t.source for t in tasks] == ["raven-watch:check:tests"]
    assert WatchStore().projects()[0].last_head == _git(project, "rev-parse", "HEAD")


def test_the_command_line_adds_lists_scans_and_removes(project: Path) -> None:
    from raven.cli.commands import app

    runner = CliRunner()

    nothing = runner.invoke(app, ["watch", "add", str(project)])
    bad = runner.invoke(app, ["watch", "add", str(project), "--check", "tests", "--every", "1h"])
    added = runner.invoke(
        app, ["watch", "add", str(project), "--check", f'tests="{PY}" check_tests.py', "--every", "2h"]
    )
    scanned = runner.invoke(app, ["watch", "scan"])
    again = runner.invoke(app, ["watch", "scan"])
    listed = runner.invoke(app, ["watch", "list"])
    removed = runner.invoke(app, ["watch", "remove", str(project)])

    assert nothing.exit_code == 2 and "nothing to watch" in nothing.output
    assert bad.exit_code == 2
    assert added.exit_code == 0 and "watching" in added.output and "next restart" in added.output
    assert "1 problem(s)" in scanned.output and "check:tests (new task)" in scanned.output
    assert "check:tests (already filed)" in again.output
    assert "every 7200s" in listed.output and "! check:tests" in listed.output
    assert removed.exit_code == 0 and WatchStore().projects() == []
    assert (
        json.loads((project / ".stint" / "backlog.json").read_text(encoding="utf-8"))["tasks"][0]["source"]
        == "raven-watch:check:tests"
    )


def test_detect_takes_the_checks_a_stint_already_recorded_for_the_project(project: Path) -> None:
    from raven.cli.commands import app
    from raven.stint.checks import remember_check

    remember_check(project, "default", "tests", f'"{PY}" check_tests.py')

    added = CliRunner().invoke(app, ["watch", "add", str(project), "--detect"])

    assert added.exit_code == 0, added.output
    (watched,) = WatchStore().projects()
    assert [(c.name, c.run) for c in watched.checks] == [("default-tests", f'"{PY}" check_tests.py')]

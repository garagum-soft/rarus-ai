"""Best of N: attempts in their own checkouts, measured by the same checks, the best one applied."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.stint.best_of import BestOfError, best_of
from raven.stint.git import ProjectGit
from raven.stint.verify import CheckSpec

PY = sys.executable.replace("\\", "/")
CHECK = CheckSpec(
    name="answer",
    command=f"\"{PY}\" -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('value.txt').read_text().strip() == '42' else 1)\"",
    timeout_sec=60,
)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    for args in (["init", "-q", "-b", "main"],):
        subprocess.run(["git", "-C", str(project), *args], check=True)
    (project / "value.txt").write_text("0\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "value.txt"], check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(project), "commit", "-q", "-m", "base"],
        check=True,
    )
    return project


async def test_three_attempts_in_three_checkouts_and_the_best_is_applied(tmp_path: Path) -> None:
    project = _project(tmp_path)
    seen: list[Path] = []

    async def attempt(index: int, checkout: Path) -> str:
        seen.append(checkout)
        if index == 1:
            (checkout / "value.txt").write_text("41\n", encoding="utf-8")
        elif index == 2:
            (checkout / "value.txt").write_text("42\n", encoding="utf-8")
            (checkout / "notes.md").write_text("".join(f"line {i}\n" for i in range(30)), encoding="utf-8")
        else:
            (checkout / "value.txt").write_text("42\n", encoding="utf-8")
        return f"attempt {index} done"

    result = await best_of(project, 3, attempt, checks=[CHECK], root=tmp_path / "runs", tag="t1")

    assert len({str(p) for p in seen}) == 3, "every attempt had a checkout of its own"
    assert [(a.index, a.passed, a.failed) for a in result.attempts] == [(1, 0, 1), (2, 1, 0), (3, 1, 0)]
    assert result.winner is not None and result.winner.index == 3, "a tie on checks goes to the smaller change"
    assert result.applied is not None and result.applied.merged
    assert (project / "value.txt").read_text(encoding="utf-8") == "42\n"
    assert not (project / "notes.md").exists()
    assert "attempt 3 (best-of/t1-3): 1 passed, 0 failed" in result.report() and "<- applied" in result.report()
    branches = subprocess.run(
        ["git", "-C", str(project), "branch", "--list", "best-of/*"], capture_output=True, text=True
    ).stdout
    assert all(f"best-of/t1-{i}" in branches for i in (1, 2, 3)), "losing attempts stay to be looked at"
    assert all(not p.exists() for p in seen), "their checkouts are removed"


async def test_when_no_attempt_is_clean_nothing_is_applied(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = ProjectGit(project).head()

    async def attempt(index: int, checkout: Path) -> str:
        if index == 2:
            raise RuntimeError("agent crashed")
        (checkout / "value.txt").write_text("7\n", encoding="utf-8")
        return "wrong"

    result = await best_of(project, 2, attempt, checks=[CHECK], root=tmp_path / "runs", tag="t2")

    assert result.winner is None and result.applied is None
    assert result.attempts[1].error == "RuntimeError: agent crashed"
    assert ProjectGit(project).head() == before
    assert "nothing was applied" in result.report()


async def test_a_run_that_cannot_start_is_refused(tmp_path: Path) -> None:
    async def attempt(index: int, checkout: Path) -> str:
        return ""

    with pytest.raises(BestOfError, match="not a git repository"):
        await best_of(tmp_path, 2, attempt, checks=[], root=tmp_path / "runs", tag="x")
    with pytest.raises(BestOfError, match="between 1 and 10"):
        await best_of(_project(tmp_path), 0, attempt, checks=[], root=tmp_path / "runs", tag="x")


def test_the_command_line_runs_attempts_on_a_roster_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The main path: `rarus playbook best-of` drives a configured agent once per checkout."""
    from raven.cli.commands import app

    home = tmp_path / "home"
    home.mkdir()
    # The agent writes 42 into value.txt in the directory it is run in.
    writer = f"\"{PY}\" -c \"import pathlib; pathlib.Path('value.txt').write_text('42\\n')\""
    (home / "config.json").write_text(
        json.dumps({"subagents": {"agents": [{"name": "writer", "kind": "cli", "command": writer}]}}), encoding="utf-8"
    )
    monkeypatch.setenv("RARUS_HOME", str(home))
    project = _project(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "playbook",
            "best-of",
            "make value.txt hold 42",
            "--agent",
            "writer",
            "-n",
            "2",
            "--check",
            CHECK.command,
            "-p",
            str(project),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "<- applied" in result.output
    assert (project / "value.txt").read_text(encoding="utf-8").strip() == "42"

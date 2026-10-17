"""Fast mode: a small task done by one agent and checked, with no planning stage."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.playbook.fast import run_fast
from raven.stint.verify import CheckSpec

PY = sys.executable.replace("\\", "/")
CHECK = CheckSpec(
    name="value",
    command=f"\"{PY}\" -c \"import pathlib,sys; sys.exit(pathlib.Path('value.txt').read_text().strip() != '42')\"",
    timeout_sec=60,
)


async def test_a_small_task_passes_on_the_first_attempt(tmp_path: Path) -> None:
    seen: list[str] = []

    async def agent(brief: str, where: Path) -> str:
        seen.append(brief)
        (where / "value.txt").write_text("42\n")
        return "wrote it"

    result = await run_fast("make value.txt hold 42", tmp_path, agent, checks=[CHECK])

    assert result.passed and result.attempts == 1 and result.answer == "wrote it"
    assert [s.kind for s in result.steps] == ["agent", "checks"]
    assert seen == ["make value.txt hold 42"]


async def test_a_failed_check_sends_the_agent_back_with_what_failed(tmp_path: Path) -> None:
    briefs: list[str] = []

    async def agent(brief: str, where: Path) -> str:
        briefs.append(brief)
        (where / "value.txt").write_text("42\n" if len(briefs) == 2 else "41\n")
        return ""

    events: list[dict] = []
    result = await run_fast("make value.txt hold 42", tmp_path, agent, checks=[CHECK], on_event=events.append)

    assert result.passed and result.attempts == 2
    assert "did not pass these checks" in briefs[1] and "value" in briefs[1] and "exit 1" in briefs[1]
    assert [e["event"] for e in events] == [
        "attempt_started",
        "agent_finished",
        "checks_finished",
        "attempt_started",
        "agent_finished",
        "checks_finished",
        "fast_finished",
    ]


async def test_it_gives_up_after_the_attempts_and_says_so(tmp_path: Path) -> None:
    async def agent(brief: str, where: Path) -> str:
        (where / "value.txt").write_text("0\n")
        return ""

    result = await run_fast("x", tmp_path, agent, checks=[CHECK], attempts=2)

    assert not result.passed and result.attempts == 2
    assert "not done after 2 attempt(s)" in result.report()


async def test_an_attempt_that_runs_too_long_or_raises_is_a_failed_attempt(tmp_path: Path) -> None:
    calls = 0

    async def agent(brief: str, where: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(5)
        if calls == 2:
            raise RuntimeError("agent crashed")
        return "done"

    result = await run_fast("x", tmp_path, agent, attempts=3, step_timeout_s=0.2)

    assert result.passed and result.attempts == 3
    assert [s.ok for s in result.steps] == [False, False, True]
    assert "no answer in 0s" in result.steps[0].detail and "agent crashed" in result.steps[1].detail


async def test_with_no_checks_the_agents_answer_is_the_result(tmp_path: Path) -> None:
    async def agent(brief: str, where: Path) -> str:
        return "42"

    result = await run_fast("what is 6*7", tmp_path, agent)

    assert result.passed and result.answer == "42" and [s.kind for s in result.steps] == ["agent"]


def test_the_command_line_does_a_small_task_without_planning_a_playbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main path: `rarus playbook fast` with a configured agent; the playbook generator is never asked."""
    from raven.cli.commands import app
    from raven.playbook import PlaybookGenerator

    async def no_planning(self, *args, **kwargs):  # pragma: no cover - its being called is the failure
        raise AssertionError("fast mode must not plan a playbook")

    monkeypatch.setattr(PlaybookGenerator, "generate", no_planning)
    home = tmp_path / "home"
    home.mkdir()
    # The agent reads its brief on stdin: the first attempt writes 41, a retry
    # that carries the failed check writes 42.
    script = tmp_path / "agent.py"
    script.write_text(
        "import pathlib, sys\n"
        "brief = sys.stdin.read()\n"
        "pathlib.Path('value.txt').write_text('42\\n' if 'did not pass' in brief else '41\\n')\n"
        "print('ok')\n",
        encoding="utf-8",
    )
    command = f'"{PY}" "{script.as_posix()}"'
    (home / "config.json").write_text(
        json.dumps({"subagents": {"agents": [{"name": "writer", "kind": "cli", "command": command}]}}), encoding="utf-8"
    )
    monkeypatch.setenv("RARUS_HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "playbook",
            "fast",
            "make value.txt hold 42",
            "--agent",
            "writer",
            "--check",
            CHECK.command,
            "-p",
            str(project),
            "--events",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "fast: done after 2 attempt(s)" in result.output and '"event": "fast_finished"' in result.output
    assert (project / "value.txt").read_text(encoding="utf-8").strip() == "42"
    records = list((home / "fast").glob("*.json"))
    assert len(records) == 1 and json.loads(records[0].read_text(encoding="utf-8"))["passed"] is True

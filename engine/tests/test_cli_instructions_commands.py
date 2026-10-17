"""``rarus instructions``: log, show, diff, record and rollback from the command line."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from raven.agent.instructions import InstructionHistory
from raven.cli.commands import app

AGENT = "agent_memory/profile/agent.md"
runner = CliRunner()


def _ws(tmp_path: Path) -> Path:
    (tmp_path / AGENT).parent.mkdir(parents=True)
    (tmp_path / AGENT).write_text("first\n", encoding="utf-8")
    return tmp_path


def _run(*args: str):
    result = runner.invoke(app, ["instructions", *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_record_two_revisions_roll_back_and_read_the_log(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    _run("record", "agent", "-m", "initial", "-w", str(ws))
    (ws / AGENT).write_text("second\n", encoding="utf-8")
    _run("record", "agent", "-m", "rewrite", "-w", str(ws))

    out = _run("rollback", "agent", "1", "-w", str(ws))
    log = _run("log", "agent", "-w", str(ws))

    assert "back at revision 1, recorded as revision 3" in out
    assert (ws / AGENT).read_text(encoding="utf-8") == "first\n"
    assert "initial" in log and "rewrite" in log and "rollback <- 1" in log
    assert "head: 3" in log


def test_show_and_diff(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    history = InstructionHistory(ws)
    history.record(AGENT, author="user")
    history.save(AGENT, "first\nsecond\n", author="user")

    assert _run("show", "agent", "--rev", "1", "-w", str(ws)) == "first\n"
    assert "+second" in _run("diff", "agent", "1", "2", "-w", str(ws))


def test_a_file_that_is_not_an_instruction_file_is_refused(tmp_path: Path) -> None:
    result = runner.invoke(app, ["instructions", "log", "notes.md", "-w", str(tmp_path)])
    assert result.exit_code == 1
    assert "not an instruction file" in result.output


def test_the_tracked_files_are_listed() -> None:
    out = _run("files")
    assert "soul" in out and "agent_memory/profile/agent.md" in out and "TOOLS.md" in out

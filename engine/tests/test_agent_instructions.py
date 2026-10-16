"""Revisions of the agent's instruction files: head, log, rollback and review."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.instructions import InstructionError, InstructionHistory
from raven.agent.tools.filesystem import EditFileTool, WriteFileTool

AGENT = "agent_memory/profile/agent.md"


def _workspace(tmp_path: Path, text: str = "v0: be terse\n") -> Path:
    target = tmp_path / AGENT
    target.parent.mkdir(parents=True)
    target.write_text(text, encoding="utf-8")
    return tmp_path


def test_two_saves_make_two_revisions_and_the_head_follows(tmp_path: Path) -> None:
    history = InstructionHistory(_workspace(tmp_path))

    history.save(AGENT, "v1: answer in English\n", author="user", note="language")
    history.save(AGENT, "v2: answer in English, cite sources\n", author="user", note="citations")

    log = history.log(AGENT)
    assert [(r.rev, r.author, r.note) for r in log] == [
        (1, "baseline", "the text before revisions were kept"),
        (2, "user", "language"),
        (3, "user", "citations"),
    ]
    assert history.head(AGENT) == 3
    assert history.text_at(AGENT, 2) == "v1: answer in English\n"


def test_rollback_puts_the_old_text_back_as_a_new_revision(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    history = InstructionHistory(ws)
    history.save(AGENT, "first\n", author="user", note="one")
    history.save(AGENT, "second\n", author="user", note="two")

    revision = history.rollback(AGENT, 2, author="user")

    assert (ws / AGENT).read_text(encoding="utf-8") == "first\n"
    assert revision.rev == 4 and revision.action == "rollback" and revision.restored_from == 2
    assert history.head(AGENT) == 4
    log = history.log(AGENT)
    assert [r.note for r in log][1:] == ["one", "two", "roll back to revision 2"], (
        "the log keeps both changes and the undo"
    )


def test_an_unchanged_file_records_nothing(tmp_path: Path) -> None:
    history = InstructionHistory(_workspace(tmp_path))
    assert history.record(AGENT, author="user") is not None
    assert history.record(AGENT, author="user") is None
    assert len(history.log(AGENT)) == 1


def test_a_diff_between_revisions_is_there_for_review(tmp_path: Path) -> None:
    history = InstructionHistory(_workspace(tmp_path))
    history.save(AGENT, "be terse\ncite sources\n", author="user")

    diff = history.diff(AGENT, 1, 2)

    assert "-v0: be terse" in diff and "+be terse" in diff and "+cite sources" in diff
    assert f"{AGENT}@1" in diff and f"{AGENT}@2" in diff


def test_only_instruction_files_are_tracked(tmp_path: Path) -> None:
    history = InstructionHistory(tmp_path)
    with pytest.raises(InstructionError):
        history.record("notes.md", author="user")
    with pytest.raises(InstructionError):
        history.record(tmp_path.parent / "elsewhere" / "agent.md", author="user")
    with pytest.raises(InstructionError):
        history.text_at(AGENT, 7)
    assert history.is_tracked(tmp_path / "TOOLS.md")


async def test_the_agent_writing_its_instructions_leaves_revisions(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    write = WriteFileTool(workspace=ws, follow_binding=False)
    edit = EditFileTool(workspace=ws, follow_binding=False)

    out1 = await write.execute(path=AGENT, content="v1: be thorough\n")
    out2 = await edit.execute(path=AGENT, old_text="thorough", new_text="thorough and brief")

    history = InstructionHistory(ws)
    log = history.log(AGENT)
    assert "Successfully" in str(getattr(out1, "model_text", out1))
    assert "Successfully" in str(getattr(out2, "model_text", out2))
    assert [(r.author, r.note) for r in log] == [
        ("baseline", "the text before revisions were kept"),
        ("agent", "write_file"),
        ("agent", "edit_file"),
    ]
    assert history.text_at(AGENT, 1) == "v0: be terse\n"
    assert history.text_at(AGENT, 3) == "v1: be thorough and brief\n"


async def test_other_files_written_by_the_agent_leave_no_revisions(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    await WriteFileTool(workspace=ws, follow_binding=False).execute(path="notes.md", content="x")
    assert not (ws / ".instructions").exists()


async def test_a_failed_write_records_no_change(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    out = await EditFileTool(workspace=ws, follow_binding=False).execute(path=AGENT, old_text="nope", new_text="x")

    assert str(getattr(out, "model_text", out)).startswith("Error")
    assert [r.author for r in InstructionHistory(ws).log(AGENT)] == ["baseline"]

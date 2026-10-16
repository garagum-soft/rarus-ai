"""Message retention: past their age messages go, unless something kept depends on them."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.session.retention import RetentionPolicy, apply, mark_read, plan

NOW = datetime(2026, 10, 13, 12, 0, 0)


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


POLICY = RetentionPolicy(read_days=30, unread_days=90)


def test_an_old_message_with_no_dependents_goes_and_one_referred_to_stays() -> None:
    messages = [
        {"id": "m1", "role": "user", "content": "old and alone", "timestamp": ago(200)},
        {"id": "m2", "role": "user", "content": "old but quoted", "timestamp": ago(200)},
        {"id": "m3", "role": "assistant", "content": "recent reply", "reply_to": "m2", "timestamp": ago(1)},
    ]

    result = plan(messages, POLICY, now=NOW)

    assert result.drop == [0]
    assert result.keep == [1, 2] and result.kept_for == {1: "a kept message refers to it"}


def test_a_tool_call_and_its_result_go_or_stay_together() -> None:
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "c1"}], "timestamp": ago(200)},
        {"role": "tool", "tool_call_id": "c1", "content": "result", "timestamp": ago(1)},
        {"role": "assistant", "tool_calls": [{"id": "c2"}], "timestamp": ago(200)},
        {"role": "tool", "tool_call_id": "c2", "content": "old result", "timestamp": ago(200)},
    ]

    result = plan(messages, POLICY, now=NOW)

    assert result.keep == [0, 1] and result.kept_for == {0: "a kept result answers its tool call"}
    assert result.drop == [2, 3]


def test_read_and_unread_messages_keep_for_different_periods() -> None:
    messages = [
        {"content": "read 40 days ago", "timestamp": ago(40)},
        {"content": "unread 40 days ago", "timestamp": ago(39)},
    ]
    mark = NOW - timedelta(days=39.5)

    result = plan(messages, POLICY, now=NOW, last_read_at=mark)

    assert result.drop == [0], "read and older than 30 days goes; unread keeps for 90"
    assert plan(messages, POLICY, now=NOW).drop == [], "a session never marked read counts as unread"


def test_pinned_and_undated_messages_stay() -> None:
    messages = [{"content": "pinned", "pinned": True, "timestamp": ago(500)}, {"content": "no date"}]
    assert plan(messages, POLICY, now=NOW).drop == []


def test_bad_periods_are_refused() -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(read_days=0, unread_days=10)


def _session(tmp_path: Path):
    from raven.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    session = manager.get_or_create("tui:20260101_000000_aaaaaa")
    session.messages = [
        {"id": "m1", "role": "user", "content": "ancient", "timestamp": ago(200)},
        {"id": "m2", "role": "user", "content": "ancient, quoted", "timestamp": ago(200)},
        {"id": "m3", "role": "assistant", "content": "recent", "reply_to": "m2", "timestamp": ago(1)},
    ]
    manager.save(session)
    return manager


def test_the_transcript_on_disk_is_rewritten_without_the_dropped_messages(tmp_path: Path) -> None:
    manager = _session(tmp_path)

    result = apply(manager, "tui:20260101_000000_aaaaaa", POLICY, now=NOW)

    manager.invalidate("tui:20260101_000000_aaaaaa")
    kept = [m["content"] for m in manager.get_or_create("tui:20260101_000000_aaaaaa").messages]
    assert result.drop == [0] and kept == ["ancient, quoted", "recent"]


def test_mark_read_moves_messages_to_the_shorter_period(tmp_path: Path) -> None:
    from raven.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    session = manager.get_or_create("tui:x")
    session.messages = [{"content": "seen long ago", "timestamp": ago(60)}]
    manager.save(session)

    assert apply(manager, "tui:x", POLICY, now=NOW, dry_run=True).drop == []
    mark_read(manager, "tui:x", at=NOW - timedelta(days=50))
    manager.invalidate("tui:x")
    assert apply(manager, "tui:x", POLICY, now=NOW, dry_run=True).drop == [0]


def test_the_command_line_runs_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.cli import session_commands
    from raven.cli.commands import app

    manager = _session(tmp_path)
    monkeypatch.setattr(session_commands, "_open_manager", lambda: manager)
    runner = CliRunner()

    dry = runner.invoke(app, ["sessions", "retention", "--read-days", "30", "--unread-days", "90", "--dry-run"])
    real = runner.invoke(app, ["sessions", "retention", "--read-days", "30", "--unread-days", "90"])

    assert dry.exit_code == 0 and "would remove 1, kept 2 (1 kept for a dependency)" in dry.output
    assert "removed 1, kept 2" in real.output
    manager.invalidate("tui:20260101_000000_aaaaaa")
    assert len(manager.get_or_create("tui:20260101_000000_aaaaaa").messages) == 2

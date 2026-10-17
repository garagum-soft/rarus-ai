"""``raven attention``: the queue on the command line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.proactive_engine.attention import AttentionQueue

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RARUS_HOME", str(tmp_path))
    return tmp_path


def test_list_shows_the_most_pressing_first_and_resolve_dismisses(home: Path) -> None:
    q = AttentionQueue(home / "attention.json")
    q.raise_item("q1", kind="question", priority="normal", title="Which branch?", session_key="tui:1")
    q.raise_item("hg", kind="host_guard", priority="critical", title="Disk full")

    listed = runner.invoke(app, ["attention", "list"])
    as_json = json.loads(runner.invoke(app, ["attention", "list", "--json"]).output)
    dismissed = runner.invoke(app, ["attention", "resolve", "hg"])
    again = runner.invoke(app, ["attention", "resolve", "hg"])

    lines = listed.output.splitlines()
    assert lines[0].startswith("critical Disk full") and lines[1].startswith("normal   Which branch? [tui:1]")
    assert "1 critical, 1 normal" in listed.output
    assert [i["id"] for i in as_json["items"]] == ["hg", "q1"]
    assert dismissed.exit_code == 0 and again.exit_code == 1
    assert [i.id for i in q.items()] == ["q1"]


def test_an_empty_queue_says_so(home: Path) -> None:
    assert "nothing is waiting on you" in runner.invoke(app, ["attention", "list"]).output

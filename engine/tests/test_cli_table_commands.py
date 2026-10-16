"""``raven table``: create a table, give it a source, sync it, and see the change arrive."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    return tmp_path


def test_a_source_change_reaches_the_table_through_the_command_line(home: Path) -> None:
    feed = home / "people.csv"
    feed.write_text("id,name\n1,Ann\n2,Bo\n", encoding="utf-8")

    assert runner.invoke(app, ["table", "create", "people", "--key", "id", "-c", "name"]).exit_code == 0
    assert runner.invoke(app, ["table", "source", "people", "hr", str(feed), "--every", "300"]).exit_code == 0
    first = runner.invoke(app, ["table", "sync", "people"])
    feed.write_text("id,name\n1,Ann B.\n3,Cy\n", encoding="utf-8")
    second = runner.invoke(app, ["table", "sync", "people"])
    shown = json.loads(runner.invoke(app, ["table", "show", "people", "--json"]).output)

    assert "people/hr: +2 ~0 -0 =0" in first.output
    assert "people/hr: +1 ~1 -1 =0" in second.output
    assert {r["id"]: r["name"] for r in shown["rows"]} == {"1": "Ann B.", "3": "Cy"}
    assert shown["sources"][0]["every"] == 300 and shown["sources"][0]["kind"] == "csv"
    log = runner.invoke(app, ["table", "log", "people"]).output
    assert log.count("people/hr") == 2


def test_a_failed_sync_exits_non_zero_and_local_edits_show(home: Path) -> None:
    runner.invoke(app, ["table", "create", "t", "--key", "id"])
    runner.invoke(app, ["table", "source", "t", "s", str(home / "missing.json")])
    runner.invoke(app, ["table", "set", "t", "7", "note", "mine"])

    failed = runner.invoke(app, ["table", "sync", "t"])
    listed = runner.invoke(app, ["table", "list"])
    shown = runner.invoke(app, ["table", "show", "t"])

    assert failed.exit_code == 1 and "not synced" in failed.output
    assert "t" in listed.output and "1 rows, 1 source(s)" in listed.output
    assert "7\tmine" in shown.output


def test_an_unknown_table_is_refused() -> None:
    assert runner.invoke(app, ["table", "sync", "nope"]).exit_code == 1

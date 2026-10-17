"""``rarus explain`` and the one-shot reply hint read the config in force."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app

runner = CliRunner()


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from raven.config.loader import set_config_path

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"agents": {"defaults": {"maxToolIterations": 17}}}), encoding="utf-8")
    set_config_path(path)
    yield path
    set_config_path(None)  # type: ignore[arg-type]


def test_explain_names_the_config_value_behind_a_refusal(config_file: Path) -> None:
    text = "I reached the maximum number of tool call iterations (17) without completing the task."

    plain = runner.invoke(app, ["explain", text])
    as_json = runner.invoke(app, ["explain", text, "--json"])

    assert plain.exit_code == 0 and "agents.defaults.maxToolIterations = 17" in plain.output
    assert json.loads(as_json.output) == {
        "rule": "max_tool_iterations",
        "hint": json.loads(as_json.output)["hint"],
        "settings": {"cap": 17},
    }


def test_explain_says_so_when_it_knows_no_such_block(config_file: Path) -> None:
    result = runner.invoke(app, ["explain", "Error: file not found"])
    assert result.exit_code == 1 and "No known block" in result.output


def test_explain_reads_the_refusal_from_stdin(config_file: Path) -> None:
    result = runner.invoke(app, ["explain"], input="Error: Path /x is outside allowed directories /w")
    assert result.exit_code == 0 and "tools.restrictToWorkspace is false" in result.output


def test_a_one_shot_reply_that_hit_the_iteration_cap_carries_the_hint(config_file: Path, capsys) -> None:
    from raven.cli import agent_commands

    agent_commands._print_agent_response(
        "I reached the maximum number of tool call iterations (17) without completing the task.", False
    )
    agent_commands._print_agent_response("The file is outside the workspace, so I left it.", False)

    out = capsys.readouterr().out
    assert out.count("How to unblock:") == 1 and "maxToolIterations = 17" in out

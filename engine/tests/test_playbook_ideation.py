"""Ideation: ideas per category from agents, a roadmap laid out from them, progress as events."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.playbook.ideation import Idea, ideate, lay_out, parse_ideas, save

ANSWERS = {
    "features": '[{"title": "Dark mode", "why": "asked for", "effort": "S", "impact": 4},'
    ' {"title": "Plugins", "why": "extensibility", "effort": "L", "impact": 5},'
    ' {"title": "Export CSV", "why": "reports", "effort": "M", "impact": 3}]',
    "security": 'Here you go:\n[{"title": "Rotate tokens", "why": "leaks", "effort": "M", "impact": 5},]',
}


async def test_each_category_returns_ideas_and_its_part_of_the_roadmap() -> None:
    events: list[dict] = []
    asked: list[str] = []

    async def ask(category: str, prompt: str) -> str:
        asked.append(category)
        assert category in prompt and "JSON" in prompt
        return ANSWERS[category]

    roadmap = await ideate("a notes app", ask, categories=["features", "security"], on_event=events.append)

    assert sorted(asked) == ["features", "security"]
    assert [i.title for i in roadmap.ideas if i.category == "features"] == ["Dark mode", "Plugins", "Export CSV"]
    assert roadmap.by_category("security") == {"Now": ["Rotate tokens"], "Next": [], "Later": []}
    features = roadmap.by_category("features")
    assert features["Now"] == ["Dark mode"] and features["Next"] == ["Plugins"] and features["Later"] == ["Export CSV"]
    names = [e["event"] for e in events]
    assert names[0] == "ideation_started" and names[-1] == "roadmap_finished"
    assert names.count("category_finished") == 2
    assert [e["progress"] for e in events if "progress" in e] == sorted(
        e["progress"] for e in events if "progress" in e
    )
    assert events[-1]["progress"] == 100


async def test_a_category_that_fails_is_reported_and_the_rest_go_on() -> None:
    async def ask(category: str, prompt: str) -> str:
        if category == "security":
            return "I would rather not."
        return ANSWERS[category]

    events: list[dict] = []
    roadmap = await ideate("x", ask, categories=["features", "security"], on_event=events.append)

    assert "security" in roadmap.failed and len(roadmap.ideas) == 3
    assert any(e["event"] == "category_failed" and e["category"] == "security" for e in events)
    assert "security: no ideas" in roadmap.render()


def test_loose_answers_are_still_read() -> None:
    ideas = parse_ideas(
        "quality", 'notes [{"title": "Add tests", "effort": "xl", "impact": "9"}, {"why": "no title"}] end'
    )
    assert [(i.title, i.effort, i.impact) for i in ideas] == [("Add tests", "M", 5)]
    with pytest.raises(ValueError):
        parse_ideas("quality", "no list here")


def test_the_layout_ranks_by_impact_over_effort() -> None:
    phases = lay_out(
        [
            Idea("a", "big win", effort="S", impact=5),
            Idea("a", "slog", effort="L", impact=2),
            Idea("a", "ok", effort="M", impact=3),
        ]
    )
    assert [i.title for i in phases["Now"]] == ["big win"] and [i.title for i in phases["Later"]] == ["slog"]


async def test_the_roadmap_is_written_out(tmp_path: Path) -> None:
    async def ask(category: str, prompt: str) -> str:
        return ANSWERS["features"]

    roadmap = await ideate("x", ask, categories=["features"])
    ideas_file, page = save(roadmap, tmp_path / "out")

    assert len(json.loads(ideas_file.read_text(encoding="utf-8"))) == 3
    assert "## Now" in page.read_text(encoding="utf-8") and "Dark mode" in page.read_text(encoding="utf-8")


def test_the_command_line_ideates_with_a_roster_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The main path: `raven playbook ideate` puts each category to a configured agent."""
    from raven.cli.commands import app

    home = tmp_path / "home"
    home.mkdir()
    python = sys.executable.replace("\\", "/")
    # The agent reads which category it was asked about and answers with JSON ideas for it.
    script = (
        "import sys, json, re; p = sys.stdin.read(); c = re.search(r'only at (\\w+)', p).group(1); "
        "print(json.dumps([{'title': c + ' idea ' + str(i), 'why': 'w', 'effort': 'SML'[i], 'impact': 5 - i} for i in range(3)]))"
    )
    (home / "config.json").write_text(
        json.dumps(
            {"subagents": {"agents": [{"name": "planner", "kind": "cli", "command": f'"{python}" -c "{script}"'}]}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RAVEN_HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("# Notes app\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "playbook",
            "ideate",
            "--agent",
            "planner",
            "-c",
            "features",
            "-c",
            "security",
            "-p",
            str(project),
            "--events",
        ],
    )

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    assert {e["category"] for e in events if e["event"] == "category_finished"} == {"features", "security"}
    page = (project / ".raven" / "ideation" / "roadmap.md").read_text(encoding="utf-8")
    assert "[features] features idea 0" in page and "[security] security idea 0" in page

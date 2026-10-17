"""Decomposition of a large goal into a tree of tasks (raven/playbook/decompose.py), its command and its scenario.

The planner is a scripted ``ask`` here, and a real command-line agent behind
the real sub-agent manager in the command and scenario tests.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.playbook import decompose as dc

# -- one split -------------------------------------------------------------------------------


def test_an_answer_is_a_split_or_says_the_task_is_atomic() -> None:
    assert dc.parse_split('Sure:\n```json\n{"atomic": true, "why": "small"}\n```').atomic
    split = dc.parse_split('{"subtasks": [{"title": "a", "goal": "x"}, {"title": "b", "goal": "y"}]}')
    assert [r["title"] for r in split.children] == ["a", "b"]
    assert len(dc.parse_split('[{"title": "a", "goal": "x"}, {"title": "b", "goal": "y",}]').children) == 2
    with pytest.raises(ValueError, match="no JSON"):
        dc.parse_split("I would split it in two.")


def _split(*rows: dict) -> dc.Split:
    return dc.Split(children=[dict(r) for r in rows])


def test_a_split_is_checked_and_what_has_one_fix_is_fixed() -> None:
    split = _split(
        {"title": "Write the schema", "goal": "g"},
        {"id": "api", "title": "Build the API", "goal": "g", "depends_on": ["Write the schema"], "agent": "ghost"},
        {"id": "api", "title": "Second api", "goal": "g"},
    )

    assert dc.check_split(split, max_children=5, agents=["Raven"]) == []
    ids = [r["id"] for r in split.children]
    assert ids == ["write-the-schema", "api", "api-3"]
    # A dependency spelled as a title resolves; an agent the roster lacks is dropped.
    assert split.children[1]["depends_on"] == ["write-the-schema"] and split.children[1]["agent"] == ""


@pytest.mark.parametrize(
    ("rows", "max_children", "said"),
    [
        ([{"title": "only", "goal": "g"}], 5, "at least 2 subtasks"),
        ([{"title": str(i), "goal": "g"} for i in range(4)], 3, "at most 3 subtasks"),
        ([{"title": "a", "goal": "g"}, {"title": "", "goal": "g"}], 5, "has no title"),
        ([{"title": "a", "goal": "g"}, {"title": "b"}], 5, "has no goal"),
        (
            [{"title": "a", "goal": "g", "depends_on": ["zz"]}, {"title": "b", "goal": "g"}],
            5,
            "not one of its siblings",
        ),
        ([{"title": "a", "goal": "g", "depends_on": ["a"]}, {"title": "b", "goal": "g"}], 5, "depends on itself"),
        (
            [{"title": "a", "goal": "g", "depends_on": ["b"]}, {"title": "b", "goal": "g", "depends_on": ["a"]}],
            5,
            "form a cycle",
        ),
    ],
)
def test_a_split_that_cannot_be_applied_says_why(rows, max_children, said) -> None:
    errors = dc.check_split(_split(*rows), max_children=max_children)

    assert any(said in e for e in errors), errors


# -- the recursion ---------------------------------------------------------------------------

PLAN = {
    "the goal": {
        "subtasks": [
            {"id": "design", "title": "Design", "goal": "design it"},
            {"id": "build", "title": "Build", "goal": "build it", "depends_on": ["design"]},
            {"id": "docs", "title": "Docs", "goal": "document it", "atomic": True},
        ]
    },
    "Design": {
        "subtasks": [
            {"id": "schema", "title": "Schema", "goal": "the schema", "atomic": True},
            {"id": "api", "title": "API", "goal": "the api", "atomic": True, "depends_on": ["schema"]},
        ]
    },
}


class Planner:
    """Answers by the task named in the prompt; records every prompt by role."""

    def __init__(self, plan: dict, *, reviews: list[str] | None = None) -> None:
        self.plan = plan
        self.reviews = list(reviews or [])
        self.asked: list[tuple[str, str]] = []

    async def __call__(self, role: str, prompt: str) -> str:
        self.asked.append((role, prompt))
        if role == "reviewer":
            return self.reviews.pop(0)
        task = re.search(r"The task to split: (.+)", prompt).group(1).strip()
        answer = self.plan.get(task, {"atomic": True, "why": "small enough"})
        return answer if isinstance(answer, str) else json.dumps(answer)


async def test_a_goal_is_split_recursively_within_the_limits() -> None:
    planner = Planner(PLAN)
    events: list[dict] = []

    tree = await dc.decompose("ship the feature", planner, max_depth=2, plan_id="plan-abc123", on_event=events.append)

    assert [t.id for t in tree.tasks()] == ["root", "design", "design.schema", "design.api", "build", "docs"]
    reasons = {t.id: t.leaf_reason for t in tree.leaves()}
    # Build was asked and came back atomic; Docs was atomic when proposed, so never asked.
    assert reasons == {"build": "atomic", "docs": "atomic", "design.schema": "atomic", "design.api": "atomic"}
    assert tree.find("design.api").depends_on == ["design.schema"] and tree.find("build").depends_on == ["design"]
    asked = [re.search(r"The task to split: (.+)", p).group(1) for _, p in planner.asked]
    assert asked == ["the goal", "Design", "Build"]
    assert "Its siblings (not yours to cover): Build, Docs" in planner.asked[1][1]
    assert [e["task"] for e in events if e["event"] == "split"] == ["root", "design", "build"]

    # One level only: what was not atomic stops at the depth limit, unasked.
    shallow = await dc.decompose("ship the feature", Planner(PLAN), max_depth=1)
    assert {t.id: t.leaf_reason for t in shallow.leaves()} == {
        "design": "max depth",
        "build": "max depth",
        "docs": "atomic",
    }


async def test_dependencies_are_lifted_to_the_leaves_and_the_leaves_run_in_levels() -> None:
    tree = await dc.decompose("ship the feature", Planner(PLAN), max_depth=2, plan_id="plan-abc123")

    deps = dc.leaf_dependencies(tree)

    # Build waits on Design's finishing leaf only: the schema is already waited on by the api.
    assert deps == {"build": ["design.api"], "docs": [], "design.schema": [], "design.api": ["design.schema"]}
    assert dc.levels(tree) == [["design.schema", "docs"], ["design.api"], ["build"]]


async def test_a_refused_answer_is_asked_again_once_and_then_the_task_stays_whole() -> None:
    bad = json.dumps({"subtasks": [{"title": "a", "goal": "g", "depends_on": ["nope"]}, {"title": "b", "goal": "g"}]})
    good = json.dumps(
        {"subtasks": [{"title": "a", "goal": "g", "atomic": True}, {"title": "b", "goal": "g", "atomic": True}]}
    )
    answers = iter([bad, good])

    async def ask(role: str, prompt: str) -> str:
        return next(answers)

    tree = await dc.decompose("goal", ask)
    assert [t.id for t in tree.leaves()] == ["a", "b"]

    asked: list[str] = []

    async def stubborn(role: str, prompt: str) -> str:
        asked.append(prompt)
        return bad

    tree = await dc.decompose("goal", stubborn)
    assert len(asked) == 2 and "Your previous answer was refused" in asked[1] and "'nope'" in asked[1]
    assert tree.leaves()[0].id == "root" and tree.root.leaf_reason.startswith("planner failed:")


async def test_a_planner_that_fails_leaves_its_task_whole_and_the_rest_goes_on() -> None:
    async def ask(role: str, prompt: str) -> str:
        if "The task to split: Design" in prompt:
            raise RuntimeError("model down")
        return await Planner(PLAN)(role, prompt)

    tree = await dc.decompose("ship it", ask, max_depth=3)

    assert tree.find("design").leaf_reason == "planner failed: RuntimeError: model down"
    assert {t.id for t in tree.leaves()} >= {"design", "docs"}


async def test_the_leaf_budget_stops_a_split_that_would_overrun_it() -> None:
    tree = await dc.decompose("ship it", Planner(PLAN), max_depth=3, max_leaves=3)

    assert tree.find("design").leaf_reason == "leaf budget" and len(tree.leaves()) == 3


async def test_a_reviewer_sends_the_plan_back_with_notes_a_bounded_number_of_times() -> None:
    planner = Planner(PLAN, reviews=["Docs overlaps Build.", "APPROVED"])

    tree = await dc.decompose("ship it", planner, max_depth=2, review_rounds=2)

    assert [(r["round"], r["approved"]) for r in tree.reviews] == [(1, False), (2, True)]
    second_root = [p for role, p in planner.asked if role == "planner" and "The task to split: the goal" in p][1]
    assert "A reviewer's notes on an earlier plan" in second_root and "Docs overlaps Build." in second_root

    stubborn = Planner(PLAN, reviews=["no", "still no"])
    tree = await dc.decompose("ship it", stubborn, max_depth=2, review_rounds=1)
    assert [r["approved"] for r in tree.reviews] == [False] and len(tree.leaves()) == 4


async def test_limits_and_an_empty_goal_are_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        await dc.decompose("  ", Planner(PLAN))
    with pytest.raises(ValueError, match="limits"):
        await dc.decompose("x", Planner(PLAN), max_children=1)


# -- what the tree becomes -------------------------------------------------------------------


async def test_the_leaves_compile_to_nodes_the_graph_tool_accepts() -> None:
    from raven.agent.subagent.dag_graph import parse_dag_spec, validate_and_order

    tree = await dc.decompose("ship the feature", Planner(PLAN), max_depth=2, plan_id="plan-abc123")

    nodes = dc.to_dag_nodes(tree, agent="Raven")
    spec = parse_dag_spec({"task_summary": tree.goal, "nodes": nodes})
    order = validate_and_order(spec)

    by_id = {n["id"]: n for n in nodes}
    assert by_id["plan-abc123_build"]["depends_on"] == ["plan-abc123_design_api"]
    assert "{{ plan-abc123_design_api.output }}" in by_id["plan-abc123_build"]["prompt_template"]
    assert "ship the feature" in by_id["plan-abc123_design_api"]["prompt_template"]
    assert "- Design: design it" in by_id["plan-abc123_design_api"]["prompt_template"]
    assert (
        order.index("plan-abc123_design_schema")
        < order.index("plan-abc123_design_api")
        < order.index("plan-abc123_build")
    )


async def test_a_leaf_is_handed_the_results_it_waits_on() -> None:
    tree = await dc.decompose("ship the feature", Planner(PLAN), max_depth=2)

    prompt = dc.leaf_prompt(tree, tree.find("build"), {"design.api": "GET /items returns the list"})

    assert "Your part: Build" in prompt and "Result of the earlier part 'API' (design.api):" in prompt
    assert "GET /items returns the list" in prompt


async def test_the_tree_is_saved_whole_and_read_back(tmp_path: Path) -> None:
    tree = await dc.decompose("ship the feature", Planner(PLAN), max_depth=2, plan_id="plan-abc123")
    tree.find("docs").status = "done"

    data, page = dc.save(tree, dc.plan_dir(tmp_path, tree.id))
    back = dc.load(data.parent)

    assert data == tmp_path / ".raven" / "plans" / "plan-abc123" / "tree.json"
    assert back.to_dict() == tree.to_dict()
    outline = page.read_text(encoding="utf-8")
    assert "+ design: Design" in outline and "  - design.api: API (after schema)" in outline
    assert "- docs: Docs [done]" in outline and "4 leaves in 3 level(s):" in outline


# -- the command and the scenario ------------------------------------------------------------

PY = sys.executable.replace("\\", "/")

AGENT = r"""
import json, re, sys
p = sys.stdin.read()
m = re.search(r"The task to split: (.+)", p)
if m:
    plans = %s
    print(json.dumps(plans.get(m.group(1).strip(), {"atomic": True})))
elif "Several agents each did one part" in p:
    print("SYNTHESIS of " + str(p.count("## ")) + " parts")
else:
    part = re.search(r"Your part: (.+)", p).group(1).strip()
    if part == "%s":
        sys.stderr.write("cannot"); sys.exit(4)
    print("did " + part + (" using earlier results" if "Result of the earlier part" in p else ""))
"""


def _agent_file(tmp_path: Path, fail: str = "") -> str:
    script = tmp_path / "agent.py"
    script.write_text(AGENT % (repr(PLAN), fail), encoding="utf-8")
    return f'"{PY}" "{script.as_posix()}"'


def test_the_command_saves_the_plan_and_prints_it_or_its_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.cli.commands import app

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"subagents": {"agents": [{"name": "planner", "kind": "cli", "command": _agent_file(tmp_path)}]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RARUS_HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()

    result = CliRunner().invoke(
        app, ["playbook", "decompose", "ship the feature", "--agent", "planner", "--depth", "2", "-p", str(project)]
    )

    assert result.exit_code == 0, result.output
    assert "+ design: Design" in result.output and "4 leaves in 3 level(s):" in result.output
    saved = list((project / ".raven" / "plans").glob("plan-*/tree.json"))
    assert len(saved) == 1 and dc.load(saved[0]).find("design.api") is not None

    result = CliRunner().invoke(
        app,
        [
            "playbook",
            "decompose",
            "ship the feature",
            "--agent",
            "planner",
            "--depth",
            "2",
            "-p",
            str(project),
            "--dag",
        ],
    )
    nodes = json.loads(result.output[result.output.index("[") :].split("\nplan saved")[0])
    assert {n["subagent"] for n in nodes} == {"Raven"} and len(nodes) == 4


async def _run_decompose_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: str = ""):
    from raven.agent.subagent.manager import SubagentManager
    from raven.config.schema import ThirdPartyCliSubagentConfig
    from raven.scenario.catalog import discover
    from raven.scenario.runner import run_scenario

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("RARUS_HOME", str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    manager = SubagentManager(
        provider=type("_NoModel", (), {"get_default_model": lambda self: "none"})(),  # type: ignore[arg-type]
        workspace=workspace,
        agents=[ThirdPartyCliSubagentConfig(name="a", command=_agent_file(tmp_path, fail))],
    )
    args = {"goal": "ship the feature", "planner": "a", "worker": "a", "synthesizer": "a", "depth": "2"}
    outcome = await run_scenario(discover(workspace)["decompose"], args, manager=manager, workspace=workspace)
    trees = list((workspace / ".raven" / "plans").glob("plan-*/tree.json"))
    return outcome, dc.load(trees[0]) if trees else None


async def test_the_scenario_plans_runs_the_leaves_in_order_and_puts_them_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, tree = await _run_decompose_scenario(tmp_path, monkeypatch)

    assert outcome.ok, outcome.error
    assert outcome.result.startswith("SYNTHESIS of 4 parts") and "4 of 4 parts done" in outcome.result
    assert {t.id: t.status for t in tree.leaves()} == {
        "build": "done",
        "docs": "done",
        "design.schema": "done",
        "design.api": "done",
    }
    assert tree.find("build").output == "did Build using earlier results"
    names = [r["name"] for r in outcome.runs]
    # The api ran after the schema, and the build after the api.
    assert names.index("design.schema") < names.index("design.api") < names.index("build")


async def test_a_failed_leaf_skips_what_waits_on_it_and_the_rest_still_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, tree = await _run_decompose_scenario(tmp_path, monkeypatch, fail="API")

    assert outcome.ok, outcome.error
    states = {t.id: t.status for t in tree.leaves()}
    assert states == {"design.schema": "done", "docs": "done", "design.api": "failed", "build": "skipped"}
    assert "2 of 4 parts done" in outcome.result and "SYNTHESIS of 2 parts" in outcome.result

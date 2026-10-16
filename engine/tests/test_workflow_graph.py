"""Workflow node types (raven/playbook/node_types.py) and editing a graph as written (graph_edit.py, playbooks.graph.*)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from raven.playbook import PlaybookStore
from raven.playbook import graph_edit as ge
from raven.playbook.node_types import NO_PREVIOUS, NodeTypeError, expand_nodes, expand_spec_data
from raven.playbook.validate import validate_structure
from raven.rpc.methods import playbooks as mod
from raven.rpc.models import METHOD_MODELS


def _step(nid: str, **over: Any) -> dict[str, Any]:
    return {"id": nid, "subagent": "Raven", "node_summary": nid, "prompt_template": f"do {nid}", **over}


def _by_id(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in nodes}


# -- expansion -------------------------------------------------------------------------------


def test_a_graph_with_no_typed_step_is_passed_through_untouched() -> None:
    nodes = [_step("a"), _step("b", depends_on=["a"])]

    assert expand_nodes(nodes) is nodes


def test_for_each_runs_one_copy_per_item_and_a_dependent_waits_for_them_all() -> None:
    nodes = [
        _step("prep"),
        _step(
            "review",
            forEach=["api.py", "views.py"],
            prompt_template="Review {{ item }} ({{ item_index }})",
            depends_on=["prep"],
        ),
        _step("report", depends_on=["review"]),
    ]

    out = _by_id(expand_nodes(nodes))

    assert list(out) == ["prep", "review-1", "review-2", "report"]
    assert (
        out["review-1"]["prompt_template"] == "Review api.py (1)"
        and out["review-2"]["prompt_template"] == "Review views.py (2)"
    )
    assert out["review-2"]["node_summary"] == "review (2/2: views.py)"
    assert out["review-1"]["dependsOn"] == ["prep"] and out["report"]["dependsOn"] == ["review-1", "review-2"]
    assert "forEach" not in out["review-1"]


def test_collect_keeps_the_id_so_a_dependent_reads_the_collected_answer() -> None:
    nodes = [
        _step(
            "review", forEach=["a", "b"], collect={"node_summary": "Merge", "prompt_template": "Merge:\n{{ results }}"}
        ),
        _step("report", depends_on=["review"], prompt_template="Write up {{ review.output }}"),
    ]

    out = _by_id(expand_nodes(nodes))

    assert list(out) == ["review-1", "review-2", "review", "report"]
    merge = out["review"]
    assert merge["dependsOn"] == ["review-1", "review-2"] and merge["subagent"] == "Raven"
    assert "{{ review-1.output }}" in merge["prompt_template"] and "{{ review-2.output }}" in merge["prompt_template"]
    assert out["report"]["depends_on"] == ["review"], "unchanged, so left as written"
    assert out["report"]["prompt_template"] == "Write up {{ review.output }}"


def test_repeat_chains_its_passes_and_reading_it_reads_the_last() -> None:
    nodes = [
        _step("draft"),
        _step("polish", repeat=3, depends_on=["draft"], prompt_template="Improve: {{ previous }}"),
        _step(
            "publish",
            depends_on=["polish"],
            prompt_template="Publish {{ polish.output }}",
            inputs={"text": {"node": "polish"}},
        ),
    ]

    out = _by_id(expand_nodes(nodes))

    assert [n for n in out if n.startswith("polish")] == ["polish-1", "polish-2", "polish-3"]
    assert out["polish-1"]["prompt_template"] == f"Improve: {NO_PREVIOUS}" and out["polish-1"]["dependsOn"] == ["draft"]
    assert out["polish-3"]["prompt_template"] == "Improve: {{ polish-2.output }}"
    assert out["polish-3"]["dependsOn"] == ["draft", "polish-2"]
    assert out["publish"]["dependsOn"] == ["polish-3"]
    assert out["publish"]["prompt_template"] == "Publish {{ polish-3.output }}"
    assert out["publish"]["inputs"] == {"text": {"node": "polish-3"}}


def test_branches_are_steps_of_their_own_that_inherit_what_they_leave_out() -> None:
    nodes = [
        _step("start"),
        _step(
            "angles",
            depends_on=["start"],
            branches=[{"subagent": "Raven-Research", "prompt_template": "the market"}, {"prompt_template": "the tech"}],
        ),
        _step("merge", depends_on=["angles"]),
    ]

    out = _by_id(expand_nodes(nodes))

    assert out["angles-1"]["subagent"] == "Raven-Research" and out["angles-2"]["subagent"] == "Raven"
    assert out["angles-2"]["dependsOn"] == ["start"] and out["merge"]["dependsOn"] == ["angles-1", "angles-2"]


@pytest.mark.parametrize(
    ("node", "said"),
    [
        (_step("x", forEach=[]), "non-empty list"),
        (_step("x", forEach=list(range(21))), "at most 20"),
        (_step("x", forEach=[{"a": 1}]), "plain values"),
        (_step("x", repeat=1), "from 2 to 10"),
        (_step("x", repeat=True), "from 2 to 10"),
        (_step("x", branches=[{}]), "2 to 10 steps"),
        (_step("x", forEach=["a"], repeat=2), "not several"),
        ({"forEach": ["a"]}, "needs an id"),
    ],
)
def test_a_typed_step_written_wrong_is_refused_with_why(node: dict, said: str) -> None:
    with pytest.raises(NodeTypeError, match=said):
        expand_nodes([node])


def test_reading_the_output_of_a_fan_out_without_collect_is_refused() -> None:
    with pytest.raises(NodeTypeError, match="give 'review' a collect step"):
        expand_nodes([_step("review", forEach=["a", "b"]), _step("report", prompt_template="{{ review.output }}")])


def test_both_playbook_shapes_are_expanded_where_their_steps_live() -> None:
    v1 = expand_spec_data({"nodes": [_step("r", forEach=["a", "b"])]})
    v2 = expand_spec_data({"workflow": {"nodes": [_step("r", repeat=2)]}})

    assert [n["id"] for n in v1["nodes"]] == ["r-1", "r-2"] and [n["id"] for n in v2["workflow"]["nodes"]] == [
        "r-1",
        "r-2",
    ]


# -- a playbook file with typed steps --------------------------------------------------------

FILE = """---
name: review-each
description: review every file then merge
---

# review-each

Prose a person wrote, kept as it is.

```yaml playbook-spec
mode: dag
triggers:
  keywords: [review]
nodes:
  - id: review
    subagent: Raven
    node_summary: Review a file
    prompt_template: "Review {{ item }}"
    forEach: [api.py, views.py]
    collect:
      node_summary: Merge
      prompt_template: "Merge:\\n{{ results }}"
  - id: report
    subagent: Raven
    node_summary: Report
    prompt_template: "Write up {{ review.output }}"
    depends_on: [review]
```
"""


@pytest.fixture
def store(tmp_path: Path) -> PlaybookStore:
    s = PlaybookStore(tmp_path / "user", builtin_root=tmp_path / "builtin")
    (tmp_path / "user" / "review-each").mkdir(parents=True)
    (tmp_path / "user" / "review-each" / "playbook.md").write_text(FILE, encoding="utf-8")
    return s


def test_a_playbook_with_typed_steps_loads_as_the_plain_graph_that_runs(store: PlaybookStore) -> None:
    spec = store.load("review-each")

    assert [n.id for n in spec.nodes] == ["review-1", "review-2", "review", "report"]
    assert validate_structure(spec, known_agents=["Raven"]) == []
    assert spec.nodes[0].prompt_template == "Review api.py"


def test_a_typed_step_the_load_cannot_expand_names_the_playbook(store: PlaybookStore, tmp_path: Path) -> None:
    path = tmp_path / "user" / "review-each" / "playbook.md"
    path.write_text(FILE.replace("forEach: [api.py, views.py]", "forEach: []"), encoding="utf-8")

    with pytest.raises(ValueError, match="playbook 'review-each': node 'review': forEach"):
        store.load("review-each")


# -- reading, checking and saving a graph as written ------------------------------------------


def test_reading_a_graph_gives_it_as_written_beside_what_it_runs_as(store: PlaybookStore) -> None:
    got = ge.read_graph(store, "review-each")

    assert [n["id"] for n in got["nodes"]] == ["review", "report"] and "forEach" in got["nodes"][0]
    assert [n["id"] for n in got["expanded"]] == ["review-1", "review-2", "review", "report"]
    assert got["expanded"][3]["depends_on"] == ["review"] and got["errors"] == []
    assert got["origin"] == "user" and got["description"] == "review every file then merge"


def test_a_graph_being_edited_is_checked_like_a_load() -> None:
    assert ge.check_graph([_step("a"), _step("b", depends_on=["ghost"])])["errors"]
    assert "forEach" in ge.check_graph([_step("a", forEach=[])])["errors"][0]
    assert ge.check_graph([_step("a", subagent="Nobody")], known_agents=["Raven"])["errors"]
    assert ge.check_graph([_step("a"), _step("b", depends_on=["a"])], known_agents=["Raven"])["errors"] == []
    assert ge.check_graph("not a list")["errors"] == ["the graph is a list of steps"]  # type: ignore[arg-type]


def test_saving_over_a_playbook_changes_its_steps_and_nothing_else(store: PlaybookStore, tmp_path: Path) -> None:
    nodes = ge.read_graph(store, "review-each")["nodes"]
    nodes[0]["forEach"] = ["api.py", "views.py", "models.py"]

    with pytest.raises(ge.GraphEditError, match="save over it explicitly"):
        ge.save_graph(store, "review-each", nodes)
    out = ge.save_graph(store, "review-each", nodes, overwrite=True, known_agents=["Raven"])

    text = (tmp_path / "user" / "review-each" / "playbook.md").read_text(encoding="utf-8")
    assert "Prose a person wrote, kept as it is." in text and "keywords:" in text
    assert "forEach:" in text and "models.py" in text, "the typed step is saved as written, not flattened"
    assert [n["id"] for n in out["expanded"]][:3] == ["review-1", "review-2", "review-3"]
    assert [n.id for n in store.load("review-each").nodes][:3] == ["review-1", "review-2", "review-3"]


def test_a_new_workflow_needs_a_description_and_a_builtin_is_shadowed_not_edited(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin" / "shipped"
    builtin.mkdir(parents=True)
    (builtin / "playbook.md").write_text(FILE.replace("review-each", "shipped"), encoding="utf-8")
    store = PlaybookStore(tmp_path / "user", builtin_root=tmp_path / "builtin")

    with pytest.raises(ge.GraphEditError, match="needs a description"):
        ge.save_graph(store, "fresh", [_step("a")])
    made = ge.save_graph(store, "fresh", [_step("a"), _step("b", repeat=2, depends_on=["a"])], description="two steps")
    assert made["origin"] == "user" and [n.id for n in store.load("fresh").nodes] == ["a", "b-1", "b-2"]

    shadow = ge.save_graph(store, "shipped", [_step("only")], overwrite=True)
    assert shadow["shadows_builtin"] and store.origin_of("shipped") == "user"
    assert "forEach" in (builtin / "playbook.md").read_text(encoding="utf-8"), "the builtin file is untouched"


def test_nothing_is_written_when_the_graph_has_a_finding_or_the_result_would_not_load(
    store: PlaybookStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "user" / "review-each" / "playbook.md"
    with pytest.raises(ge.GraphEditError, match="ghost"):
        ge.save_graph(store, "review-each", [_step("a", depends_on=["ghost"])], overwrite=True)
    assert path.read_text(encoding="utf-8") == FILE

    def broken(name: str) -> None:
        raise ValueError("refused on load")

    monkeypatch.setattr(store, "load", broken)
    with pytest.raises(ge.GraphEditError, match="would not load"):
        ge.save_graph(store, "review-each", [_step("a")], overwrite=True)
    assert path.read_text(encoding="utf-8") == FILE, "the previous file is put back"


def test_a_name_that_is_not_kebab_case_or_a_prompt_playbook_is_refused(store: PlaybookStore, tmp_path: Path) -> None:
    with pytest.raises(ge.GraphEditError, match="must match"):
        ge.save_graph(store, "../escape", [_step("a")], description="x")
    prompt = tmp_path / "user" / "talky"
    prompt.mkdir()
    (prompt / "playbook.md").write_text(
        "---\nname: talky\ndescription: d\n---\n\n```yaml playbook-spec\nmode: prompt\nprompts: {}\n```\n",
        encoding="utf-8",
    )
    with pytest.raises(ge.GraphEditError, match="not a graph"):
        ge.read_graph(store, "talky")


# -- over the wire ---------------------------------------------------------------------------


@pytest.fixture
def wired(store: PlaybookStore, monkeypatch: pytest.MonkeyPatch) -> PlaybookStore:
    monkeypatch.setattr(mod, "_store", lambda: store)
    monkeypatch.setattr(mod, "_known_agent_names", lambda factory: ["Raven"])
    return store


async def test_the_editor_methods_answer_in_their_contract(wired: PlaybookStore) -> None:
    got = await mod.playbooks_graph_get({"name": "review-each"})
    checked = await mod.playbooks_graph_check({"nodes": got["nodes"]})
    saved = await mod.playbooks_graph_save(
        {"name": "review-copy", "nodes": got["nodes"], "description": "a copy", "overwrite": False}
    )

    METHOD_MODELS["playbooks.graph.get"][1].model_validate(got)
    METHOD_MODELS["playbooks.graph.check"][1].model_validate(checked)
    METHOD_MODELS["playbooks.graph.save"][1].model_validate(saved)
    assert checked["errors"] == [] and saved["name"] == "review-copy" and not saved["shadows_builtin"]


async def test_the_editor_methods_refuse_in_words(wired: PlaybookStore) -> None:
    from raven.rpc.errors import ConfigValidationError

    with pytest.raises(ConfigValidationError, match="no playbook named nope"):
        await mod.playbooks_graph_get({"name": "nope"})
    with pytest.raises(ConfigValidationError, match="at least one step"):
        await mod.playbooks_graph_save({"name": "x", "nodes": []})
    with pytest.raises(ConfigValidationError, match="ghost"):
        await mod.playbooks_graph_save({"name": "x", "nodes": [_step("a", depends_on=["ghost"])], "description": "d"})
    assert (await mod.playbooks_graph_check({"nodes": [_step("a", subagent="Nobody")]}))["errors"]

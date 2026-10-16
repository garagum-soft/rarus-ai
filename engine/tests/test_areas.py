"""Areas of responsibility (raven/agent/subagent/areas.py): owners, a journal, a standing duty, and spawn."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from raven.agent.subagent import areas as ar
from raven.proactive_engine.attention import AttentionQueue


@pytest.fixture
def store(tmp_path: Path) -> ar.AreaStore:
    return ar.AreaStore(tmp_path / "home" / "areas.json")


def _area(name: str = "routing", **over: Any) -> ar.Area:
    base = {"agent": "Raven-Code", "project": "C:/work/raven", "paths": ["raven/routing/**"], "topics": ["routing"]}
    return ar.Area(name=name, **{**base, **over})


# -- what an area is -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "said"),
    [
        ({"name": "Bad Name!"}, "lowercase"),
        ({"agent": " "}, "needs an owner"),
        ({"paths": [], "topics": [], "charter": ""}, "something to cover"),
        ({"every_s": 60}, "at most every"),
    ],
)
def test_an_area_that_cannot_be_kept_says_why(over: dict, said: str) -> None:
    with pytest.raises(ar.AreaError, match=said):
        ar.check_area(_area(**over))


def test_names_and_patterns_are_kept_in_one_spelling() -> None:
    area = ar.check_area(_area(name=" Routing ", paths=["raven\\routing\\**", " "], topics=["Model Tiers"]))

    assert area.name == "routing" and area.paths == ["raven/routing/**"] and area.topics == ["model tiers"]


@pytest.mark.parametrize(
    ("text", "seconds"), [("off", 0), ("", 0), ("900", 900), ("90m", 5400), ("6h", 21600), ("1d", 86400)]
)
def test_a_duty_interval_is_read_in_the_usual_units(text: str, seconds: int) -> None:
    assert ar.parse_every(text) == seconds


def test_an_unreadable_interval_is_refused() -> None:
    with pytest.raises(ar.AreaError, match="90m"):
        ar.parse_every("every tuesday")


# -- the store and the journal ---------------------------------------------------------------


def test_areas_outlive_the_process_and_their_journal_outlives_the_area(store: ar.AreaStore) -> None:
    store.put(_area())
    store.put(_area("ui", paths=["ui-web/**"], topics=[]))
    store.note("routing", {"at": 1.0, "kind": "task", "task": "t", "ok": True, "report": "r"})

    again = ar.AreaStore(store.path)
    assert [a.name for a in again.areas()] == ["routing", "ui"]
    assert again.get("ROUTING").agent == "Raven-Code"
    assert again.journal("routing") == [{"at": 1.0, "kind": "task", "task": "t", "ok": True, "report": "r"}]

    assert again.remove("routing") and not again.remove("routing")
    assert again.journal("routing")[0]["report"] == "r"


def test_a_run_is_stamped_on_the_stored_area(store: ar.AreaStore) -> None:
    store.put(_area())

    store.stamp("routing", at=123.0, status="attention")
    store.stamp("gone", at=1.0, status="ok")

    area = store.get("routing")
    assert (area.last_run_at, area.last_status) == (123.0, "attention")


# -- who owns what ---------------------------------------------------------------------------


def test_the_narrowest_path_pattern_owns_a_path() -> None:
    areas = [
        _area("raven", paths=["raven/**"], topics=[]),
        _area("routing", paths=["raven/routing/**"], topics=[]),
        _area("ui", paths=["ui-web/**"], topics=[]),
    ]

    assert [a.name for a in ar.owners_of_path(areas, "raven/routing/tiers.py")] == ["routing", "raven"]
    # Absolute, in the project's own checkout, and with Windows separators.
    assert ar.owners_of_path(areas, "C:\\work\\raven\\raven\\routing\\x.py")[0].name == "routing"
    assert ar.owners_of_path(areas, "docs/index.md") == []
    # A plain directory covers everything under it -- the spelling that survives a Windows command line.
    plain = [_area("routing", paths=["raven/routing"], topics=[])]
    assert ar.owners_of_path(plain, "raven/routing/tiers.py") and ar.owners_of_path(plain, "raven/routing")
    assert ar.owners_of_path(plain, "raven/routing_old/x.py") == []


def test_text_finds_its_owner_by_the_paths_it_names_then_by_topic() -> None:
    areas = [
        _area("routing", paths=["raven/routing/**"], topics=["routing", "model tiers"]),
        _area("ui", paths=["ui-web/**"], topics=["page", "settings"]),
    ]

    assert [a.name for a in ar.owners_of_text(areas, "Fix the crash in ui-web/src/App.tsx")] == ["ui"]
    assert [a.name for a in ar.owners_of_text(areas, "Add a fourth rung to the model tiers")] == ["routing"]
    assert [a.name for a in ar.owners_of_text(areas, "The settings page and routing both")] == ["routing", "ui"]
    assert ar.owners_of_text(areas, "write a poem") == []


# -- a run in an area ------------------------------------------------------------------------


def test_the_brief_carries_the_charter_and_what_the_owner_did_lately() -> None:
    area = _area(charter="Keep the tier table in step with the providers.")
    journal = [{"at": 0, "kind": "task", "task": f"task {i}", "report": f"did {i}\nmore"} for i in range(7)]

    first = ar.brief(area, [])
    later = ar.brief(area, journal)

    assert first.startswith("You own the area 'routing' in C:/work/raven.") and "first run" in first
    assert "Keep the tier table in step" in later and "-> did 6" in later
    # Only the latest few entries, and only their first line.
    assert "task 1" not in later and "more" not in later


class _Owner:
    def __init__(self, answer: str = "Checked the tiers. All in step.", fail: bool = False) -> None:
        self.answer, self.fail = answer, fail
        self.calls: list[tuple[str, str, Path]] = []

    async def __call__(self, agent: str, prompt: str, workspace: Path) -> str:
        self.calls.append((agent, prompt, workspace))
        if self.fail:
            raise RuntimeError("model down")
        return self.answer


async def test_a_task_in_an_area_is_briefed_journaled_and_the_next_run_sees_it(
    store: ar.AreaStore, tmp_path: Path
) -> None:
    store.put(_area(project=str(tmp_path)))
    owner = _Owner("Added the rung.")

    entry = await ar.run_area(store, store.get("routing"), owner, task="Add a fourth rung")
    await ar.run_area(store, store.get("routing"), owner)

    agent, prompt, where = owner.calls[0]
    assert agent == "Raven-Code" and where == tmp_path
    assert "first run" in prompt and prompt.endswith("Your task now:\nAdd a fourth rung")
    assert entry["ok"] and entry["kind"] == "task" and entry["report"] == "Added the rung."
    # The duty after it is handed what the task did, and asked for its standing duty.
    assert "-> Added the rung." in owner.calls[1][1] and owner.calls[1][1].endswith(ar.DUTY)
    assert [e["kind"] for e in store.journal("routing")] == ["task", "duty"]
    assert store.get("routing").last_status == "ok"


async def test_attention_lines_in_a_report_are_raised_for_a_person(store: ar.AreaStore, tmp_path: Path) -> None:
    store.put(_area())
    queue = AttentionQueue(tmp_path / "attention.json")
    owner = _Owner("Looked over routing.\nATTENTION: the big tier points at a retired model\nNothing else.")

    entry = await ar.run_area(store, store.get("routing"), owner, queue=queue)

    assert entry["attention"] == ["the big tier points at a retired model"]
    items = queue.items()
    titles = [i.title for i in items]
    assert titles == ["routing: the big tier points at a retired model"]
    assert store.get("routing").last_status == "attention"


async def test_a_failed_run_is_journaled_not_raised(store: ar.AreaStore) -> None:
    store.put(_area())

    entry = await ar.run_area(store, store.get("routing"), _Owner(fail=True))

    assert not entry["ok"] and "model down" in entry["error"]
    assert store.journal("routing")[-1]["ok"] is False and store.get("routing").last_status == "failed"


# -- the standing duty -----------------------------------------------------------------------


async def test_the_resident_pass_runs_only_the_areas_that_are_due(store: ar.AreaStore) -> None:
    store.put(_area("routing", every_s=3600, last_run_at=3000.0))
    store.put(_area("ui", paths=["ui-web/**"], every_s=3600, last_run_at=0.0))
    store.put(_area("docs", paths=["docs/**"]))
    owner = _Owner()
    watch = ar.AreaWatch(store, lambda: owner)

    done = await watch.once(now=5000.0)

    assert [e["task"] for e in done] == ["standing duty"]
    assert [c[1].startswith("You own the area 'ui'") for c in owner.calls] == [True]
    assert await ar.AreaWatch(store, lambda: None).once(now=99999.0) == []


def test_the_gateway_keeps_watch_only_when_an_area_asked_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.core.proactive_stack import build_area_watch

    path = tmp_path / "home" / "areas.json"
    monkeypatch.setattr(ar, "default_path", lambda: path)
    assert build_area_watch(None, lambda: None) is None
    ar.AreaStore(path).put(_area())
    assert build_area_watch(None, lambda: None) is None
    ar.AreaStore(path).put(_area("ui", paths=["ui-web/**"], every_s=3600))
    assert isinstance(build_area_watch(None, lambda: None), ar.AreaWatch)


# -- spawn -----------------------------------------------------------------------------------


def test_a_spawn_into_an_owners_area_is_briefed_and_any_other_is_not(store: ar.AreaStore) -> None:
    store.put(_area(charter="Keep it in step."))

    assert ar.spawn_brief("Raven-Code", "Add a rung to the model tiers in raven/routing/tiers.py", store).startswith(
        "You own the area 'routing'"
    )
    assert ar.spawn_brief("Raven-Code", "write a poem", store) == ""
    assert ar.spawn_brief("Raven-Design", "the routing tiers", store) == ""


def test_a_spawned_result_lands_in_the_journal_of_the_area_it_was_briefed_for(store: ar.AreaStore) -> None:
    store.put(_area())
    task = ar.spawn_brief("Raven-Code", "routing work", store) + "Do the routing work."

    assert ar.note_spawn_result(task, "Routing work", "Did it.", "ok", store)
    assert not ar.note_spawn_result("Do something else.", "x", "y", "ok", store)

    entry = store.journal("routing")[-1]
    assert (entry["kind"], entry["task"], entry["report"], entry["ok"]) == ("assigned", "Routing work", "Did it.", True)


async def test_spawn_hands_the_owner_its_brief_and_journals_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real spawn tool and manager: the brief rides the task, the answer reaches the journal."""
    from raven.agent.subagent import manager as manager_mod
    from raven.agent.subagent.manager import SubagentManager
    from raven.agent.subagent.spawn_tool import SpawnTool
    from raven.config.schema import ThirdPartyCliSubagentConfig
    from raven.session.manager import SessionManager

    path = tmp_path / "areas.json"
    monkeypatch.setattr(ar, "default_path", lambda: path)
    ar.AreaStore(path).put(_area(charter="Keep it in step."))
    home = tmp_path / "home"
    seen: list[str] = []

    class _Recorder:
        streams = False

        async def run(self, task: str, **kwargs: Any) -> str:
            seen.append(task)
            return "Added the rung."

    class _Exec:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    mgr = SubagentManager(
        provider=type("_P", (), {"get_default_model": lambda self: "stub"})(),  # type: ignore[arg-type]
        workspace=home,
        session_dir=lambda key: SessionManager(home).session_dir(key),
        agents=[ThirdPartyCliSubagentConfig(name=n, command="cat {prompt}") for n in ("Raven-Code", "Raven-Design")],
    )
    mgr.set_submit(lambda _s: None)
    for name in ("Raven-Code", "Raven-Design"):
        mgr.registry._backends[name] = _Recorder()
    monkeypatch.setattr(manager_mod, "build_executor", lambda *a, **k: _Exec())
    tool = SpawnTool(manager=mgr)
    tool.set_context("cli", "direct", "cli:direct")

    out = await tool.execute(
        task_summary="New tier rung",
        node_id="n1",
        prompt_template="Add a rung to the routing tiers",
        subagent="Raven-Code",
    )
    other = await tool.execute(
        task_summary="Logo", node_id="n2", prompt_template="Draw a logo", subagent="Raven-Design"
    )
    for _ in range(400):
        if len(seen) >= 2 and ar.AreaStore(path).journal("routing"):
            break
        await asyncio.sleep(0.01)

    assert not out.startswith("Error:") and not other.startswith("Error:")
    briefed = next(t for t in seen if "routing tiers" in t)
    assert briefed.startswith("You own the area 'routing'") and "Keep it in step." in briefed
    assert next(t for t in seen if "logo" in t) == "Draw a logo"
    entry = ar.AreaStore(path).journal("routing")[-1]
    assert entry["kind"] == "assigned" and entry["task"] == "New tier rung" and entry["report"] == "Added the rung."


# -- the command line ------------------------------------------------------------------------


def test_the_command_assigns_lists_finds_the_owner_and_reads_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.cli.commands import app

    path = tmp_path / "home" / "areas.json"
    monkeypatch.setattr(ar, "default_path", lambda: path)
    project = tmp_path / "proj"
    (project / "raven" / "routing").mkdir(parents=True)
    (project / "raven" / "routing" / "tiers.py").write_text("x = 1\n", encoding="utf-8")
    runner = CliRunner()

    done = runner.invoke(
        app,
        [
            "areas",
            "assign",
            "routing",
            "-a",
            "Raven-Code",
            "-p",
            "raven/routing/**",
            "-t",
            "tiers",
            "--project",
            str(project),
            "--every",
            "6h",
        ],
    )
    assert done.exit_code == 0, done.output
    assert "Raven-Code owns 'routing'" in done.output and "every 21600s" in done.output

    assert "routing  owner Raven-Code  every 21600s  last run never" in runner.invoke(app, ["areas", "list"]).output
    who = runner.invoke(app, ["areas", "who", str(project / "raven" / "routing" / "tiers.py")])
    assert who.exit_code == 0 and "Raven-Code  (area 'routing')" in who.output
    assert runner.invoke(app, ["areas", "who", "a poem"]).exit_code == 1

    ar.AreaStore(path).note(
        "routing",
        {"at": 1.0, "kind": "duty", "task": "standing duty", "ok": True, "report": "All fine.", "attention": ["look"]},
    )
    log = runner.invoke(app, ["areas", "log", "routing"]).output
    assert "duty  ok  standing duty" in log and "All fine." in log and "! look" in log

    bad = runner.invoke(app, ["areas", "assign", "x", "-a", "A", "--every", "1m", "-t", "t"])
    assert bad.exit_code == 2 and "at most every" in bad.output
    assert runner.invoke(app, ["areas", "remove", "routing"]).exit_code == 0

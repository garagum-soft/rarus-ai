"""Teams sharing one Raven: each conversation's team has its own budget and slots."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from raven.agent.subagent import manager as manager_mod
from raven.agent.subagent.manager import SPAWN_REFUSED_PREFIX, SubagentManager
from raven.agent.subagent.teams import Team, TeamTable
from raven.config.schema import SubagentsConfig, TeamConfig, ThirdPartyCliSubagentConfig

ACME = Team("acme", ("web:acme-*",), max_spawns_per_hour=2, max_concurrent=1)
GLOBEX = Team("globex", ("web:globex-*", "tui:*"), max_spawns_per_hour=5)


def test_a_conversation_belongs_to_the_first_team_that_claims_it() -> None:
    table = TeamTable([ACME, GLOBEX])

    assert table.team_of("web:acme-1") is ACME
    assert table.team_of("tui:20261013_x") is GLOBEX
    assert table.team_of("web:initech-1") is None
    with pytest.raises(ValueError, match="twice"):
        TeamTable([ACME, ACME])


def test_two_teams_with_different_limits_do_not_affect_each_other() -> None:
    table = TeamTable([ACME, GLOBEX])

    assert [table.charge("web:acme-1"), table.charge("web:acme-2")] == [None, None]
    refused = table.charge("web:acme-3")
    assert refused and "acme" in refused and "other teams are not affected" in refused
    assert all(table.charge(f"web:globex-{i}") is None for i in range(5)), (
        "acme spending its budget costs globex nothing"
    )
    assert table.charge("web:globex-9") is not None
    assert table.charge("web:initech-1") is None, "an unclaimed conversation has no team budget"


def test_the_budget_is_a_rolling_hour() -> None:
    now = [0.0]
    table = TeamTable([ACME], clock=lambda: now[0])
    table.charge("web:acme-1")
    table.charge("web:acme-1")
    assert table.charge("web:acme-1") is not None
    now[0] = 3601.0
    assert table.charge("web:acme-1") is None


async def test_a_team_at_its_cap_queues_only_its_own_work() -> None:
    table = TeamTable([ACME, GLOBEX])
    entered: list[str] = []

    async def hold(key: str, release: asyncio.Event) -> None:
        async with table.slot(key):
            entered.append(key)
            await release.wait()

    release = asyncio.Event()
    first = asyncio.create_task(hold("web:acme-1", release))
    second = asyncio.create_task(hold("web:acme-2", release))
    other = asyncio.create_task(hold("web:globex-1", release))
    await asyncio.sleep(0.05)

    assert entered == ["web:acme-1", "web:globex-1"], "acme's second dispatch waits; globex's does not"
    assert [row["in_flight"] for row in table.usage()] == [1, 1]
    release.set()
    await asyncio.gather(first, second, other)
    assert entered[-1] == "web:acme-2"


def test_teams_load_from_config() -> None:
    cfg = SubagentsConfig.model_validate(
        {"teams": [{"name": "acme", "sessions": ["web:acme-*"], "maxSpawnsPerHour": 2, "maxConcurrent": 1}]}
    )
    table = TeamTable.from_rows(cfg.teams)
    assert table.team_of("web:acme-7") is not None and table.teams[0].max_spawns_per_hour == 2
    with pytest.raises(ValueError):
        TeamConfig(name="x", max_spawns_per_hour=0)


class _Recorder:
    streams = False

    def __init__(self) -> None:
        self.tasks: list[str] = []

    async def run(self, task: str, **kwargs: Any) -> str:
        self.tasks.append(task)
        return "done"


class _StubProvider:
    def get_default_model(self) -> str:
        return "stub-model"


class _DummyExecutor:
    async def __aenter__(self) -> "_DummyExecutor":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


async def test_the_manager_refuses_a_spent_team_and_keeps_serving_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.session.manager import SessionManager

    home = tmp_path / "home"
    mgr = SubagentManager(
        provider=_StubProvider(),  # type: ignore[arg-type]
        workspace=home,
        session_dir=lambda key: SessionManager(home).session_dir(key),
        agents=[ThirdPartyCliSubagentConfig(name="worker", command="cat {prompt}")],
        teams=[
            TeamConfig(name="acme", sessions=["web:acme-*"], max_spawns_per_hour=1),
            TeamConfig(name="globex", sessions=["web:globex-*"], max_spawns_per_hour=3),
        ],
    )
    mgr.set_submit(lambda _s: None)
    recorder = _Recorder()
    mgr.registry._backends["worker"] = recorder
    monkeypatch.setattr(manager_mod, "build_executor", lambda *a, **k: _DummyExecutor())

    async def spawn(session: str, node: str) -> str:
        return await mgr.spawn(
            task=f"task {node}",
            node_id=node,
            task_summary=node,
            origin_channel="web",
            origin_chat_id=session.split(":", 1)[1],
            session_key=session,
            agent="worker",
        )

    first = await spawn("web:acme-1", "a1")
    refused = await spawn("web:acme-2", "a2")
    others = [await spawn("web:globex-1", f"g{i}") for i in range(3)]
    for _ in range(200):
        if len(recorder.tasks) >= 4:
            break
        await asyncio.sleep(0.01)

    assert not first.startswith(SPAWN_REFUSED_PREFIX)
    assert refused.startswith(SPAWN_REFUSED_PREFIX) and "team 'acme'" in refused
    assert not any(o.startswith(SPAWN_REFUSED_PREFIX) for o in others)
    assert sorted(recorder.tasks) == ["task a1", "task g0", "task g1", "task g2"]
    assert mgr.charge_dag_run("web:acme-3") is not None and "team 'acme'" in (mgr.charge_dag_run("web:acme-3") or "")

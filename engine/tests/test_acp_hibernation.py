"""Idle ACP agent connections are hibernated by the sweep and woken by the next request."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.acp_client import pool as pool_mod
from raven.acp_client.hibernation import IdleSweeper
from raven.config.schema import Config

_STUB = Path(__file__).with_name("acp_stub_server.py")


@pytest.fixture
async def pool():
    fresh = pool_mod.AcpConnectionPool()
    yield fresh
    await fresh.close_all()


async def _acquire(pool, name: str = "stub"):
    return await pool.acquire(
        name=name, command=f"{sys.executable} {_STUB}", env={"ACP_STUB_MODE": "ok"}, ready_timeout_s=15
    )


async def test_an_idle_connection_is_hibernated_and_the_next_request_wakes_it(pool) -> None:
    first = await _acquire(pool)
    assert first.alive

    slept = await pool.hibernate_idle(idle_after_s=0.0)

    assert slept == ["stub"]
    assert not first.alive, "the agent process was closed"
    assert "stub" in pool.hibernated() and pool.live("stub") == []

    woken = await _acquire(pool)

    assert woken is not first and woken.alive, "a new request launches the agent again"
    assert pool.hibernated() == {}


async def test_a_connection_used_recently_is_left_awake(pool) -> None:
    conn = await _acquire(pool)
    assert await pool.hibernate_idle(idle_after_s=3600) == []
    assert conn.alive


async def test_a_connection_with_a_turn_in_flight_is_never_hibernated() -> None:
    fresh = pool_mod.AcpConnectionPool()
    closed: list[str] = []

    class _Client:
        prompting = True
        idle_seconds = 10_000.0

        async def close(self) -> None:
            closed.append("x")

    fresh._connections["busy"] = {"": SimpleNamespace(client=_Client(), alive=True)}

    assert await fresh.hibernate_idle(idle_after_s=1) == []
    assert closed == []


async def test_the_sweep_runs_the_pool_hibernation(pool) -> None:
    await _acquire(pool)
    sweeper = IdleSweeper(pool=lambda: pool, idle_after_s=0.0)
    assert await sweeper.sweep() == ["stub"]


async def test_a_backend_turn_after_hibernation_still_answers(pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """The main path: a sub-agent dispatch on an agent that was put to sleep."""
    from raven.agent.subagent.backends import build_third_party_backend
    from raven.config.schema import ThirdPartyAcpSubagentConfig

    monkeypatch.setattr(pool_mod, "get_pool", lambda: pool)
    cfg = ThirdPartyAcpSubagentConfig(
        name="stub", command=f"{sys.executable} {_STUB}", env={"ACP_STUB_MODE": "ok"}, ready_timeout_ms=15000
    )
    backend = build_third_party_backend(cfg)
    backend._pool = pool

    class _Executor:
        pass

    async def turn(task_id: str) -> str:
        return await backend.run("say hi", task_id=task_id, workspace=Path.cwd(), executor=_Executor())

    first = await turn("t1")
    assert await pool.hibernate_idle(idle_after_s=0.0) == ["stub"]
    second = await turn("t2")

    assert first and second, "the agent answered before and after its sleep"
    assert pool.hibernated() == {}


def test_the_sweep_is_off_unless_configured() -> None:
    from raven.core.proactive_stack import build_idle_sweeper

    cfg = Config()
    assert build_idle_sweeper(cfg) is None
    cfg.subagents.hibernate_after_s = 900
    sweeper = build_idle_sweeper(cfg)
    assert sweeper is not None and sweeper.idle_after_s == 900 and sweeper.interval_s == 60.0

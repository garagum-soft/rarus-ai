"""Where an account's engine meets accounts mode, and that a Raven which is not one is untouched.

An engine at zero credits (or over its disk allowance) starts no new turn and no
background sub-agent; work already running finishes. An external agent's run is
written down for the account to be charged by. Without ``RAVEN_ACCOUNT`` none of
it happens, whatever files lie around.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.accounts import gate
from raven.accounts.estimate import external_usage_path
from raven.agent.subagent.manager import SubagentManager
from raven.contracts.llm_provider import LLMResponse
from raven.home import raven_home
from raven.providers.base import LLMProvider


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    home = tmp_path / "engine-home"
    home.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(home))
    monkeypatch.delenv(gate.ENV_ACCOUNT, raising=False)
    gate._cache.update(path=None, mtime=None, value=None)


class _Counting(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="k")
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.calls += 1
        return LLMResponse(content="hello back", finish_reason="stop")

    def get_default_model(self) -> str:
        return "m"


def _loop(tmp_path: Path, provider):
    from raven.agent.loop import AgentLoop
    from raven.agent.loop.bundles import EngineWiring
    from tests.test_agent_loop_model_selection import _stub_edges

    loop = AgentLoop(provider=provider, workspace=tmp_path, model="m", engine=EngineWiring())
    _stub_edges(loop)
    return loop


def _req(text: str = "hi", origin: str = "user"):
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    return TurnRequest(
        origin=Origin(origin),
        source=Source(channel="web", chat_id="c1", sender_id="u", chat_type=ChatType.DM),
        text=text,
        conversation="web:c1",
    )


async def _run(loop, req) -> list:
    events: list = []

    async def emit(event) -> None:
        events.append(event)

    await loop.run_turn(req, emit, lambda: [], stream=False)
    return events


def _block() -> None:
    gate.write_gate(raven_home(), blocked=True, reason=gate.NO_CREDITS)


async def test_a_blocked_accounts_engine_starts_no_new_turn(tmp_path, monkeypatch) -> None:
    from raven.spine.turn import AnswerlessTurnError

    monkeypatch.setenv(gate.ENV_ACCOUNT, "a1")
    _block()
    provider = _Counting()
    loop = _loop(tmp_path, provider)

    with pytest.raises(AnswerlessTurnError) as refused:
        await _run(loop, _req("write the report"))
    with pytest.raises(AnswerlessTurnError):
        await _run(loop, _req("nightly job", origin="cron"))

    assert str(refused.value) == gate.NO_CREDITS and provider.calls == 0


async def test_the_results_of_running_work_still_come_back(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(gate.ENV_ACCOUNT, "a1")
    _block()
    provider = _Counting()

    await _run(_loop(tmp_path, provider), _req("[sub-agent result] done", origin="subagent"))

    assert provider.calls >= 1


async def test_a_raven_that_is_not_an_accounts_engine_ignores_the_gate(tmp_path) -> None:
    _block()  # a stray file, with no RAVEN_ACCOUNT
    provider = _Counting()

    await _run(_loop(tmp_path, provider), _req("hello"))

    assert provider.calls >= 1


def _manager(tmp_path: Path) -> SubagentManager:
    return SubagentManager(provider=_Counting(), workspace=tmp_path)


async def test_a_background_spawn_is_refused_while_blocked_but_not_one_inside_a_turn(tmp_path, monkeypatch) -> None:
    from raven.token_wise import usage_context

    monkeypatch.setenv(gate.ENV_ACCOUNT, "a1")
    _block()
    mgr = _manager(tmp_path)
    started: list[str] = []

    async def fake_run(*args, **kwargs) -> None:
        started.append("run")

    monkeypatch.setattr(mgr, "_run_subagent", fake_run)

    outside = await mgr.spawn("watch the repo", task_summary="watch", session_key="web:c1")
    dag = mgr.charge_dag_run("web:c1")
    with usage_context.bind("web:c1"):
        inside = await mgr.spawn("finish the part", task_summary="part", session_key="web:c1")

    assert outside.startswith("Spawn refused: This account has no credits left")
    assert dag is not None and dag.startswith("Error: This account has no credits left")
    assert not inside.startswith("Spawn refused"), inside
    await mgr.cancel_all()


class _CliAgent:
    kind = "cli"
    command = "claude -p {prompt}"
    is_stateful = False

    async def run(self, task: str, **_kwargs) -> str:
        return "the poem is written"


async def _spawned_run(tmp_path: Path, monkeypatch) -> None:
    mgr = _manager(tmp_path)
    monkeypatch.setattr(mgr, "_resolve_backend", lambda _agent: _CliAgent())

    async def capture(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(mgr, "_announce_result", capture)
    await mgr._run_subagent_inner(
        "t1",
        "write a short poem about ravens",
        "poem",
        {"channel": "web", "chat_id": "c1", "session_key": "web:c1", "agent": "claude-code"},
        None,
        mgr.provider,
        mgr.model,
    )


async def test_an_external_agents_run_is_written_down_for_the_account(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(gate.ENV_ACCOUNT, "a1")

    await _spawned_run(tmp_path, monkeypatch)

    (row,) = [json.loads(line) for line in external_usage_path().read_text(encoding="utf-8").splitlines()]
    assert row["agent"] == "claude-code" and row["estimated"] is True and row["session_key"] == "web:c1"
    assert row["input_tokens"] > 0 and row["output_tokens"] > 0


async def test_without_accounts_mode_nothing_extra_is_written(tmp_path, monkeypatch) -> None:
    await _spawned_run(tmp_path, monkeypatch)

    assert not external_usage_path().exists()
    assert not (raven_home() / gate.GATE_FILE).exists()

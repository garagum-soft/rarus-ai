"""Group technical discussions: rounds, the round limit, and the saved protocol."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.subagent.discuss_tool import DiscussTool
from raven.agent.subagent.discussion import (
    CONSENSUS_MARKER,
    ProtocolStore,
    run_discussion,
)
from raven.agent.subagent.role import WITHHELD_FROM_SUBAGENT
from raven.providers.base import LLMProvider, LLMResponse
from tests._wiring import wire

AGENTS = ["alice", "bob", "carol"]


class _Room:
    """Three scripted agents sharing one thread; records every prompt it is given."""

    def __init__(self, replies: dict[str, list[str]] | None = None) -> None:
        self.prompts: list[tuple[str, str]] = []
        self._replies = replies or {}
        self._count: dict[str, int] = {}

    async def speak(self, agent: str, prompt: str) -> str:
        self.prompts.append((agent, prompt))
        n = self._count[agent] = self._count.get(agent, 0) + 1
        scripted = self._replies.get(agent)
        if scripted is not None:
            return scripted[min(n, len(scripted)) - 1]
        return f"{agent} point {n}"


async def test_three_agents_talk_for_several_rounds_in_one_thread() -> None:
    room = _Room()
    protocol = await run_discussion("pick a queue", AGENTS, room.speak, max_rounds=3, discussion_id="d1")

    assert [(t.round, t.speaker) for t in protocol.turns] == [(r, a) for r in (1, 2, 3) for a in AGENTS]
    assert protocol.rounds_run == 3
    last_prompt = room.prompts[-1][1]
    assert "carol point 3" not in last_prompt, "an agent's own reply of this round is not yet in the thread"
    assert "[round 1] alice: alice point 1" in last_prompt
    assert "[round 3] bob: bob point 3" in last_prompt, "later speakers see the earlier speakers of the same round"


async def test_rounds_stop_at_the_limit() -> None:
    room = _Room()
    protocol = await run_discussion("topic", AGENTS, room.speak, max_rounds=2)

    assert protocol.status == "round_limit"
    assert protocol.rounds_run == 2
    assert len(room.prompts) == 2 * len(AGENTS)


async def test_a_round_where_everyone_agrees_ends_the_discussion_early() -> None:
    room = _Room({a: ["open", f"done {CONSENSUS_MARKER}"] for a in AGENTS})
    protocol = await run_discussion("topic", AGENTS, room.speak, max_rounds=6)

    assert protocol.status == "consensus"
    assert protocol.rounds_run == 2


async def test_one_dissenter_keeps_the_discussion_going() -> None:
    room = _Room({"alice": [f"yes {CONSENSUS_MARKER}"], "bob": [f"yes {CONSENSUS_MARKER}"], "carol": ["not yet"]})
    protocol = await run_discussion("topic", AGENTS, room.speak, max_rounds=3)

    assert protocol.status == "round_limit"
    assert protocol.rounds_run == 3


async def test_a_failing_speaker_is_recorded_and_the_others_carry_on() -> None:
    async def speak(agent: str, prompt: str) -> str:
        if agent == "bob":
            raise RuntimeError("backend down")
        return f"{agent} ok"

    protocol = await run_discussion("topic", AGENTS, speak, max_rounds=2)

    failed = [t for t in protocol.turns if t.error]
    assert len(failed) == 2 and all(t.speaker == "bob" and "backend down" in t.text for t in failed)
    assert protocol.status == "round_limit"
    assert "(failed)" in protocol.render()


async def test_a_round_where_everyone_fails_aborts() -> None:
    async def speak(agent: str, prompt: str) -> str:
        raise RuntimeError("no model")

    protocol = await run_discussion("topic", AGENTS, speak, max_rounds=4)

    assert protocol.status == "failed"
    assert protocol.rounds_run == 1


@pytest.mark.parametrize(
    ("topic", "participants", "rounds"),
    [("", AGENTS, 3), ("t", ["alice"], 3), ("t", ["alice", "alice"], 3), ("t", AGENTS, 0), ("t", AGENTS, 11)],
)
async def test_bad_arguments_are_refused_before_anyone_speaks(topic, participants, rounds) -> None:
    room = _Room()
    with pytest.raises(ValueError):
        await run_discussion(topic, participants, room.speak, max_rounds=rounds)
    assert room.prompts == []


async def test_the_protocol_is_saved_and_read_back() -> None:
    room = _Room()
    protocol = await run_discussion("pick a queue", AGENTS, room.speak, max_rounds=2, discussion_id="d-2")
    with tempfile.TemporaryDirectory() as td:
        store = ProtocolStore(Path(td) / "discussions")
        path = store.save(protocol)

        assert path.name == "d-2.json"
        assert json.loads(path.read_text(encoding="utf-8"))["topic"] == "pick a queue"
        assert "## Round 2" in path.with_suffix(".md").read_text(encoding="utf-8")
        assert store.load("d-2") == protocol
        assert store.list_ids() == ["d-2"]


def test_a_discussion_id_cannot_leave_the_store() -> None:
    with tempfile.TemporaryDirectory() as td, pytest.raises(ValueError):
        ProtocolStore(Path(td)).load("../escape")


class _Manager:
    """The part of SubagentManager the tool touches."""

    def __init__(self, root: Path, room: _Room) -> None:
        self._root = root
        self._room = room
        self.chats: list[tuple[str, str, str]] = []

    def list_agents(self):
        return [SimpleNamespace(name=n) for n in AGENTS]

    async def create_instance(self, *, session_key: str, agent: str):
        return SimpleNamespace(handle=f"{agent}-h")

    async def chat(self, *, session_key: str, agent: str, handle: str, text: str, **_: object):
        self.chats.append((session_key, agent, handle))
        return await self._room.speak(agent, text), None

    def session_dir_for(self, session_key: str) -> Path:
        return self._root / session_key.replace(":", "_")


async def test_the_tool_runs_a_discussion_and_saves_the_protocol(tmp_path: Path) -> None:
    room = _Room()
    manager = _Manager(tmp_path, room)
    tool = DiscussTool(manager)  # type: ignore[arg-type]
    tool.set_context("cli", "direct", "cli:direct")

    out = await tool.execute(topic="pick a queue", agents=AGENTS, max_rounds=2)

    assert "## Round 2" in out and "ended by round_limit" in out
    saved = list((tmp_path / "cli_direct" / "discussions").glob("*.md"))
    assert len(saved) == 1 and str(saved[0]) in out
    assert {a for _, a, _ in manager.chats} == set(AGENTS)
    assert all(h == f"{a}-h" for _, a, h in manager.chats)


async def test_the_tool_reports_a_refusal_instead_of_raising(tmp_path: Path) -> None:
    tool = DiscussTool(_Manager(tmp_path, _Room()))  # type: ignore[arg-type]
    out = await tool.execute(topic="t", agents=["alice"], max_rounds=2)
    assert out.startswith("Error:")


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(self, messages, tools=None, model=None, **kwargs):
        return LLMResponse(content="stub", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


def test_an_ordinary_loop_holds_the_tool_and_a_subagent_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)
    with tempfile.TemporaryDirectory() as td:
        loop = AgentLoop(provider=_StubProvider(), workspace=Path(td), model="stub", **wire())
        assert loop.tools.has("discuss")
        monkeypatch.setenv("RAVEN_SUBAGENT", "1")
        delegated = AgentLoop(provider=_StubProvider(), workspace=Path(td), model="stub", **wire())
        assert not delegated.tools.has("discuss")
    assert "discuss" in WITHHELD_FROM_SUBAGENT

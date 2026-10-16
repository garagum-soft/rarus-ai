"""The compaction flow end to end: a summary that keeps the team, one that fails, and the restart at critical fill.

Run against the real Memory role and window code with a scripted provider: the
summary call, its failure modes and the restart are exercised, not only the
trigger arithmetic the older tests pin.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from raven.agent.harness.memory import DefaultMemory
from raven.agent.window import compaction
from raven.config.schema import CompactionConfig
from raven.contracts.harness import WindowPressure, WindowState
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest

TEAM = "[The team when the context was compacted -- current facts, not part of the summary]\n- Running now: coder (c1)"


class _Summarizer:
    """Answers the summary request with ``reply`` and ``finish``."""

    def __init__(self, reply: str = "BRIEF: read a.py, edited b.py, tests pass", finish: str = "stop") -> None:
        self.reply, self.finish, self.requests = reply, finish, []

    async def chat(self, messages, **kwargs):
        self.requests.append(messages)
        return LLMResponse(content=self.reply, finish_reason=self.finish)


class _Engine:
    owns_compaction = False


def _memory(provider: Any, cfg: CompactionConfig, team: str | None = TEAM, window: int = 8000) -> DefaultMemory:
    return DefaultMemory(
        _Engine(),
        provider=lambda: provider,
        model=lambda: "m",
        context_window_tokens=lambda: window,
        tool_definitions=lambda: [],
        system_prompt=lambda _skills: "",
        compaction=lambda: cfg,
        output_ceiling=lambda _model: 500,
        team_state=(lambda key: team if key == "telegram:42" else "") if team is not None else None,
    )


def _transcript(rounds: int = 8, body: str = "x " * 600) -> list[dict]:
    # The head outgrows the verbatim tail (2000 tokens in an 8000 window) and still
    # leaves the summary request room to be answered in.
    messages: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "refactor the parser"}]
    for i in range(rounds):
        name, args = (
            ("edit_file", {"path": f"src/mod{i}.py"}) if i % 3 == 0 else ("read_file", {"path": f"src/r{i}.py"})
        )
        messages.append(
            {
                "role": "assistant",
                "content": f"step {i}",
                "tool_calls": [
                    {"id": f"t{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                ],
            }
        )
        messages.append(
            {"role": "tool", "tool_call_id": f"t{i}", "content": ("Error: no such file" if i == 4 else body)}
        )
    return messages


async def test_a_summary_keeps_the_team_verbatim_beside_the_brief() -> None:
    provider = _Summarizer()
    memory = _memory(provider, CompactionConfig(enabled=True, prune=False))
    state = WindowState(image_window=2, last_context_used=7900, session_key="telegram:42")

    result = await memory.shrink(_transcript(), pressure=WindowPressure.PROACTIVE, state=state, model="m")

    assert result.changed and state.compress_retries == 1
    summary = result.messages[2]["content"]
    assert summary.startswith(compaction.SUMMARY_MARKER)
    assert "BRIEF: read a.py" in summary and summary.endswith(TEAM)
    assert result.messages[:2] == _transcript()[:2], "the system prefix and the task are never summarized"
    assert result.messages[3]["role"] != "tool", "the tail never opens on an orphaned tool result"
    # The team block was given to the agent, not to the summary model to restate.
    assert TEAM not in json.dumps(provider.requests)


async def test_a_cut_summary_is_thrown_away_and_counted_as_a_failure() -> None:
    memory = _memory(_Summarizer(finish="length"), CompactionConfig(enabled=True, prune=False))
    state = WindowState(image_window=2, last_context_used=7900, session_key="telegram:42")
    before = _transcript()

    result = await memory.shrink(before, pressure=WindowPressure.PROACTIVE, state=state, model="m")

    assert not result.changed and result.messages == before
    assert state.head_summary_failures == 1 and state.compress_retries == 1


async def test_at_critical_fill_the_working_context_restarts_from_a_mechanical_brief() -> None:
    memory = _memory(_Summarizer(reply="", finish="error"), CompactionConfig(enabled=True, restart_on_overflow=True))
    state = WindowState(image_window=2, session_key="telegram:42", compress_retries=2)
    messages = _transcript()

    first = await memory.shrink(messages, pressure=WindowPressure.OVERFLOW, state=state, model="m")
    again = await memory.shrink(first.messages, pressure=WindowPressure.OVERFLOW, state=state, model="m")

    assert first.changed and state.restarted and not again.changed, "once per turn"
    restart = first.messages[2]["content"]
    assert restart.startswith(compaction.RESTART_MARKER)
    assert "edit_file" in restart and "Files written or edited: src/mod0.py" in restart
    assert "Error: no such file" in restart and TEAM in restart
    assert first.messages[3]["role"] != "tool" and len(first.messages) < len(messages)


async def test_without_the_setting_overflow_with_nothing_left_fails_as_before() -> None:
    memory = _memory(_Summarizer(reply="", finish="error"), CompactionConfig(enabled=True))
    state = WindowState(image_window=2, session_key="telegram:42", compress_retries=2)

    result = await memory.shrink(_transcript(), pressure=WindowPressure.OVERFLOW, state=state, model="m")

    assert not result.changed and not state.restarted
    assert CompactionConfig().restart_on_overflow is False and CompactionConfig().enabled is False


def test_the_team_brief_reads_runs_instances_and_mail(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from raven.agent.subagent import instances
    from raven.agent.subagent.mailbox import Mailbox
    from raven.agent.subagent.team_state import team_brief

    registry = instances.InstanceRegistry(path=tmp_path / "instances.json")
    monkeypatch.setattr(instances, "_registry", registry)
    asyncio.run(registry.upsert_spawn("telegram:42", "coder", "c1", "running"))
    asyncio.run(registry.upsert_spawn("telegram:42", "reviewer", "r1", "completed"))
    box = Mailbox("telegram:42")
    box.join("coder", "coder", "c1")
    box.join("reviewer", "reviewer", "r1")
    box.send("coder", "reviewer", "please look at b.py")

    class _Manager:
        def live_handles(self, key):
            return {("coder", "c1")} if key == "telegram:42" else set()

    brief = team_brief("telegram:42", _Manager())

    assert "Running now: coder (c1)" in brief
    assert "reviewer/r1 [completed]" in brief and "coder/c1" not in brief.split("Instances")[1].split("\n")[0]
    assert "Agent mail unread: reviewer 1" in brief
    assert team_brief("telegram:other", _Manager()) == ""


class _OverflowUntilRestarted(LLMProvider):
    """Two tool rounds, then overflows every request until it sees a restarted context; summaries fail."""

    def __init__(self) -> None:
        super().__init__(api_key="k")
        self.saw_restart = False

    async def chat(self, messages, tools=None, model=None, **kwargs):
        if messages and messages[0].get("content") == compaction.SUMMARY_INSTRUCTIONS:
            return LLMResponse(content="summary endpoint unavailable", finish_reason="error")
        if any(compaction.RESTART_MARKER in str(m.get("content")) for m in messages):
            self.saw_restart = True
            return LLMResponse(content="done after the restart", finish_reason="stop")
        tools_seen = sum(1 for m in messages if m.get("role") == "tool")
        if tools_seen < 2:
            # Long reasoning each round, so the transcript outgrows the restart's tail.
            return LLMResponse(
                content=f"looking {tools_seen} " + "y " * 20_000,
                tool_calls=[ToolCallRequest(id=f"t{tools_seen}", name="no_such_tool", arguments={})],
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content="This model's maximum context length (8192 tokens) was exceeded", finish_reason="error"
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.parametrize("restart", [True, False])
async def test_a_turn_at_critical_fill_continues_only_with_the_restart_on(tmp_path: Path, restart: bool) -> None:
    from raven.agent.loop import AgentLoop
    from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import AnswerlessTurnError, Origin, TurnRequest

    provider = _OverflowUntilRestarted()
    agent = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=12),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(compaction_config=CompactionConfig(enabled=True, restart_on_overflow=restart)),
    )
    request = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text="go",
    )

    if restart:
        out = await agent._process_message(request, session_key="s1")
        assert out[0] == "done after the restart" and provider.saw_restart
    else:
        with pytest.raises(AnswerlessTurnError):
            await agent._process_message(request, session_key="s1")
        assert not provider.saw_restart

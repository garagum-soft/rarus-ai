"""Block explanations: a known refusal gets the step that lifts it, with the config value in force."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.block_hints import annotate, explain, explainer, setting
from raven.agent.loop._shared import _MAX_ITER_STATIC_FALLBACK
from raven.agent.subagent.teams import TeamTable
from raven.agent.tools.filesystem import resolve_path
from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import PermissionsConfig
from raven.contracts.tool import Tool
from raven.permissions import BuiltinRulings, PermissionGate

RAW = {
    "agents": {"defaults": {"maxSubagentSpawnsPerHour": 12, "maxToolIterations": 25}},
    "tools": {"restrictToWorkspace": True, "exec": {"extraDenyPatterns": ["curl .*\\| *sh"]}},
    "subagents": {"teams": [{"name": "docs", "maxSpawnsPerHour": 3}]},
}


def test_a_known_refusal_gets_a_hint_with_the_value_from_config() -> None:
    refusal = (
        "Spawn refused: this session hit its subagent spawn rate limit (12 per hour). "
        "It recovers automatically as earlier spawns age out."
    )

    hint = explain(refusal, RAW)

    assert hint is not None and hint.rule == "subagent_rate_limit"
    assert hint.settings == {"cap": 12}
    assert "agents.defaults.maxSubagentSpawnsPerHour = 12" in hint.text


def test_a_key_the_file_leaves_out_reads_as_the_schema_default() -> None:
    hint = explain(_MAX_ITER_STATIC_FALLBACK.format(n=40), {})

    assert hint is not None and hint.settings == {"cap": 40}, "the schema default for maxToolIterations"
    assert explain(_MAX_ITER_STATIC_FALLBACK.format(n=25), RAW).settings == {"cap": 25}


def test_the_team_limit_names_that_teams_own_cap() -> None:
    table = TeamTable.from_rows([{"name": "docs", "sessions": ["*"], "max_spawns_per_hour": 0}])
    refusal = f"Spawn refused: {table.charge('cli:1')}. No sub-agent was run."

    hint = explain(refusal, RAW)

    assert hint is not None and hint.rule == "team_dispatch_limit" and hint.settings == {"cap": 3}
    assert "subagents.teams[name=docs].maxSpawnsPerHour" in hint.text


def test_the_workspace_restriction_names_the_switch_and_the_workspace(tmp_path: Path) -> None:
    with pytest.raises(PermissionError) as refused:
        resolve_path(str(tmp_path.parent / "elsewhere.txt"), allowed_dirs=(tmp_path,))

    hint = explain(f"Error: {refused.value}", RAW)

    assert hint is not None and hint.rule == "workspace_restriction"
    assert "tools.restrictToWorkspace is true" in hint.text
    assert hint.settings["workspace"] == setting({}, "agents.defaults.workspace")


def test_the_deny_pattern_lists_the_operators_patterns() -> None:
    from raven.permissions.builtin import _DENY_REASONS

    hint = explain(f"Error: Command blocked by safety guard: it {_DENY_REASONS['deny_pattern']}", RAW)

    assert hint is not None and hint.settings == {"extra": ["curl .*\\| *sh"]}


def test_an_unknown_refusal_gets_no_hint() -> None:
    assert explain("Error: file not found", RAW) is None
    assert annotate("Error: file not found", RAW) == "Error: file not found"


def test_a_list_item_that_is_not_there_reads_as_unset() -> None:
    assert setting(RAW, "subagents.teams[name=ops].maxSpawnsPerHour") is None


def test_an_unreadable_config_gives_no_hint_rather_than_failing() -> None:
    def broken() -> dict:
        raise OSError("gone")

    assert explainer(broken)("Error: this session hit its subagent spawn rate limit") is None


class _Refuser(Tool):
    def __init__(self, text: str) -> None:
        self._text = text

    @property
    def name(self) -> str:
        return "refuser"

    @property
    def description(self) -> str:
        return "returns a refusal"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return self._text


async def test_a_refusal_from_a_tool_reaches_the_model_with_the_unblock_step() -> None:
    reg = ToolRegistry(block_explainer=explainer(lambda: RAW))
    reg.register(_Refuser("Spawn refused: this session hit its subagent spawn rate limit (12 per hour)."))

    result = await reg.execute("refuser", {})

    assert "How to unblock: a session may dispatch 12 sub-agents per hour" in str(result)


async def test_output_that_only_quotes_a_refusal_is_left_alone() -> None:
    reg = ToolRegistry(block_explainer=explainer(lambda: RAW))
    reg.register(_Refuser("notes.md: 'hit its subagent spawn rate limit' is what the old log said"))

    assert "How to unblock" not in str(await reg.execute("refuser", {}))


async def test_a_permission_deny_rule_names_the_rule_table() -> None:
    gate = PermissionGate(
        config_source=lambda: PermissionsConfig(mode="smart", tools={"refuser": "deny"}),
        builtin=BuiltinRulings(),
    )
    reg = ToolRegistry(
        permission_gate=gate, block_explainer=explainer(lambda: {"permissions": {"tools": {"refuser": "deny"}}})
    )
    reg.register(_Refuser("ran"))

    result = str(await reg.execute("refuser", {}))

    assert "deny rule" in result and "How to unblock: permissions.tools is {refuser: deny}" in result


async def test_a_real_turn_shows_the_model_how_to_unblock_a_denied_command(tmp_path: Path) -> None:
    """End to end: the loop's own registry, its live config and the exec deny pattern."""
    import json

    from raven.agent.loop import AgentLoop
    from raven.agent.tools.shell import ExecTool
    from raven.config.loader import set_config_path
    from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
    from raven.sandbox import ExecResult, SandboxExecutor
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    class Provider:
        def __init__(self) -> None:
            self.seen: list[list[dict]] = []
            self.responses = [
                LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="c1", name="exec", arguments={"command": "echo forbidden-word"})],
                    finish_reason="tool_calls",
                ),
                LLMResponse(content="Blocked, stopping.", finish_reason="stop"),
            ]

        async def chat_with_retry(self, **kwargs) -> LLMResponse:
            self.seen.append(list(kwargs.get("messages") or []))
            return self.responses.pop(0)

        def get_default_model(self) -> str:
            return "fake/model"

    class Executor(SandboxExecutor):
        commands: list[str] = []

        @property
        def is_sandboxed(self) -> bool:
            return False

        async def exec(self, command: str, **kwargs) -> ExecResult:
            self.commands.append(command)
            return ExecResult(stdout="ok", stderr="", exit_code=0)

    # Named outright: ``Path.home()`` follows USERPROFILE on Windows, not the
    # HOME the suite's fixture points at.
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"permissions": {"mode": "full"}, "tools": {"exec": {"extraDenyPatterns": ["forbidden-word"]}}})
    )
    set_config_path(config)
    try:
        provider, executor = Provider(), Executor()
        agent = AgentLoop(provider=provider, workspace=tmp_path, model="fake/model")
        agent.tools.register(ExecTool(executor=executor, working_dir=str(tmp_path)))
        request = TurnRequest(
            origin=Origin.USER,
            source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
            text="say the word",
            conversation="session-b",
        )

        result, _media = await agent._process_message(request, session_key="session-b")
    finally:
        set_config_path(None)  # type: ignore[arg-type]

    tool_results = [str(m.get("content")) for m in provider.seen[-1] if m.get("role") == "tool"]
    assert result == "Blocked, stopping." and executor.commands == []
    assert any("How to unblock:" in t and "extraDenyPatterns is [forbidden-word]" in t for t in tool_results), (
        tool_results
    )


async def test_without_an_explainer_the_registry_returns_the_refusal_untouched() -> None:
    text = "Spawn refused: this session hit its subagent spawn rate limit (12 per hour)."
    reg = ToolRegistry()
    reg.register(_Refuser(text))

    assert str(await reg.execute("refuser", {})) == text

"""Claude through the user's Claude subscription: one model step per call, Raven runs the tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import AssistantMessage, DeferredToolUse, ResultMessage, TextBlock, ToolUseBlock  # noqa: E402

from raven.providers.claude_subscription_provider import (  # noqa: E402
    ClaudeSubscriptionProvider,
    credentials_present,
    render,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    }
]


def _result(**kw: Any) -> ResultMessage:
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s")
    return ResultMessage(**{**base, **kw})


class Runner:
    """Stands in for ``claude_agent_sdk.query``: records each call, answers from a script."""

    def __init__(self, *scripts: list[Any]) -> None:
        self.scripts = list(scripts)
        self.calls: list[tuple[str, Any]] = []

    async def __call__(self, *, prompt: Any, options: Any):
        if not isinstance(prompt, str):
            prompt = [message async for message in prompt]
        self.calls.append((prompt, options))
        for message in self.scripts.pop(0):
            yield message


def _provider(runner: Runner, tmp_path: Path) -> ClaudeSubscriptionProvider:
    return ClaudeSubscriptionProvider(runner=runner, cwd=tmp_path)


def test_the_system_messages_and_the_reading_note_become_the_system_prompt() -> None:
    from raven.providers.claude_subscription_provider import TRANSCRIPT_NOTE

    system, prompt = render([{"role": "system", "content": "You are Raven."}, {"role": "user", "content": "hi"}])
    assert system == f"You are Raven.\n\n{TRANSCRIPT_NOTE}"
    assert prompt == "[user]\n\nhi", "a first step has the same shape as every later one"


def _sent_blocks(prompt: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assert len(prompt) == 1 and prompt[0]["type"] == "user"
    return prompt[0]["message"]["content"]


async def test_each_step_sends_the_previous_steps_blocks_unchanged_first(tmp_path: Path) -> None:
    """The prompt cache matches a prefix at block boundaries: history must only ever grow at the end."""
    runner = Runner(
        [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="mcp__raven__read_file", input={"path": "a"})], model="c"
            ),
            _result(),
        ],
        [AssistantMessage(content=[TextBlock("done")], model="c"), _result()],
    )
    provider = _provider(runner, tmp_path)
    first = [{"role": "system", "content": "sys"}, {"role": "user", "content": "read a"}]
    await provider.chat(first, tools=TOOLS)
    second = [
        *first,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "function": {"name": "read_file", "arguments": '{"path": "a"}'}}],
        },
        {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "hello"},
    ]
    await provider.chat(second, tools=TOOLS)

    step1, step2 = (_sent_blocks(prompt) for prompt, _ in runner.calls)
    assert step2[: len(step1)] == step1 and len(step2) == len(step1) + 2
    assert runner.calls[0][1].system_prompt == runner.calls[1][1].system_prompt


def test_a_conversation_is_written_out_with_its_tool_calls_and_results() -> None:
    system, prompt = render(
        [
            {"role": "user", "content": [{"type": "text", "text": "read a"}, {"type": "image_url", "image_url": {}}]},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "t1", "function": {"name": "read_file", "arguments": '{"path": "a"}'}}],
            },
            {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "hello"},
        ]
    )
    assert "[user]\n\nread a\n\n[image omitted: no data]" in prompt
    assert '[assistant called tool read_file (call t1)]\n{"path": "a"}' in prompt
    assert "[result of tool read_file (call t1)]\n\nhello" in prompt
    assert prompt.endswith("hello"), "nothing after the last message: it would move every step"
    assert "Do not repeat a tool call" in system


PNG = "iVBORw0KGgo="


async def test_an_image_from_the_user_reaches_claude_as_an_image(tmp_path: Path) -> None:
    runner = Runner([AssistantMessage(content=[TextBlock("A cat.")], model="claude"), _result()])

    answer = await _provider(runner, tmp_path).chat(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
                ],
            }
        ]
    )

    sent = runner.calls[0][0]
    assert answer.content == "A cat." and isinstance(sent, list) and len(sent) == 1
    assert sent[0]["type"] == "user" and sent[0]["message"]["role"] == "user"
    assert sent[0]["message"]["content"] == [
        {"type": "text", "text": "[user]\n\nwhat is this?"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}},
    ]


async def test_an_image_a_tool_returned_sits_under_its_result_in_the_transcript(tmp_path: Path) -> None:
    runner = Runner([AssistantMessage(content=[TextBlock("ok")], model="claude"), _result()])
    await _provider(runner, tmp_path).chat(
        [
            {"role": "user", "content": "take a screenshot"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "t1", "function": {"name": "shot", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "t1",
                "name": "shot",
                "content": [
                    {"type": "text", "text": "saved"},
                    {"type": "image", "source": {"type": "url", "url": "https://example.com/s.png"}},
                ],
            },
        ]
    )

    blocks = _sent_blocks(runner.calls[0][0])
    assert [b.get("text") for b in blocks[:3]] == [
        "[user]\n\ntake a screenshot",
        "[assistant called tool shot (call t1)]\n{}",
        "[result of tool shot (call t1)]\n\nsaved",
    ]
    assert blocks[3] == {"type": "image", "source": {"type": "url", "url": "https://example.com/s.png"}}
    assert len(blocks) == 4


def test_an_image_claude_cannot_read_is_named_rather_than_dropped() -> None:
    from raven.providers.claude_subscription_provider import render_parts

    _, parts = render_parts(
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/bmp;base64,Qk0="}},
                    {"type": "image_url", "image_url": "https://example.com/a.jpg"},
                ],
            }
        ]
    )

    assert parts[0] == "[user]\n\n[image omitted: image/bmp is not a format Claude reads]"
    assert parts[1] == {"type": "image", "source": {"type": "url", "url": "https://example.com/a.jpg"}}


def test_the_provider_is_marked_as_seeing_images() -> None:
    from raven.providers.registry import find_by_name

    spec = find_by_name("claude_subscription")
    assert spec.vision_override is True and spec.image_tool_result_override is True


async def test_a_tool_call_is_handed_back_to_raven_not_run_by_the_cli(tmp_path: Path) -> None:
    runner = Runner(
        [
            AssistantMessage(
                content=[
                    TextBlock("Let me look."),
                    ToolUseBlock(id="t1", name="mcp__raven__read_file", input={"path": "a"}),
                ],
                model="claude",
            ),
            _result(
                deferred_tool_use=DeferredToolUse(id="t1", name="mcp__raven__read_file", input={"path": "a"}),
                usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2},
            ),
        ]
    )

    answer = await _provider(runner, tmp_path).chat(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "read a"}],
        tools=TOOLS,
        model="claude-subscription/opus",
    )

    assert answer.finish_reason == "tool_calls" and answer.content == "Let me look."
    assert [(c.id, c.name, c.arguments) for c in answer.tool_calls] == [("t1", "read_file", {"path": "a"})]
    assert answer.usage["prompt_tokens"] == 10 and answer.usage["prompt_tokens_include_cache"] is False
    _, options = runner.calls[0]
    assert options.tools == [] and options.setting_sources == [] and options.max_turns == 1
    assert options.allowed_tools == ["mcp__raven__read_file"] and "raven" in options.mcp_servers
    assert options.model == "opus" and options.system_prompt.startswith("sys\n\n")
    hook = options.hooks["PreToolUse"][0].hooks[0]
    decided = await hook({"tool_name": "mcp__raven__read_file"}, "t1", None)
    assert decided["hookSpecificOutput"]["permissionDecision"] == "defer"


async def test_a_plain_answer_needs_no_tools_and_no_hook(tmp_path: Path) -> None:
    runner = Runner([AssistantMessage(content=[TextBlock("Hello!")], model="claude"), _result(result="Hello!")])

    answer = await _provider(runner, tmp_path).chat([{"role": "user", "content": "hi"}])

    assert (answer.content, answer.finish_reason, answer.tool_calls) == ("Hello!", "stop", [])
    _, options = runner.calls[0]
    assert options.mcp_servers == {} and options.hooks is None and options.model == "sonnet"


async def test_a_missing_login_says_how_to_sign_in(tmp_path: Path) -> None:
    runner = Runner(
        [AssistantMessage(content=[TextBlock("Not logged in")], model="claude", error="authentication_failed")]
    )

    answer = await _provider(runner, tmp_path).chat([{"role": "user", "content": "hi"}])

    assert answer.finish_reason == "error"
    assert "raven provider login claude_subscription" in answer.content
    assert answer.error_classification is not None and answer.error_classification.category == "auth"


async def test_an_errored_run_is_an_error_turn(tmp_path: Path) -> None:
    runner = Runner([_result(is_error=True, subtype="error_during_execution", errors=["overloaded"])])

    answer = await _provider(runner, tmp_path).chat([{"role": "user", "content": "hi"}])

    assert answer.finish_reason == "error" and "overloaded" in answer.content


def test_the_factory_builds_it_from_config_and_the_wire_id_drops_the_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.config.schema import Config
    from raven.providers.factory import make_provider

    # Signed in: without a login the factory refuses, naming `raven provider login`.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    config = Config.model_validate(
        {"agents": {"defaults": {"model": "claude-subscription/opus", "provider": "claude_subscription"}}}
    )
    provider = make_provider(config)

    assert isinstance(provider, ClaudeSubscriptionProvider)
    assert provider.wire_model_id("claude-subscription/opus") == "opus"
    assert provider.wire_model_id("claude_subscription/claude-opus-5-5") == "claude-opus-5-5"


def test_without_a_login_the_factory_names_the_sign_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.config.schema import Config
    from raven.providers.auth import MissingCredentialsError
    from raven.providers.factory import make_provider

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")
    config = Config.model_validate(
        {"agents": {"defaults": {"model": "claude-subscription/sonnet", "provider": "claude_subscription"}}}
    )

    with pytest.raises(MissingCredentialsError, match="raven provider login claude-subscription"):
        make_provider(config)


def test_a_login_is_the_token_in_the_environment_or_the_clis_credentials_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")
    assert credentials_present() is False
    (tmp_path / ".credentials.json").write_text("{}", encoding="utf-8")
    assert credentials_present() is True
    (tmp_path / ".credentials.json").unlink()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    assert credentials_present() is True


async def test_the_page_sign_in_hands_over_the_link_the_cli_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import sys

    from raven.providers import claude_subscription_provider as claude
    from raven.providers import oauth_login

    script = tmp_path / "fake_claude.py"
    script.write_text("print('Open https://claude.ai/oauth/authorize?code=1 to sign in')\n", encoding="utf-8")
    if os.name == "nt":
        cli = tmp_path / "claude.cmd"
        cli.write_text(f'@"{sys.executable}" "{script}"\r\n', encoding="utf-8")
    else:
        cli = tmp_path / "claude"
        cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n', encoding="utf-8")
        cli.chmod(0o755)
    monkeypatch.setattr(claude, "bundled_cli", lambda: cli)
    monkeypatch.setattr(claude, "login_status", lambda: True)
    monkeypatch.setattr("raven.config.paths.restrict_to_owner", lambda *paths: None)

    pair = await oauth_login.start("claude_subscription")
    await oauth_login._PENDING.get("claude_subscription", asyncio.sleep(0))

    assert pair["verification_uri"] == "https://claude.ai/oauth/authorize?code=1" and pair["user_code"] == ""


def test_the_connection_check_runs_one_short_turn(tmp_path: Path) -> None:
    from raven.config.update_providers import _probe_claude_subscription

    ok = _probe_claude_subscription(
        timeout_s=30,
        provider=_provider(Runner([AssistantMessage(content=[TextBlock("OK")], model="claude"), _result()]), tmp_path),
    )
    refused = _probe_claude_subscription(
        timeout_s=30,
        provider=_provider(
            Runner([AssistantMessage(content=[TextBlock("x")], model="claude", error="authentication_failed")]),
            tmp_path,
        ),
    )

    assert ok["ok"] is True and ok["model_ids"] == ["sonnet", "opus", "haiku"]
    assert refused["ok"] is False and refused["status"] == "oauth_token_missing"


async def test_a_real_turn_runs_the_tool_claude_asked_for_and_answers_with_its_result(tmp_path: Path) -> None:
    """End to end through Raven's own loop: Claude asks, Raven runs the tool, Claude answers."""
    from raven.agent.loop import AgentLoop
    from raven.config.loader import set_config_path
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    (tmp_path / "note.txt").write_text("the answer is 42", encoding="utf-8")
    runner = Runner(
        [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="mcp__raven__read_file", input={"path": "note.txt"})],
                model="claude",
            ),
            _result(
                deferred_tool_use=DeferredToolUse(id="t1", name="mcp__raven__read_file", input={"path": "note.txt"})
            ),
        ],
        [AssistantMessage(content=[TextBlock("It says 42.")], model="claude"), _result()],
    )
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"permissions": {"mode": "full"}}), encoding="utf-8")
    set_config_path(config)
    try:
        agent = AgentLoop(
            provider=_provider(runner, tmp_path / "sdk"), workspace=tmp_path, model="claude-subscription/sonnet"
        )
        request = TurnRequest(
            origin=Origin.USER,
            source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
            text="what does note.txt say?",
            conversation="session-c",
        )
        result, _media = await agent._process_message(request, session_key="session-c")
    finally:
        set_config_path(None)  # type: ignore[arg-type]

    assert result == "It says 42."
    first_options = runner.calls[0][1]
    assert "mcp__raven__read_file" in first_options.allowed_tools
    second_prompt = "\n".join(b.get("text", "") for b in _sent_blocks(runner.calls[1][0]))
    assert "[result of tool read_file (call t1)]" in second_prompt and "the answer is 42" in second_prompt

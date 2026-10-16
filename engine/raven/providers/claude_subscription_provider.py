"""Claude through the user's own Claude subscription, by way of the Claude Agent SDK.

There is no API key on this path. The Agent SDK runs Claude Code's own CLI (the
SDK bundles it), and the CLI signs in with the login the user made on this
machine (``raven provider login claude_subscription`` runs that login, or
``CLAUDE_CODE_OAUTH_TOKEN`` carries one).

Raven's loop owns its tools and runs them itself, while the CLI is an agent
that would run tools of its own. So each call is one model step and nothing
more:

- the CLI's built-in tools are switched off (``tools=[]``) and the user's
  Claude settings and memory files are not loaded (``setting_sources=[]``);
- Raven's tool schemas are offered as the tools of an in-process MCP server;
- a ``PreToolUse`` hook answers ``defer`` for every call, which stops the run
  before the tool executes and hands the call back -- the SDK's own mechanism
  for a caller that executes tools itself;
- the calls the model made are returned as ``tool_calls``, Raven runs them, and
  the next call sends the whole conversation again.

The SDK takes user messages, not a conversation with assistant turns, so the
conversation is written out as a transcript: the system messages become the
system prompt, and the turns, tool calls and tool results follow in order as
the blocks of one user message, images in place. One block run per message,
appended to and never rewritten, so the prompt cache serves the history a
previous step already sent (see ``render_blocks``). A new process per step does
not cost the cache: it lives on Anthropic's side, keyed by the request's prefix.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest, format_llm_error
from raven.providers.usage import token_count

PROVIDER = "claude_subscription"
SERVER = "raven"
TOOL_PREFIX = f"mcp__{SERVER}__"
DEFAULT_MODEL = "sonnet"
LOGIN_HINT = "run `raven provider login claude_subscription` (it opens the Claude sign-in in your browser)"

Runner = Callable[..., AsyncIterator[Any]]


class AuthenticationError(RuntimeError):
    """Named so ``LLMProvider.classify_error`` reads it as the ``auth`` category."""


class ClaudeLoginError(AuthenticationError):
    """The CLI has no Claude login to run on, or Claude refused it."""


def bundled_cli() -> Path | None:
    """The Claude Code CLI the Agent SDK ships with, if the SDK is installed."""
    try:
        import claude_agent_sdk
    except ImportError:
        return None
    name = "claude.exe" if os.name == "nt" else "claude"
    path = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return path if path.is_file() else None


_STATUS_TTL_S = 60.0
_status_cache: tuple[float, bool] | None = None


def login_status() -> bool:
    """What the CLI itself says (``claude auth status``): the authority, but a process start."""
    import subprocess
    import time

    global _status_cache
    now = time.monotonic()
    if _status_cache is not None and now - _status_cache[0] < _STATUS_TTL_S:
        return _status_cache[1]
    cli = bundled_cli()
    logged_in = False
    if cli is not None:
        try:
            done = subprocess.run(  # noqa: S603 - the SDK's own bundled binary, fixed arguments
                [str(cli), "auth", "status", "--json"], capture_output=True, text=True, timeout=20
            )
            logged_in = bool(json.loads(done.stdout or "{}").get("loggedIn"))
        except (OSError, ValueError, subprocess.SubprocessError):
            logged_in = False
    _status_cache = (now, logged_in)
    return logged_in


def credentials_present() -> bool:
    """Whether this machine has a Claude login the CLI can use.

    A token in the environment, or the credentials file the CLI writes on login.
    On macOS the CLI keeps its login in the keychain instead, so there the CLI is
    asked (and the answer kept for a minute: provider lists ask often).
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    if (home / ".credentials.json").is_file():
        return True
    import sys

    return sys.platform == "darwin" and login_status()


#: What Claude accepts as an image.
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

Part = str | dict[str, Any]


def _image(block: dict[str, Any]) -> dict[str, Any] | str:
    """An image block in Anthropic's shape, or a note saying why it could not be one.

    Raven carries images the OpenAI way (``image_url`` with a data URL or a link)
    and, from some tools, already in Anthropic's (``image`` with a ``source``).
    """
    if block.get("type") == "image" and isinstance(block.get("source"), dict):
        return {"type": "image", "source": block["source"]}
    ref = block.get("image_url")
    url = ref.get("url") if isinstance(ref, dict) else ref
    if not isinstance(url, str) or not url:
        return "[image omitted: no data]"
    if url.startswith("data:"):
        head, _, data = url.partition(",")
        media = head[5:].split(";", 1)[0].lower()
        if ";base64" not in head or media not in _IMAGE_TYPES:
            return f"[image omitted: {media or 'unknown type'} is not a format Claude reads]"
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return "[image omitted: not a data URL or a link]"


def _parts(content: Any) -> list[Part]:
    """A message's content as text and images, in order."""
    if content is None:
        return []
    if isinstance(content, str):
        return [content] if content.strip() else []
    parts: list[Part] = []
    for block in content if isinstance(content, list) else [content]:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif block.get("type") in {"image_url", "image", "input_image"}:
                parts.append(_image(block))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        else:
            parts.append(str(block))
    return [p for p in parts if p]


def _text(content: Any) -> str:
    return "\n".join(p if isinstance(p, str) else "[image]" for p in _parts(content))


#: How to read the transcript. In the system prompt, which does not move between
#: steps, rather than after the transcript, where it would sit in a different
#: place every step and cost the cached prefix behind it.
TRANSCRIPT_NOTE = (
    "The user's message is the conversation so far, oldest first, one block per message: "
    "[user], [assistant], the tool calls the assistant made and their results. You are the assistant. "
    "Continue from the end: answer the last message, or call a tool if you need one. "
    "Do not repeat a tool call whose result is already there."
)


def _blocks(parts: list[Part]) -> list[dict[str, Any]]:
    """One message's parts as content blocks: its text runs joined, its images between them."""
    blocks: list[dict[str, Any]] = []
    run: list[str] = []
    for part in parts:
        if isinstance(part, str):
            run.append(part)
            continue
        if run:
            blocks.append({"type": "text", "text": "\n\n".join(run)})
            run = []
        blocks.append(part)
    if run:
        blocks.append({"type": "text", "text": "\n\n".join(run)})
    return blocks


def _message_parts(m: dict[str, Any]) -> list[Part]:
    role = m.get("role")
    if role == "user":
        return ["[user]", *_parts(m.get("content"))]
    if role == "assistant":
        out: list[Part] = []
        said = _parts(m.get("content"))
        if said:
            out += ["[assistant]", *said]
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            out.append(
                f"[assistant called tool {fn.get('name')} (call {call.get('id')})]\n{fn.get('arguments') or '{}'}"
            )
        return out
    if role == "tool":
        return [f"[result of tool {m.get('name') or ''} (call {m.get('tool_call_id')})]", *_parts(m.get("content"))]
    return []


def render_blocks(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """(system prompt, content blocks): the conversation written out for one SDK call.

    Built for the prompt cache. The cache matches a request's prefix at block
    boundaries, so each message is blocks of its own -- never merged with the
    next -- and the next step's blocks are this step's with new ones after them:
    the history a step already sent is read from the cache rather than again.
    Images stay in place as image blocks.
    """
    raven = "\n\n".join(_text(m.get("content")) for m in messages if m.get("role") == "system").strip()
    system = f"{raven}\n\n{TRANSCRIPT_NOTE}" if raven else TRANSCRIPT_NOTE
    blocks: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") != "system":
            blocks += _blocks(_message_parts(m))
    return system, blocks


def render_parts(messages: list[dict[str, Any]]) -> tuple[str, list[Part]]:
    """(system prompt, parts): the same transcript as text and images, one part per block."""
    system, blocks = render_blocks(messages)
    return system, [b["text"] if b.get("type") == "text" else b for b in blocks]


def render(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """(system prompt, prompt) as text only, images marked where they were."""
    system, parts = render_parts(messages)
    return system, "\n\n".join(p if isinstance(p, str) else "[image]" for p in parts)


def prompt_for(blocks: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """One user message of content blocks, the shape the SDK's streaming input takes."""
    message = {
        "type": "user",
        "message": {"role": "user", "content": blocks},
        "parent_tool_use_id": None,
        "session_id": "default",
    }

    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield message

    return stream()


def _usage(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not raw:
        return {}
    prompt = token_count(raw.get("input_tokens"))
    output = token_count(raw.get("output_tokens"))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "cache_read_input_tokens": token_count(raw.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": token_count(raw.get("cache_creation_input_tokens")),
        # Anthropic counts cache apart from input. No cost: the subscription is
        # billed by plan, and the CLI's figure is what the API would have charged.
        "prompt_tokens_include_cache": False,
    }


async def _sdk_query(*, prompt: str | AsyncIterator[dict[str, Any]], options: Any) -> AsyncIterator[Any]:
    from claude_agent_sdk import query

    async for message in query(prompt=prompt, options=options):
        yield message


class ClaudeSubscriptionProvider(LLMProvider):
    """One model step per call, on the user's Claude subscription."""

    def __init__(self, default_model: str | None = None, *, runner: Runner | None = None, cwd: Path | None = None):
        super().__init__(api_key=None, api_base=None)
        self.default_model = default_model or f"claude-subscription/{DEFAULT_MODEL}"
        self._run = runner or _sdk_query
        self._cwd = cwd

    def wire_model_id(self, model: str) -> str:
        """The id the CLI is asked for (an alias or a model id). See ``providers.wire``."""
        from raven.providers.registry import find_by_name
        from raven.providers.wire import wire_model

        return wire_model(model, spec=find_by_name(PROVIDER))

    def get_default_model(self) -> str:
        return self.default_model

    def _options(self, system: str, tools: list[dict[str, Any]] | None, model: str, effort: str | None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, SdkMcpTool, create_sdk_mcp_server

        async def never_runs(args: dict[str, Any]) -> dict[str, Any]:
            # The defer hook stops the run before this; reaching it means the
            # hook did not, and the model is told to hand the call back instead.
            return {"content": [{"type": "text", "text": "This tool runs in Raven, not here."}], "is_error": True}

        async def defer(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "defer"}}

        sdk_tools = []
        for schema in tools or []:
            fn = schema.get("function") or schema
            sdk_tools.append(
                SdkMcpTool(
                    name=fn["name"],
                    description=fn.get("description") or "",
                    input_schema=fn.get("parameters") or {"type": "object", "properties": {}},
                    handler=never_runs,
                )
            )
        cwd = self._cwd
        if cwd is None:
            from raven.home import raven_home

            cwd = raven_home() / "claude-sdk"
        cwd.mkdir(parents=True, exist_ok=True)
        return ClaudeAgentOptions(
            system_prompt=system or None,
            tools=[],
            mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=sdk_tools)} if sdk_tools else {},
            allowed_tools=[f"{TOOL_PREFIX}{t.name}" for t in sdk_tools],
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[defer])]} if sdk_tools else None,
            setting_sources=[],
            max_turns=1,
            model=self.wire_model_id(model),
            effort=effort if effort in {"low", "medium", "high", "xhigh", "max"} else None,
            cwd=str(cwd),
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        model = model or self.default_model
        if tool_choice == "none":
            tools = None
        system, blocks = render_blocks(messages)
        prompt = prompt_for(blocks)
        texts: list[str] = []
        calls: dict[str, ToolCallRequest] = {}
        raw_usage: dict[str, Any] | None = None
        try:
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

            options = self._options(system, tools, model, reasoning_effort)
            async for message in self._run(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    error = getattr(message, "error", None)
                    if error in {"authentication_failed", "billing_error"}:
                        raise ClaudeLoginError(
                            f"authentication failed: Claude refused the login ({error}); {LOGIN_HINT}"
                        )
                    if error:
                        said = " ".join(b.text for b in message.content if isinstance(b, TextBlock))
                        raise RuntimeError(f"Claude API error ({error}): {said}".strip())
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text:
                            texts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            calls[block.id] = ToolCallRequest(
                                id=block.id,
                                name=block.name.removeprefix(TOOL_PREFIX),
                                arguments=dict(block.input or {}),
                            )
                elif isinstance(message, ResultMessage):
                    raw_usage = message.usage
                    deferred = message.deferred_tool_use
                    if deferred is not None and deferred.id not in calls:
                        calls[deferred.id] = ToolCallRequest(
                            id=deferred.id,
                            name=deferred.name.removeprefix(TOOL_PREFIX),
                            arguments=dict(deferred.input or {}),
                        )
                    if message.is_error and deferred is None and not calls:
                        detail = "; ".join(message.errors or []) or (message.result or message.subtype)
                        if "login" in detail.lower() or "auth" in detail.lower():
                            raise ClaudeLoginError(f"authentication failed: {detail}; {LOGIN_HINT}")
                        raise RuntimeError(f"Claude ended the turn with an error: {detail}")
        except ImportError:
            missing: Exception = RuntimeError(
                "the Claude Agent SDK is not installed; install raven with the `claude` extra (claude-agent-sdk)"
            )
            return self._error(missing, raw_usage)
        except Exception as exc:
            if type(exc).__name__ == "CLINotFoundError":
                exc = RuntimeError(f"the Claude Code CLI was not found ({exc})")
            return self._error(exc, raw_usage)
        return LLMResponse(
            content="\n".join(texts) or None,
            tool_calls=list(calls.values()),
            finish_reason="tool_calls" if calls else "stop",
            usage=_usage(raw_usage),
        )

    def _error(self, exc: Exception, raw_usage: dict[str, Any] | None) -> LLMResponse:
        classification = self.classify_error(exc)
        return LLMResponse(
            content=format_llm_error(exc, classification, provider=PROVIDER),
            finish_reason="error",
            usage=_usage(raw_usage),
            error_classification=classification,
        )


__all__ = [
    "ClaudeLoginError",
    "ClaudeSubscriptionProvider",
    "bundled_cli",
    "credentials_present",
    "prompt_for",
    "render",
    "render_blocks",
    "render_parts",
]

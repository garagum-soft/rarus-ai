"""The built-in Rarus provider: OpenAI-style chat through the alembox.com agent API.

The wire is plain chat completions, so LiteLLM does the streaming and tool
calls. What is Rarus's own is the credential: the agent key a browser sign-in
left on this computer is read per call, never copied into ``config.json``, so a
sign-out or a revoked key takes effect on the next turn.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

from raven.providers.auth import MissingCredentialsError
from raven.providers.base import ChatDelta, LLMProvider, LLMResponse
from raven.providers.litellm_provider import LiteLLMProvider
from raven.providers.rarus_session import API_PATH, DEFAULT_SITE, SITE_ENV, default_api_base, load_key

PROVIDER_NAME = "rarus"


def _api_base(passed: str | None) -> str:
    """The address chat goes to.

    The registry hands the production address over as the default, so a ``RARUS_SITE`` override (a
    staging server, a local stand-in) would reach sign-in and balance but not chat. It wins over that
    default; an address the reader configured themselves is never replaced.
    """
    production = DEFAULT_SITE + API_PATH
    if os.environ.get(SITE_ENV, "").strip() and (not passed or passed.rstrip("/") == production):
        return default_api_base()
    return passed or default_api_base()


class RarusProvider(LiteLLMProvider):
    def __init__(
        self,
        default_model: str,
        api_base: str | None = None,
        model_overrides: dict[str, dict[str, Any]] | None = None,
    ):
        super().__init__(
            api_base=_api_base(api_base),
            default_model=default_model,
            provider_name=PROVIDER_NAME,
            model_overrides=model_overrides,
        )

    async def _prepare_key(self) -> None:
        key = await asyncio.to_thread(load_key)
        if not key:
            raise MissingCredentialsError(
                "Rarus AI needs a sign-in",
                provider=PROVIDER_NAME,
                remedy="Run `rarus provider login rarus`, or press Sign in in the app.",
            )
        self.api_key = key

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
        await self._prepare_key()
        return await super().chat(messages, tools, model, max_tokens, temperature, reasoning_effort, tool_choice)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        # Sentinels, forwarded unresolved: the agent loop calls chat_stream with
        # messages/tools/model only, so a literal here would become the value that
        # is sent and the parent's fallback to self.generation would never run.
        max_tokens: object = LLMProvider._SENTINEL,
        temperature: object = LLMProvider._SENTINEL,
        reasoning_effort: object = LLMProvider._SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatDelta]:
        await self._prepare_key()
        async for delta in super().chat_stream(
            messages, tools, model, max_tokens, temperature, reasoning_effort, tool_choice
        ):
            yield delta

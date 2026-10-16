"""The ``discuss`` tool: several roster agents talk one topic through in rounds.

Each participant gets its own instance of its agent for the discussion and is
asked, round by round, for its next contribution with the thread so far in front
of it. The protocol is saved under the session's sub-agent records and the tool
returns it to the caller.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from raven.agent.subagent.discussion import (
    DEFAULT_MAX_ROUNDS,
    MAX_ROUNDS_CEILING,
    MIN_PARTICIPANTS,
    ProtocolStore,
    run_discussion,
)
from raven.contracts.tool import Tool

if TYPE_CHECKING:
    from raven.agent.subagent import SubagentManager

_STORE_DIR = "discussions"


@dataclass(frozen=True)
class _Origin:
    session_key: str


class DiscussTool(Tool):
    """Run a multi-round discussion between roster agents and save its protocol."""

    timeout_seconds = 1800.0
    blocking_interaction = True

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._default = _Origin(session_key="cli:direct")
        self._origin: ContextVar[_Origin] = ContextVar("discuss_origin")

    def _cur(self) -> _Origin:
        return self._origin.get(None) or self._default

    def set_context(self, channel: str, chat_id: str, session_key: str) -> None:
        self._origin.set(replace(self._cur(), session_key=session_key))

    @property
    def name(self) -> str:
        return "discuss"

    @property
    def description(self) -> str:
        return (
            "Group discussion between several roster agents in rounds, each seeing the whole thread, "
            "until the round limit or until all agree. Returns the saved protocol."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        names = sorted(a.name for a in self._manager.list_agents())
        agents: dict[str, Any] = {
            "type": "array",
            "minItems": MIN_PARTICIPANTS,
            "uniqueItems": True,
            "items": {"type": "string", **({"enum": names} if names else {})},
            "description": "Roster names, in speaking order.",
        }
        return {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "minLength": 1, "description": "The subject."},
                "agents": agents,
                "max_rounds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_ROUNDS_CEILING,
                    "description": f"Default {DEFAULT_MAX_ROUNDS}.",
                },
            },
            "required": ["topic", "agents"],
        }

    async def execute(self, **kwargs: Any) -> str:
        topic = str(kwargs.get("topic") or "")
        agents = [str(a) for a in kwargs.get("agents") or []]
        max_rounds = int(kwargs.get("max_rounds") or DEFAULT_MAX_ROUNDS)
        session_key = self._cur().session_key
        handles: dict[str, str] = {}
        try:
            for agent in dict.fromkeys(agents):
                created = await self._manager.create_instance(session_key=session_key, agent=agent)
                handles[agent] = created.handle
        except Exception as exc:
            return f"Error: could not start the discussion: {exc}"

        async def speak(agent: str, prompt: str) -> str:
            reply, _meta = await self._manager.chat(
                session_key=session_key, agent=agent, handle=handles[agent], text=prompt
            )
            return reply

        try:
            protocol = await run_discussion(topic, agents, speak, max_rounds=max_rounds)
        except ValueError as exc:
            return f"Error: {exc}"
        store = ProtocolStore(self._manager.session_dir_for(session_key) / _STORE_DIR)
        path = store.save(protocol)
        return f"{protocol.render()}\nProtocol saved: {path.with_suffix('.md')}"


__all__ = ["DiscussTool"]

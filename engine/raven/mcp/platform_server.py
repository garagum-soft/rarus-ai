"""Raven's own MCP server: the platform's tasks, sessions, agents and channels as tools.

An MCP client -- another agent, an IDE -- connects over stdio (``raven mcp
serve``) and drives the Raven that is running on this machine: start a task in a
conversation, list a conversation's tasks, list conversations, the agent roster
and the channels. Every tool is one call on the gateway's own JSON-RPC
(``/rpc``), so the server holds no state of its own and can do nothing the page
cannot; it finds the gateway and its credential in ``serve.json``, the file
``raven web`` / ``raven serve`` writes for local tooling.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from raven.home import raven_home

Call = Callable[[str, dict[str, Any]], Awaitable[Any]]


class GatewayUnavailableError(RuntimeError):
    """No running gateway to talk to."""


class GatewayClient:
    """One JSON-RPC call per request against the local gateway's WebSocket."""

    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path or raven_home() / "serve.json"
        self._ids = itertools.count(1)

    def _state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise GatewayUnavailableError("Raven is not running here; start it with `raven web`") from None
        if not state.get("port") or not state.get("token"):
            raise GatewayUnavailableError(f"{self.state_path} names no port or token; restart `raven web`")
        return state

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        import aiohttp

        state = self._state()
        base = f"http://127.0.0.1:{state['port']}"
        headers = {"Origin": base, "X-Raven-Token": str(state["token"])}
        request_id = next(self._ids)
        try:
            async with aiohttp.ClientSession() as http, http.ws_connect(f"{base}/rpc", headers=headers) as ws:
                await ws.send_json({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        break
                    frame = json.loads(message.data)
                    if frame.get("id") != request_id:
                        continue
                    if "error" in frame:
                        error = frame["error"]
                        raise RuntimeError(f"{method}: {error.get('message')} {error.get('data') or ''}".strip())
                    return frame.get("result")
        except aiohttp.ClientError as exc:
            raise GatewayUnavailableError(f"could not reach Raven at {base}: {exc}") from exc
        raise GatewayUnavailableError(f"{method}: the gateway closed the connection without answering")


def build_server(call: Call | None = None) -> FastMCP:
    """The MCP server; ``call`` is the gateway's JSON-RPC (a live client unless given one)."""
    rpc: Call = call or GatewayClient().call
    server = FastMCP("raven")

    @server.tool()
    async def create_task(prompt: str, session_id: str = "") -> dict[str, Any]:
        """Start a task in Raven: the prompt is sent as a message to a conversation.

        With no session_id a new conversation is created for it. Returns the
        conversation and the turn the task runs as; follow it with list_tasks.
        """
        if not prompt.strip():
            raise ValueError("prompt is empty")
        if not session_id:
            created = await rpc("session.create", {})
            session_id = str(created["session_id"])
        sent = await rpc("turn.send", {"session_key": session_id, "content": prompt})
        return {"session_id": session_id, "turn_id": sent.get("turn_id"), "accepted": bool(sent.get("accepted"))}

    @server.tool()
    async def list_tasks(session_id: str) -> list[dict[str, Any]]:
        """The tasks (delegated runs and their nodes) of one conversation."""
        answer = await rpc("tasks.list", {"session_key": session_id})
        return list(answer.get("tasks") or [])

    @server.tool()
    async def list_sessions() -> list[dict[str, Any]]:
        """Raven's conversations, most recent first."""
        answer = await rpc("session.list", {})
        return list(answer.get("sessions") or [])

    @server.tool()
    async def list_agents() -> list[dict[str, Any]]:
        """The agent roster Raven dispatches work to: name, kind, whether enabled."""
        answer = await rpc("subagents.list", {"probe": False})
        return [
            {"name": row.get("name"), "kind": row.get("kind"), "enabled": row.get("enabled")}
            for row in answer.get("rows") or []
        ]

    @server.tool()
    async def list_channels() -> dict[str, Any]:
        """The messaging channels Raven serves and whether each is enabled and running."""
        return dict(await rpc("channels.status", {}) or {})

    return server


def main() -> None:
    build_server().run()


__all__ = ["GatewayClient", "GatewayUnavailableError", "build_server", "main"]

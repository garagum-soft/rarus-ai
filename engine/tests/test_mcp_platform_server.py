"""Raven's own MCP server: an MCP client lists its tools and creates a task through it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from raven.mcp.platform_server import GatewayClient, GatewayUnavailableError, build_server


class _Gateway:
    """The gateway's JSON-RPC, as the methods the tools call answer."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tasks: dict[str, list[dict[str, Any]]] = {}

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "session.create":
            return {"session_id": "tui:20261013_000000_abc123"}
        if method == "turn.send":
            self.tasks.setdefault(params["session_key"], []).append({"id": "t1", "title": params["content"]})
            return {"turn_id": "turn-1", "accepted": True}
        if method == "tasks.list":
            return {"tasks": self.tasks.get(params["session_key"], [])}
        if method == "session.list":
            return {"sessions": [{"session_id": key} for key in self.tasks]}
        if method == "subagents.list":
            return {"rows": [{"name": "Raven", "kind": "builtin", "enabled": True, "description": "x"}]}
        if method == "channels.status":
            return {"channels": [{"name": "telegram", "enabled": False}], "gateway_running": True}
        raise AssertionError(f"unexpected {method}")


def _payload(result: Any) -> Any:
    assert not result.isError, result
    if result.structuredContent is not None:
        content = result.structuredContent
        return content.get("result", content)
    return json.loads(result.content[0].text)


async def test_a_client_sees_the_platform_tools() -> None:
    async with create_connected_server_and_client_session(build_server(_Gateway().call)._mcp_server) as client:
        listed = await client.list_tools()

    assert {tool.name for tool in listed.tools} == {
        "create_task",
        "list_tasks",
        "list_sessions",
        "list_agents",
        "list_channels",
    }


async def test_a_client_creates_a_task_and_gets_it_back() -> None:
    gateway = _Gateway()
    async with create_connected_server_and_client_session(build_server(gateway.call)._mcp_server) as client:
        created = _payload(await client.call_tool("create_task", {"prompt": "summarise the repo"}))
        tasks = _payload(await client.call_tool("list_tasks", {"session_id": created["session_id"]}))

    assert created == {"session_id": "tui:20261013_000000_abc123", "turn_id": "turn-1", "accepted": True}
    assert [method for method, _ in gateway.calls] == ["session.create", "turn.send", "tasks.list"]
    assert gateway.calls[1][1] == {"session_key": created["session_id"], "content": "summarise the repo"}
    assert tasks == [{"id": "t1", "title": "summarise the repo"}]


async def test_a_task_can_join_an_existing_conversation() -> None:
    gateway = _Gateway()
    async with create_connected_server_and_client_session(build_server(gateway.call)._mcp_server) as client:
        created = _payload(await client.call_tool("create_task", {"prompt": "next step", "session_id": "tui:x"}))

    assert created["session_id"] == "tui:x"
    assert [method for method, _ in gateway.calls] == ["turn.send"]


async def test_roster_sessions_and_channels_come_from_the_gateway() -> None:
    gateway = _Gateway()
    async with create_connected_server_and_client_session(build_server(gateway.call)._mcp_server) as client:
        agents = _payload(await client.call_tool("list_agents", {}))
        channels = _payload(await client.call_tool("list_channels", {}))

    assert agents == [{"name": "Raven", "kind": "builtin", "enabled": True}]
    assert channels["channels"][0]["name"] == "telegram"


async def test_a_failure_reaches_the_client_as_a_tool_error() -> None:
    async def down(method: str, params: dict[str, Any]) -> Any:
        raise GatewayUnavailableError("Raven is not running here; start it with `rarus web`")

    async with create_connected_server_and_client_session(build_server(down)._mcp_server) as client:
        result = await client.call_tool("list_agents", {})
        empty = await client.call_tool("create_task", {"prompt": "  "})

    assert result.isError and "rarus web" in result.content[0].text
    assert empty.isError


async def test_without_a_running_raven_the_client_is_told_how_to_start_one(tmp_path: Path) -> None:
    with pytest.raises(GatewayUnavailableError, match="rarus web"):
        await GatewayClient(tmp_path / "serve.json").call("session.list", {})
    (tmp_path / "serve.json").write_text("{}", encoding="utf-8")
    with pytest.raises(GatewayUnavailableError, match="no port or token"):
        await GatewayClient(tmp_path / "serve.json").call("session.list", {})

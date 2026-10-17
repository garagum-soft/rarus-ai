"""Onboarding an outside agent: the connection for a chosen client and the instructions to give it."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from raven.mcp.onboarding import CLIENTS, ToolInfo, client, connect_text, instructions_text, onboard, tool_catalog

CMD = ["/opt/raven/bin/raven", "mcp", "serve"]
TOOLS = [
    ToolInfo("create_task", "Start a task in Raven.\n\nMore detail.", ("prompt", "session_id")),
    ToolInfo("list_tasks", "The tasks of one conversation.", ("session_id",)),
    ToolInfo("list_agents", "The agent roster.", ()),
]


def test_a_cli_client_gets_one_command_around_the_servers_own_launcher() -> None:
    result = onboard("claude-code", TOOLS, command=CMD, windows=False)

    assert result.kind == "cli" and result.filename == ""
    assert result.connect == "claude mcp add raven -- /opt/raven/bin/raven mcp serve"
    assert onboard("codex", TOOLS, command=CMD, windows=False).connect.startswith("codex mcp add raven -- ")
    assert (
        onboard("gemini", TOOLS, command=CMD, windows=False).connect
        == "gemini mcp add raven /opt/raven/bin/raven mcp serve"
    )


def test_a_config_client_gets_its_file_and_the_entry_to_put_in_it() -> None:
    desktop = onboard("claude-desktop", TOOLS, command=CMD)
    vscode = onboard("vscode", TOOLS, command=CMD)

    assert desktop.filename == "claude_desktop_config.json"
    assert json.loads(desktop.connect) == {
        "mcpServers": {"raven": {"command": "/opt/raven/bin/raven", "args": ["mcp", "serve"]}}
    }
    assert json.loads(vscode.connect)["servers"]["raven"]["type"] == "stdio"


def test_the_instructions_name_the_servers_tools_and_how_to_delegate() -> None:
    text = onboard("cursor", TOOLS, command=CMD).instructions

    assert "create_task(prompt, session_id): Start a task in Raven." in text
    assert "More detail" not in text, "one line per tool"
    assert "list_tasks(session_id)" in text and "check list_agents" in text
    assert "rarus web" in text


def test_a_windows_path_with_spaces_is_quoted_for_cmd() -> None:
    line = connect_text(client("claude-code"), [r"C:\Program Files\raven\raven.exe", "mcp", "serve"], windows=True)
    assert line == r'claude mcp add raven -- "C:\Program Files\raven\raven.exe" mcp serve'


def test_an_unknown_client_names_the_known_ones() -> None:
    with pytest.raises(KeyError, match="claude-code"):
        onboard("nope", TOOLS, command=CMD)


async def test_the_catalog_is_read_from_the_server_itself() -> None:
    catalog = {t.name: t for t in await tool_catalog()}

    assert {"create_task", "list_tasks", "list_sessions", "list_agents", "list_channels"} <= set(catalog)
    assert catalog["create_task"].params == ("prompt", "session_id")
    assert all(t.name in instructions_text(list(catalog.values())) for t in catalog.values())


async def test_the_gateway_answers_every_client_in_the_contracts_shape() -> None:
    from raven.rpc.methods.console import settings_mcp_clients
    from raven.rpc.models import SettingsMcpClientsResult

    answer = await settings_mcp_clients({})

    parsed = SettingsMcpClientsResult.model_validate(answer)
    assert [c.client for c in parsed.clients] == [c.id for c in CLIENTS]
    assert all("create_task" in c.instructions for c in parsed.clients)


@pytest.mark.slow  # a real `rarus mcp serve` subprocess, spoken to over stdio
async def test_the_written_config_starts_a_server_an_mcp_client_can_talk_to() -> None:
    """End to end: the entry a config client is told to write, spawned the way that client spawns it."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    entry = json.loads(onboard("cursor", TOOLS, command=[sys.executable, "-m", "raven", "mcp", "serve"]).connect)
    server = entry["mcpServers"]["raven"]

    async def talk() -> list[str]:
        params = StdioServerParameters(command=server["command"], args=server["args"])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return [t.name for t in (await session.list_tools()).tools]

    names = await asyncio.wait_for(talk(), timeout=90)

    assert {"create_task", "list_tasks", "list_agents"} <= set(names)

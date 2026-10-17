"""Onboarding an outside agent: for a chosen client, the line that connects it and the text that briefs it.

Raven's own MCP server (``rarus mcp serve``, :mod:`raven.mcp.platform_server`)
is how another agent -- Claude Code, Codex, Gemini CLI, Cursor, an IDE -- drives
the Raven running here. Each client is wired up differently: some take one
command (``claude mcp add …``), some read a JSON file at a path of their own.
For every known client this answers with exactly that, built around the
absolute command that starts the server on this machine, plus the instructions
an agent needs once connected. The instructions are written from the server's
own tool list, so they name the tools the server actually has.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

SERVER_NAME = "raven"
SERVE_ARGS = ("mcp", "serve")


@dataclass(frozen=True)
class Client:
    id: str
    label: str
    kind: Literal["cli", "config"]
    filename: str | None = None


CLIENTS: tuple[Client, ...] = (
    Client("claude-code", "Claude Code", "cli"),
    Client("codex", "Codex", "cli"),
    Client("gemini", "Gemini CLI", "cli"),
    Client("claude-desktop", "Claude Desktop", "config", "claude_desktop_config.json"),
    Client("cursor", "Cursor", "config", ".cursor/mcp.json"),
    Client("vscode", "VS Code", "config", ".vscode/mcp.json"),
)


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    params: tuple[str, ...] = ()


@dataclass(frozen=True)
class Onboarding:
    client: str
    label: str
    kind: Literal["cli", "config"]
    connect: str
    filename: str
    instructions: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def server_command() -> list[str]:
    """The absolute command that starts Raven's MCP server here: the installed ``raven``, else this interpreter."""
    found = shutil.which("raven")
    head = [found] if found else [sys.executable, "-m", "raven"]
    return [*head, *SERVE_ARGS]


def client(client_id: str) -> Client:
    for known in CLIENTS:
        if known.id == client_id:
            return known
    names = ", ".join(c.id for c in CLIENTS)
    raise KeyError(f"unknown client {client_id!r}; known clients: {names}")


def _join(argv: Sequence[str], windows: bool) -> str:
    return subprocess.list2cmdline(list(argv)) if windows else shlex.join(argv)


def connect_text(chosen: Client, command: Sequence[str], *, windows: bool | None = None) -> str:
    """The command to run, or the file content to write, that connects ``chosen`` to the server."""
    windows = os.name == "nt" if windows is None else windows
    exe, *args = command
    if chosen.id == "claude-code":
        return _join(["claude", "mcp", "add", SERVER_NAME, "--", *command], windows)
    if chosen.id == "codex":
        return _join(["codex", "mcp", "add", SERVER_NAME, "--", *command], windows)
    if chosen.id == "gemini":
        return _join(["gemini", "mcp", "add", SERVER_NAME, *command], windows)
    entry: dict[str, Any] = {"command": exe, "args": args}
    if chosen.id == "vscode":
        return json.dumps({"servers": {SERVER_NAME: {"type": "stdio", **entry}}}, indent=2)
    return json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2)


def instructions_text(tools: Sequence[ToolInfo]) -> str:
    """What an agent connected to the server needs to know, naming the server's own tools."""
    lines = [
        f'You are connected to Raven, an agent platform running on this machine, through the MCP server "{SERVER_NAME}".',
        "Use it to hand work to Raven and to follow that work, instead of doing it yourself when Raven is asked for.",
        "",
        "Tools:",
    ]
    for tool in tools:
        summary = tool.description.strip().splitlines()[0] if tool.description.strip() else ""
        signature = f"{tool.name}({', '.join(tool.params)})"
        lines.append(f"- {signature}: {summary}" if summary else f"- {signature}")
    names = {t.name for t in tools}
    lines.append("")
    if {"create_task", "list_tasks"} <= names:
        lines.append(
            "To delegate: call create_task with the whole request as the prompt, keep the session_id it returns, "
            "and check progress with list_tasks(session_id). Reuse the session_id to continue the same conversation."
        )
    if "list_agents" in names:
        lines.append("Before naming an agent in a prompt, check list_agents for the ones that are enabled.")
    lines.append(
        "Raven has to be running (`rarus web` or `rarus serve`); if a tool says it is not, ask the user to start it."
    )
    return "\n".join(lines)


async def tool_catalog() -> list[ToolInfo]:
    """The tools Raven's MCP server offers, read from the server itself."""
    from raven.mcp.platform_server import build_server

    async def unused(method: str, params: dict[str, Any]) -> Any:
        raise RuntimeError("the catalog is read without a gateway")

    listed = await build_server(call=unused).list_tools()
    return [
        ToolInfo(t.name, t.description or "", tuple((t.inputSchema or {}).get("properties", {}).keys())) for t in listed
    ]


def onboard(
    client_id: str,
    tools: Sequence[ToolInfo],
    *,
    command: Sequence[str] | None = None,
    windows: bool | None = None,
) -> Onboarding:
    """The connection line and the agent's instructions for one client."""
    chosen = client(client_id)
    return Onboarding(
        client=chosen.id,
        label=chosen.label,
        kind=chosen.kind,
        connect=connect_text(chosen, list(command or server_command()), windows=windows),
        filename=chosen.filename or "",
        instructions=instructions_text(tools),
    )


def onboard_all(
    tools: Sequence[ToolInfo], *, command: Sequence[str] | None = None, windows: bool | None = None
) -> list[Onboarding]:
    cmd = list(command or server_command())
    return [onboard(c.id, tools, command=cmd, windows=windows) for c in CLIENTS]


__all__ = [
    "CLIENTS",
    "Client",
    "Onboarding",
    "ToolInfo",
    "client",
    "connect_text",
    "instructions_text",
    "onboard",
    "onboard_all",
    "server_command",
    "tool_catalog",
]

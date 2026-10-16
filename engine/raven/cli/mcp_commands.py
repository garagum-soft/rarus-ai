"""``raven mcp bridge``: the frame pump's process shell. ``raven mcp serve``: Raven's own MCP server.

A sub-agent spawns this and speaks newline-delimited JSON-RPC to its stdin and
stdout, so the only bytes allowed on stdout are frames. ``claim_stdout`` is what
enforces that: it moves fd 1 to stderr and hands back a duplicate, so a library
that prints on import cannot corrupt the stream.
"""

from __future__ import annotations

import asyncio
import json

import typer

from raven.acp.stdio import claim_stdout
from raven.mcp.bridge import run_bridge

mcp_app = typer.Typer(name="mcp", help="MCP: the plumbing for sub-agents, and Raven's own MCP server.")


@mcp_app.command("bridge")
def bridge(socket_path: str = typer.Argument(..., help="Unix socket the host is listening on")) -> None:
    """Relay MCP frames between this process's stdio and a host-held server."""
    with claim_stdout() as frames:
        code = asyncio.run(run_bridge(socket_path, frames))
    raise typer.Exit(code)


@mcp_app.command("serve")
def serve() -> None:
    """Serve Raven's own tools (tasks, sessions, agents, channels) to an MCP client over stdio.

    Drives the Raven running on this machine (`raven web`): register this
    command as a stdio MCP server in the client, e.g. `raven mcp serve`.
    """
    from raven.mcp.platform_server import main

    main()


@mcp_app.command("onboard")
def onboard(
    client: str = typer.Argument("", help="Client id (claude-code, codex, gemini, claude-desktop, cursor, vscode)"),
    json_output: bool = typer.Option(False, "--json", help="Emit the connection and instructions as JSON"),
) -> None:
    """Connect an outside agent: the command (or config file) for its client and the instructions to give it."""
    from raven.mcp.onboarding import CLIENTS, tool_catalog
    from raven.mcp.onboarding import onboard as build

    if not client:
        for known in CLIENTS:
            where = f"  writes {known.filename}" if known.filename else ""
            typer.echo(f"{known.id:<16}{known.label}{where}")
        typer.echo("\nPick one: raven mcp onboard <client>")
        return
    try:
        result = build(client, asyncio.run(tool_catalog()))
    except KeyError as exc:
        typer.echo(str(exc.args[0]), err=True)
        raise typer.Exit(1) from None
    if json_output:
        typer.echo(json.dumps(result.to_dict(), indent=2))
        return
    if result.kind == "cli":
        typer.echo(f"Run this to connect {result.label}:\n\n  {result.connect}\n")
    else:
        typer.echo(f"Add this to {result.filename} for {result.label}:\n\n{result.connect}\n")
    typer.echo(f"Instructions for the agent:\n\n{result.instructions}")

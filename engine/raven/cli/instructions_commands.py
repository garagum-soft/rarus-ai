"""``rarus instructions``: the revision log of the agent's instruction files.

``log`` lists the revisions, ``show`` prints one, ``diff`` compares two for
review, ``record`` keeps the file's current text as a revision, and
``rollback`` puts an older revision back as a new one. A file is named by its
short name (``soul``, ``agent``, ``tools``) or its workspace-relative path.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from raven.agent.instructions import INSTRUCTION_FILES, InstructionError, InstructionHistory

instructions_app = typer.Typer(help="Revisions of the agent's instruction files: log, diff, rollback.")
console = Console()

SHORT_NAMES = {
    "soul": "agent_memory/profile/soul.md",
    "agent": "agent_memory/profile/agent.md",
    "tools": "TOOLS.md",
}


def _workspace(workspace: str | None) -> Path:
    if workspace:
        return Path(workspace).expanduser()
    from raven.config.loader import load_config

    return load_config().workspace_path


def _key(name: str) -> str:
    return SHORT_NAMES.get(name.lower(), name)


def _history(workspace: str | None) -> InstructionHistory:
    return InstructionHistory(_workspace(workspace))


def _fail(exc: Exception) -> None:
    console.print(f"[red]{exc}[/red]")
    raise typer.Exit(1)


WorkspaceOpt = typer.Option(None, "--workspace", "-w", help="Workspace directory (default: the configured one).")


@instructions_app.command("log")
def log_cmd(
    name: str = typer.Argument("agent", help="soul | agent | tools, or a path"), workspace: str | None = WorkspaceOpt
) -> None:
    """List the revisions of one instruction file, oldest first."""
    history = _history(workspace)
    try:
        entries = history.log(_key(name))
        head = history.head(_key(name))
    except InstructionError as exc:
        _fail(exc)
        return
    if not entries:
        console.print(
            f"No revisions recorded for {_key(name)}. `rarus instructions record {name}` keeps the current text."
        )
        return
    table = Table(title=f"{_key(name)} (head: {head})")
    for column in ("rev", "when", "author", "action", "note"):
        table.add_column(column)
    for entry in entries:
        action = entry.action if entry.restored_from is None else f"{entry.action} <- {entry.restored_from}"
        mark = "*" if entry.rev == head else ""
        when = datetime.fromtimestamp(entry.at).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(f"{entry.rev}{mark}", when, entry.author, action, entry.note)
    console.print(table)


@instructions_app.command("show")
def show_cmd(
    name: str = typer.Argument(..., help="soul | agent | tools, or a path"),
    rev: int | None = typer.Option(None, "--rev", "-r", help="Revision (default: head)."),
    workspace: str | None = WorkspaceOpt,
) -> None:
    """Print one revision of an instruction file."""
    history = _history(workspace)
    try:
        typer.echo(history.text_at(_key(name), rev if rev is not None else history.head(_key(name))), nl=False)
    except InstructionError as exc:
        _fail(exc)


@instructions_app.command("diff")
def diff_cmd(
    name: str = typer.Argument(..., help="soul | agent | tools, or a path"),
    a: int = typer.Argument(..., help="From revision."),
    b: int | None = typer.Argument(None, help="To revision (default: head)."),
    workspace: str | None = WorkspaceOpt,
) -> None:
    """Show what changed between two revisions, for review."""
    try:
        typer.echo(_history(workspace).diff(_key(name), a, b) or "(no difference)")
    except InstructionError as exc:
        _fail(exc)


@instructions_app.command("record")
def record_cmd(
    name: str = typer.Argument(..., help="soul | agent | tools, or a path"),
    note: str = typer.Option("", "--note", "-m", help="Why it changed."),
    author: str = typer.Option("user", "--author"),
    workspace: str | None = WorkspaceOpt,
) -> None:
    """Keep the file's current text as a revision (nothing happens if it equals the head)."""
    try:
        revision = _history(workspace).record(_key(name), author=author, note=note)
    except InstructionError as exc:
        _fail(exc)
        return
    console.print(f"recorded revision {revision.rev}" if revision else "unchanged since the head; nothing recorded")


@instructions_app.command("rollback")
def rollback_cmd(
    name: str = typer.Argument(..., help="soul | agent | tools, or a path"),
    rev: int = typer.Argument(..., help="The revision to put back."),
    note: str = typer.Option("", "--note", "-m"),
    author: str = typer.Option("user", "--author"),
    workspace: str | None = WorkspaceOpt,
) -> None:
    """Put an older revision back; it is recorded as a new revision, so nothing is lost."""
    history = _history(workspace)
    try:
        history.record(_key(name), author=author, note="the text before the rollback")
        revision = history.rollback(_key(name), rev, author=author, note=note)
    except InstructionError as exc:
        _fail(exc)
        return
    console.print(f"{_key(name)} is back at revision {rev}, recorded as revision {revision.rev}")


@instructions_app.command("files")
def files_cmd() -> None:
    """The instruction files that are tracked."""
    for path in INSTRUCTION_FILES:
        short = next((s for s, p in SHORT_NAMES.items() if p == path), "")
        console.print(f"{short:6} {path}")

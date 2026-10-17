"""``raven session`` subapp — manage conversation sessions.

User-facing "session id" is the bare chat_id (strip "cli:" prefix for
display; re-prepend internally). Full session key = "cli:<chat_id>".

Persistence semantics:
- ``session create`` mints a new id and immediately persists minimal
  metadata (zero messages, title optional), so the id shows up in
  ``list`` and can be ``resume``d or ``delete``d right away. The CLI
  process dies as soon as the command returns, so a lazy (in-memory)
  session would be lost — hence eager persistence, diverging from the
  TUI's lazy semantics.
- Untitled sessions are auto-named from their first user message (see
  ``raven.session.manager``); ``--title`` names the session up front
  and is never overwritten.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from raven.cli._log_silence import mute_subsystem_logs_unless_debug
from raven.config.paths import get_workspace_path
from raven.session.export import default_export_path, write_transcript
from raven.session.manager import SessionManager, new_chat_id

console = Console()

session_app = typer.Typer(
    help="Manage conversation sessions.",
    no_args_is_help=True,
)

_CLI_CHANNEL = "cli"


@session_app.callback()
def _suppress_info_logs() -> None:
    mute_subsystem_logs_unless_debug()


def _open_manager() -> SessionManager:
    return SessionManager(get_workspace_path())


def _bare_id(key: str) -> str:
    if key.startswith(f"{_CLI_CHANNEL}:"):
        return key[len(f"{_CLI_CHANNEL}:") :]
    return key


def _full_key(bare_or_key: str) -> str:
    if ":" in bare_or_key:
        return bare_or_key
    return f"{_CLI_CHANNEL}:{bare_or_key}"


def resolve_session(manager: SessionManager, id_or_prefix: str) -> str:
    """Resolve a bare id or prefix to a full session key.

    Searches only the cli channel. Returns the key on unambiguous match.
    Exits with code 1 on not-found or ambiguous prefix.
    """
    sessions = manager.list_sessions(channel=_CLI_CHANNEL)
    bare_input = _bare_id(id_or_prefix)

    exact = [s for s in sessions if _bare_id(s["key"]) == bare_input]
    if exact:
        return exact[0]["key"]

    prefix_matches = [s for s in sessions if _bare_id(s["key"]).startswith(bare_input)]
    if not prefix_matches:
        console.print(f"[red]No session matching id/prefix {id_or_prefix!r}[/red]")
        raise typer.Exit(code=1)
    if len(prefix_matches) > 1:
        cands = ", ".join(_bare_id(s["key"]) for s in prefix_matches[:8])
        console.print(f"[red]Ambiguous prefix {id_or_prefix!r} — candidates: {cands}[/red]")
        raise typer.Exit(code=1)
    return prefix_matches[0]["key"]


def resolve_session_cross_channel(manager: SessionManager, value: str) -> str:
    """Resolve the agent ``--session`` value to a full ``channel:chat_id`` key.

    Unlike :func:`resolve_session` (cli-only, exits on no-match), this searches
    every channel and never leaves a colon-less value that would mis-path:

    - already a full key (contains ':') → returned unchanged;
    - bare id with exactly one exact chat_id match → that full key;
    - bare id with exactly one prefix match → that full key;
    - multiple matches → ``typer.BadParameter`` listing candidate full keys;
    - no match → ``cli:<value>`` fallback (mirrors the interactive bare-as-cli
      path, allowing a new cli session to be named/created).

    Delegates the resolution rules to :meth:`SessionManager.resolve_key`; this
    function only maps the structured outcome to the agent path's CLI behavior.
    """
    res = manager.resolve_key(value)
    if res.status == "ambiguous":
        cands = ", ".join(res.candidates[:8])
        raise typer.BadParameter(f"{value!r} matches multiple sessions: {cands}. Pass the full channel:chat_id key.")
    if res.status == "resolved":
        assert res.key is not None  # noqa: S101 - resolve_key fills key on the resolved outcome
        return res.key
    return f"{_CLI_CHANNEL}:{value}"


# ── create ────────────────────────────────────────────────────────────


@session_app.command("create")
def session_create(
    title: str | None = typer.Option(None, "--title", "-t", help="Optional session title"),
) -> None:
    """Mint a new session id and persist it immediately.

    Minimal metadata is written to disk right away (zero messages), so
    the new id shows up in ``list`` and can be deleted. Without --title
    the session is auto-named from its first user message.
    """
    chat_id = new_chat_id()
    key = f"{_CLI_CHANNEL}:{chat_id}"

    manager = _open_manager()
    session = manager.get_or_create(key)
    if title is not None:
        session.set_title(title)
    manager.save(session)

    if title is not None:
        console.print(f"[green]✓[/green] Created session [cyan]{chat_id}[/cyan] (title: {title!r})")
        console.print(f"  Use with: rarus agent --session {key}")
    else:
        console.print(chat_id)
        console.print(f"[dim]  (use: rarus agent --session {key})[/dim]")


# ── list ──────────────────────────────────────────────────────────────


@session_app.command("list")
def session_list(
    all_channels: bool = typer.Option(
        False,
        "--all",
        help="Show sessions from all channels (default: cli only)",
    ),
) -> None:
    """List sessions, sorted by most recently updated.

    Default shows only sessions on the ``cli`` channel (within-surface).
    Pass ``--all`` to include sessions from every channel.
    """
    manager = _open_manager()
    channel_filter = None if all_channels else _CLI_CHANNEL
    sessions = manager.list_sessions(channel=channel_filter)

    if not sessions:
        label = "all channels" if all_channels else "cli channel"
        console.print(f"[dim]No sessions found ({label}).[/dim]")
        return

    table = Table()
    table.add_column("ID", style="cyan")
    if all_channels:
        table.add_column("Channel")
    table.add_column("Title")
    table.add_column("Messages", justify="right")
    table.add_column("Updated")

    for s in sessions:
        key = s["key"]
        channel, _, chat_id = key.partition(":")
        meta = s.get("metadata") or {}
        title = meta.get("title") or "-"
        updated = (s.get("updated_at") or "")[:19].replace("T", " ")
        msg_count = str(s.get("message_count", 0))

        row = [chat_id]
        if all_channels:
            row.append(channel)
        row += [title, msg_count, updated]
        table.add_row(*row)

    console.print(table)


# ── resume ────────────────────────────────────────────────────────────


@session_app.command("resume")
def session_resume(
    id_or_prefix: str = typer.Argument(..., metavar="ID", help="Session id or unique prefix"),
) -> None:
    """Resolve a session id or prefix and print the full session key.

    The printed key can be fed directly to ``rarus agent --session``.
    This command is a pure resolver — it does not start an agent loop.
    """
    manager = _open_manager()
    key = resolve_session(manager, id_or_prefix)
    console.print(key)
    console.print(f"[dim]  Use with: rarus agent --session {key}[/dim]")


# ── delete ────────────────────────────────────────────────────────────


@session_app.command("delete")
def session_delete(
    id_or_key: str = typer.Argument(..., metavar="ID", help="Bare session id or full key"),
) -> None:
    """Delete a session by bare id or full key (e.g. ``cli:<id>``)."""
    manager = _open_manager()
    key = _full_key(id_or_key)

    if not manager.exists(key):
        console.print(f"[red]Session not found: {id_or_key!r}[/red]")
        raise typer.Exit(code=1)

    if manager.delete(key):
        console.print(f"[green]✓[/green] Deleted session {_bare_id(key)}")
    else:
        console.print(f"[red]Failed to delete session {id_or_key!r}[/red]")
        raise typer.Exit(code=1)


# ── fork ──────────────────────────────────────────────────────────────


@session_app.command("fork")
def session_fork(
    id_or_prefix: str = typer.Argument(..., metavar="ID", help="Session id or unique prefix to fork"),
    title: str | None = typer.Option(None, "--title", "-t", help="Optional title for the forked session"),
) -> None:
    """Fork a session into a new diverging copy (fork-at-head).

    Resolves the id/prefix within the cli channel, copies its full history
    into a fresh session, records the source as the child's parent, and prints
    the new child id (first line) for use with ``rarus agent --session``.
    """
    manager = _open_manager()
    key = resolve_session(manager, id_or_prefix)
    child = manager.fork(key, title=title)
    if child is None:
        console.print(f"[red]Cannot fork {id_or_prefix!r}: session is empty or missing[/red]")
        raise typer.Exit(code=1)

    child_bare = _bare_id(child.key)
    console.print(child_bare)
    console.print(f"[dim]  (forked from {_bare_id(key)}; use: rarus agent --session {child.key})[/dim]")


# ── export ────────────────────────────────────────────────────────────


@session_app.command("export")
def session_export(
    id_or_prefix: str = typer.Argument(..., metavar="ID", help="Session id, prefix, or full channel:chat_id key"),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write to this path (absolute, or relative to the current directory) instead of the default exports dir",
    ),
) -> None:
    """Export a session transcript to a human-readable Markdown file.

    Resolves the id across channels (full key / bare id / unique prefix). A
    value matching no session exits non-zero rather than creating one, since
    export is read-only.
    """
    manager = _open_manager()
    res = manager.resolve_key(id_or_prefix)
    if res.status == "ambiguous":
        cands = ", ".join(res.candidates[:8])
        console.print(f"[red]Ambiguous id {id_or_prefix!r} — candidates: {cands}[/red]")
        raise typer.Exit(code=1)
    session = manager.peek(res.key) if res.status == "resolved" else None
    if session is None:
        console.print(f"[red]No session matching {id_or_prefix!r}[/red]")
        raise typer.Exit(code=1)
    dest = output if output is not None else default_export_path(get_workspace_path(), res.key)
    try:
        written = write_transcript(session, dest)
    except OSError as exc:
        console.print(f"[red]Failed to write export to {dest}: {exc}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/green] Exported to [cyan]{written}[/cyan]")


__all__ = ["session_app", "resolve_session", "resolve_session_cross_channel"]


@session_app.command("switch")
def switch_cmd(
    session: str = typer.Argument(..., help="The conversation the instance belongs to"),
    agent: str = typer.Argument(..., help="The roster agent"),
    handle: str = typer.Argument(..., help="The instance's handle"),
    to: str = typer.Option(..., "--to", help="terminal or chat"),
    policy: str = typer.Option("drain", "--policy", help="drain: wait for work in flight; interrupt: stop it"),
    timeout: float = typer.Option(120.0, "--timeout", help="Seconds to wait for work in flight"),
    command: str = typer.Option("", "--command", help="The terminal resume command; must contain {agent_id}"),
) -> None:
    """Hand an agent instance's conversation between Raven's chat and the agent's own terminal (a tmux pane)."""
    import asyncio

    from raven.agent.subagent.instances import InstanceRegistry
    from raven.agent.subagent.interface_switch import InterfaceSwitch, SwitchError, local_hooks, resume_template
    from raven.home import raven_home
    from raven.ops.pane_board import PaneBoard

    if to not in ("terminal", "chat"):
        console.print("[red]--to is terminal or chat[/red]")
        raise typer.Exit(1)
    board = PaneBoard(raven_home() / "panes" / "board.json")
    switch = InterfaceSwitch(local_hooks(InstanceRegistry(), board))
    key = (session, agent, handle)
    try:
        if to == "terminal":
            template = command
            if not template:
                from raven.config.loader import load_config

                rows = {a.name: a for a in load_config().subagents.agents}
                template = resume_template(agent, str(getattr(rows.get(agent), "command", "") or "")) or ""
            if not template:
                console.print(
                    f"[red]No known terminal resume command for {agent}; pass --command '... {{agent_id}}'[/red]"
                )
                raise typer.Exit(1)
            done = asyncio.run(switch.to_terminal(key, template=template, policy=policy, timeout_s=timeout))  # type: ignore[arg-type]
            console.print(
                f"{agent}/{handle}: in the terminal, pane {done.pane}, conversation {done.agent_id}", markup=False
            )
        else:
            done = asyncio.run(switch.to_chat(key, policy=policy, timeout_s=timeout))  # type: ignore[arg-type]
            console.print(f"{agent}/{handle}: back in chat, conversation {done.agent_id}", markup=False)
    except (SwitchError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None


@session_app.command("switches")
def switches_cmd(session: str = typer.Argument("", help="Only this conversation")) -> None:
    """The interface switches written down, newest last, with the phase each reached."""
    from raven.agent.subagent.interface_switch import TransitionLog

    rows = [t for t in TransitionLog().all() if not session or t.session_key == session]
    if not rows:
        console.print("no switches")
    for t in rows:
        tail = f" ({t.error})" if t.error else ""
        console.print(
            f"{t.agent}/{t.handle}@{t.session_key}: {t.source} -> {t.target} [{t.policy}] {t.phase}{tail}", markup=False
        )


@session_app.command("mark-read")
def mark_read_cmd(key: str = typer.Argument(..., help="Session key, e.g. tui:20261013_101500_abcdef")) -> None:
    """Mark everything in a session so far as read; read messages keep for the shorter retention."""
    from datetime import datetime

    from raven.session.retention import mark_read

    mark_read(_open_manager(), key, at=datetime.now())
    console.print(f"{key}: marked read")


@session_app.command("retention")
def retention_cmd(
    read_days: float = typer.Option(..., "--read-days", help="How long read messages keep"),
    unread_days: float = typer.Option(..., "--unread-days", help="How long unread messages keep"),
    session: list[str] = typer.Option([], "--session", help="Only this session; repeatable (default: every session)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Say what would go, change nothing"),
) -> None:
    """Remove messages past their retention, keeping anything a kept message depends on."""
    from datetime import datetime

    from raven.session.retention import RetentionPolicy, apply

    manager = _open_manager()
    try:
        policy = RetentionPolicy(read_days=read_days, unread_days=unread_days)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    keys = session or [str(item.get("key")) for item in manager.list_sessions() if item.get("key")]
    now = datetime.now()
    for key in keys:
        outcome = apply(manager, key, policy, now=now, dry_run=dry_run)
        kept = len(outcome.kept_for)
        verb = "would remove" if dry_run else "removed"
        console.print(f"{key}: {verb} {len(outcome.drop)}, kept {len(outcome.keep)} ({kept} kept for a dependency)")

"""``rarus team``: join, stop and remove roster agents through requests the leader approves;
``rarus team list`` shows the teams (projects) sharing this Raven and their limits.

``request-join``, ``request-shutdown`` and ``request-remove`` file a request and
change nothing; ``approve`` applies one and ``reject`` closes it, and both are
refused unless the acting identity (``--as`` or ``RAVEN_TEAM_IDENTITY``) is the
leader named by ``subagents.leader``.
"""

from __future__ import annotations

import json
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table

from raven.agent.subagent.membership import MembershipError, MembershipLedger, acting_identity, default_ledger

team_app = typer.Typer(help="Join, stop and remove roster agents through requests the leader approves.")
console = Console()

AsOpt = typer.Option(None, "--as", help="Who is acting (default: $RAVEN_TEAM_IDENTITY).")


def _ledger() -> MembershipLedger:
    return default_ledger()


def _fail(exc: Exception) -> None:
    console.print(f"[red]{exc}[/red]")
    raise typer.Exit(1)


def _filed(kind: str, agent: str, who: str | None, reason: str, entry: dict | None = None) -> None:
    try:
        request = _ledger().request(kind, agent, requested_by=acting_identity(who), reason=reason, entry=entry)  # type: ignore[arg-type]
    except MembershipError as exc:
        _fail(exc)
        return
    console.print(f"filed {kind} request {request.id} for {agent}; nothing changes until the leader approves it")


@team_app.command("request-join")
def request_join(
    name: str = typer.Argument(..., help="The agent's roster name."),
    entry: str = typer.Option(
        ..., "--entry", help='Its config row as JSON, e.g. \'{"kind": "cli", "command": "codex exec {prompt}"}\'.'
    ),
    reason: str = typer.Option("", "--reason"),
    who: str | None = AsOpt,
) -> None:
    """Ask for an agent to join the roster."""
    try:
        row = json.loads(entry)
    except ValueError as exc:
        _fail(exc)
        return
    _filed("join", name, who, reason, row)


@team_app.command("request-shutdown")
def request_shutdown(
    name: str = typer.Argument(...), reason: str = typer.Option("", "--reason"), who: str | None = AsOpt
) -> None:
    """Ask for a roster agent to be stopped (kept on the roster, disabled)."""
    _filed("shutdown", name, who, reason)


@team_app.command("request-remove")
def request_remove(
    name: str = typer.Argument(...), reason: str = typer.Option("", "--reason"), who: str | None = AsOpt
) -> None:
    """Ask for a roster agent to be removed."""
    _filed("remove", name, who, reason)


@team_app.command("requests")
def list_requests(all_: bool = typer.Option(False, "--all", help="Include decided requests.")) -> None:
    """The pending requests (all of them with --all)."""
    ledger = _ledger()
    rows = ledger.requests() if all_ else ledger.requests("pending")
    if not rows:
        console.print("no requests" if all_ else "no pending requests")
        return
    table = Table(title=f"membership requests (leader: {ledger.leader})")
    for column in ("id", "kind", "agent", "by", "status", "decided by", "when"):
        table.add_column(column)
    for r in rows:
        when = datetime.fromtimestamp(r.decided_at or r.created_at).strftime("%Y-%m-%d %H:%M")
        table.add_row(r.id, r.kind, r.agent, r.requested_by, r.status, r.decided_by, when)
    console.print(table)


@team_app.command("approve")
def approve(
    request_id: str = typer.Argument(...), note: str = typer.Option("", "--note"), who: str | None = AsOpt
) -> None:
    """Approve a request and apply it to the roster (leader only)."""
    try:
        request = _ledger().approve(request_id, approver=acting_identity(who), note=note)
    except MembershipError as exc:
        _fail(exc)
        return
    console.print(f"approved: {request.kind} {request.agent}")


@team_app.command("reject")
def reject(
    request_id: str = typer.Argument(...), note: str = typer.Option("", "--note"), who: str | None = AsOpt
) -> None:
    """Reject a request; the roster is not touched (leader only)."""
    try:
        request = _ledger().reject(request_id, approver=acting_identity(who), note=note)
    except MembershipError as exc:
        _fail(exc)
        return
    console.print(f"rejected: {request.kind} {request.agent}")


@team_app.command("list")
def list_teams() -> None:
    """The teams (projects) sharing this Rarus, their limits, and the conversations each one owns."""
    from raven.agent.subagent.teams import TeamTable
    from raven.config.loader import load_config
    from raven.home import raven_home

    config = load_config()
    table = TeamTable.from_rows(config.subagents.teams)
    if not table:
        console.print("no teams configured (subagents.teams); every conversation runs under the host's limits")
        return
    sessions_root = raven_home() / "workspace" / "sessions"
    keys = []
    if sessions_root.is_dir():
        for channel in sessions_root.iterdir():
            if channel.is_dir():
                keys += [f"{channel.name}:{item.stem}" for item in channel.iterdir()]
    view = Table(title="teams")
    for column in ("team", "sessions", "spawns/hour", "max in flight", "conversations"):
        view.add_column(column)
    for team in table.teams:
        owned = [key for key in keys if table.team_of(key) is team]
        view.add_row(
            team.name,
            ", ".join(team.sessions),
            str(team.max_spawns_per_hour or "host"),
            str(team.max_concurrent or "host"),
            str(len(owned)),
        )
    console.print(view)

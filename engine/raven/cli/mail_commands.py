"""``rarus mail``: agent mail from a shell -- an external agent's own line, and the operator's view."""

from __future__ import annotations

import json
import os
from dataclasses import asdict

import typer

mail_app = typer.Typer(name="mail", help="Agent mail: messages sub-agents of one conversation send each other.")

_SESSION_HELP = "The conversation (default: $RAVEN_MAIL_SESSION, set for an agent Raven started)"


def _session(session: str) -> str:
    key = session or os.environ.get("RAVEN_MAIL_SESSION", "")
    if not key:
        typer.echo("no conversation: pass --session (see `rarus mail sessions`)", err=True)
        raise typer.Exit(2)
    return key


def _me(name: str) -> str:
    return name or os.environ.get("RAVEN_MAIL_AS", "") or "operator"


@mail_app.command("send")
def send(
    to: str = typer.Argument(..., help="The recipient's name"),
    text: str = typer.Argument(..., help="The message"),
    kind: str = typer.Option("note", "--kind", help="note, question or reply"),
    reply_to: str = typer.Option("", "--reply-to", help="The id of the message this answers"),
    session: str = typer.Option("", "--session", help=_SESSION_HELP),
    as_name: str = typer.Option("", "--as", help="Send as (default: $RAVEN_MAIL_AS, else operator)"),
) -> None:
    """Write to a participant by name; it reaches the recipient at its next step or start."""
    from raven.agent.subagent.mailbox import Mailbox, MailError

    try:
        sent = Mailbox(_session(session)).send(_me(as_name), to, text, kind=kind, reply_to=reply_to or None)
    except MailError as exc:
        typer.echo(f"not sent: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"sent {sent.id} to {sent.to}")


@mail_app.command("inbox")
def inbox(
    session: str = typer.Option("", "--session", help=_SESSION_HELP),
    as_name: str = typer.Option("", "--as", help="Whose inbox (default: $RAVEN_MAIL_AS, else operator)"),
    peek: bool = typer.Option(False, "--peek", help="Show without marking read"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Unread messages, marked read unless --peek."""
    from raven.agent.subagent.mailbox import Mailbox

    box, me = Mailbox(_session(session)), _me(as_name)
    mail = box.unread(me) if peek else box.take(me)
    if json_output:
        typer.echo(json.dumps([asdict(m) for m in mail], ensure_ascii=False, indent=2))
        return
    if not mail:
        typer.echo("no unread messages")
        return
    for m in mail:
        typer.echo(m.render() + "\n")


@mail_app.command("history")
def history(
    session: str = typer.Option("", "--session", help=_SESSION_HELP),
    member: str = typer.Option("", "--of", help="Only what this participant sent or got"),
    peer: str = typer.Option("", "--with", help="Only messages with this participant"),
    limit: int = typer.Option(50, "--limit"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """The conversation's mail, oldest first."""
    from raven.agent.subagent.mailbox import Mailbox

    mail = Mailbox(_session(session)).history(member=member or None, peer=peer or None, limit=limit)
    if json_output:
        typer.echo(json.dumps([asdict(m) for m in mail], ensure_ascii=False, indent=2))
        return
    if not mail:
        typer.echo("no messages")
        return
    for m in mail:
        extra = f" (reply to {m.reply_to})" if m.reply_to else (" (question)" if m.kind == "question" else "")
        typer.echo(f"{m.id}  {m.sender} -> {m.to}{extra}: {m.body}")


@mail_app.command("members")
def members(session: str = typer.Option("", "--session", help=_SESSION_HELP)) -> None:
    """Who can be written to in this conversation, with unread counts."""
    from raven.agent.subagent.mailbox import Mailbox

    box = Mailbox(_session(session))
    rows = box.members()
    if not rows:
        typer.echo("nobody has joined this conversation's mail")
        return
    for name, row in sorted(rows.items()):
        typer.echo(f"{name:<24} agent={row.get('agent')}  unread={len(box.unread(name))}")


@mail_app.command("sessions")
def sessions() -> None:
    """Conversations that have mail."""
    from raven.agent.subagent.mailbox import default_root

    root = default_root()
    found = False
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found = True
        typer.echo(
            f"{state.get('session')}  members={len(state.get('members', {}))}  sent={state.get('next_seq', 1) - 1}"
        )
    if not found:
        typer.echo("no conversation has mail yet")

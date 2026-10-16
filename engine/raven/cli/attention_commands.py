"""``raven attention``: what is waiting on a person, most pressing first."""

from __future__ import annotations

import json
import time

import typer

attention_app = typer.Typer(name="attention", help="The operator's attention queue: what is waiting on a person.")


@attention_app.command("list")
def list_items(
    session: str = typer.Option("", "--session", help="Only this conversation's items"),
    all_items: bool = typer.Option(False, "--all", help="Also the resolved items still kept"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """The open items by priority (waiting long enough raises one), then oldest first."""
    from raven.proactive_engine.attention import queue

    now = time.time()
    q = queue()
    q.reap(now=now)
    items = q.items(session_key=session or None, include_resolved=all_items, now=now)
    if json_output:
        typer.echo(json.dumps({"items": [i.to_json(now) for i in items], "summary": q.summary(now=now)}, indent=2))
        return
    if not items:
        typer.echo("nothing is waiting on you")
        return
    for item in items:
        age = int((now - item.created) // 60)
        where = f" [{item.session_key}]" if item.session_key else ""
        state = "" if item.status == "open" else f" ({item.resolution})"
        typer.echo(f"{item.effective_priority(now):<9}{item.title}{where}  {age} min  id={item.id}{state}")
    summary = q.summary(now=now)
    typer.echo("\n" + ", ".join(f"{n} {p}" for p, n in summary.items() if n))


@attention_app.command("resolve")
def resolve(
    item_id: str = typer.Argument(..., help="The item's id, as `list` shows it"),
    resolution: str = typer.Option("dismissed", "--as", help="What became of it"),
) -> None:
    """Dismiss one item by hand."""
    from raven.proactive_engine.attention import queue

    if not queue().resolve(item_id, resolution):
        typer.echo(f"no open item {item_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{item_id}: {resolution}")

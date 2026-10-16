"""``raven notify``: try a channel, send an event through the rules, read what was sent."""

from __future__ import annotations

import json
import time

import typer

notify_app = typer.Typer(
    name="notify", help="Operator notifications: rules route events to webhook, ntfy, sound or desktop."
)


def _notifier():
    from raven.config.loader import load_config
    from raven.proactive_engine.notify import Notifier, Settings

    return Notifier(Settings.from_config(load_config().gateway.notifications))


@notify_app.command("test")
def test_channel(channel: str = typer.Argument(..., help="A channel name from gateway.notifications.channels")) -> None:
    """Send one test message on a channel, past the rules (a switched-off channel still sends nothing)."""
    from raven.proactive_engine.notify import Event, Settings

    notifier = _notifier()
    settings: Settings = notifier.settings
    notifier.settings = Settings(
        enabled=True,
        channels=settings.channels,
        rules=[{"on": ["*"], "channel": channel, "min_priority": "low"}],
        daily_cap=settings.daily_cap,
    )
    [delivery] = notifier.dispatch(
        Event(kind="test", priority="critical", title="Raven test notification", key=f"test:{time.time()}")
    ) or [None]
    if delivery is None or not delivery.sent:
        typer.echo(f"{channel}: not sent ({delivery.reason if delivery else 'no delivery'})", err=True)
        raise typer.Exit(1)
    typer.echo(f"{channel}: sent")


@notify_app.command("send")
def send(
    title: str = typer.Argument(...),
    kind: str = typer.Option("manual", "--kind"),
    priority: str = typer.Option("high", "--priority"),
    detail: str = typer.Option("", "--detail"),
) -> None:
    """Route one event through the rules, as an agent's problem would be."""
    from raven.proactive_engine.notify import Event

    deliveries = _notifier().dispatch(
        Event(kind=kind, priority=priority, title=title, key=f"manual:{title}", detail=detail)
    )
    if not deliveries:
        typer.echo("no rule routes this event (or notifications are off)")
        return
    for d in deliveries:
        typer.echo(f"{d.channel}: {'sent' if d.sent else 'not sent (' + d.reason + ')'}")


@notify_app.command("log")
def show_log(limit: int = typer.Option(20, "--limit", "-n")) -> None:
    """The latest deliveries, and the reasons for the ones that did not go."""
    from raven.proactive_engine.notify import default_log_path

    path = default_log_path()
    if not path.is_file():
        typer.echo("nothing sent yet")
        return
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        row = json.loads(line)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["time"]))
        state = "sent" if row["sent"] else f"held ({row['reason']})"
        typer.echo(f"{stamp}  {row['channel']:<12} {state:<34} {row['priority']:<9} {row['title']}")

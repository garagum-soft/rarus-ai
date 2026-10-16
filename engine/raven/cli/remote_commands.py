"""``raven remote``: pair a remote device with this Raven, list the paired ones, revoke one.

``pair`` prints a one-time ticket good for five minutes and how a device trades
it for a token (``POST /pair`` on the gateway). The device then reaches the
gateway with ``X-Raven-Device-Token`` -- over a tunnel or a LAN route the owner
provides; Raven itself only listens on this machine.
"""

from __future__ import annotations

import json
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table

from raven.auth.pairing import default_store

remote_app = typer.Typer(help="Pair remote devices with this Raven, list them, revoke them.")
console = Console()


def _gateway_port() -> int | None:
    from raven.home import raven_home

    try:
        return int(json.loads((raven_home() / "serve.json").read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError):
        return None


@remote_app.command("pair")
def pair(ttl: int = typer.Option(300, "--ttl", min=30, max=3600, help="Seconds the ticket stays good")) -> None:
    """Issue a one-time pairing ticket."""
    ticket, seconds = default_store().issue_ticket(ttl)
    port = _gateway_port()
    console.print(f"ticket: {ticket}", markup=False, soft_wrap=True)
    console.print(f"good for {seconds} seconds, once.")
    where = f"http://<this machine>:{port}" if port else "the gateway (start it with `raven web`)"
    console.print(
        f'On the device: POST {where}/pair with {{"ticket": "{ticket}", "device": "<name>"}}; '
        "the answer carries the device token for X-Raven-Device-Token.",
        markup=False,
        soft_wrap=True,
    )


@remote_app.command("devices")
def devices() -> None:
    """The paired devices."""
    rows = default_store().devices()
    if not rows:
        console.print("no paired devices")
        return
    table = Table(title="paired devices")
    table.add_column("device")
    table.add_column("paired")
    for row in rows:
        table.add_row(row.name, datetime.fromtimestamp(row.paired_at).strftime("%Y-%m-%d %H:%M"))
    console.print(table)


@remote_app.command("revoke")
def revoke(device: str = typer.Argument(..., help="The device name, as listed")) -> None:
    """Revoke a device; its token stops working at once."""
    if not default_store().revoke(device):
        console.print(f"[red]no paired device named {device!r}[/red]")
        raise typer.Exit(1)
    console.print(f"revoked {device}")

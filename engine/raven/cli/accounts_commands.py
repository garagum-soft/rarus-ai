"""Typer subcommands: ``raven accounts`` -- accounts mode, one server for several people.

``raven accounts serve`` runs the account gateway; nothing else here changes how
a Raven runs. The rest is the same administration the admin page offers, for the
server's terminal: the first account (``init``), accounts, credits, prices and
settings.
"""

from __future__ import annotations

import typer
from rich.table import Table

from raven.cli._helpers import console

accounts_app = typer.Typer(help="Accounts mode: one server, an engine per person, credits by the admin's prices.")


def _password(prompt: str = "Password") -> str:
    return typer.prompt(prompt, hide_input=True, confirmation_prompt=True)


def _account(username: str):
    from raven.accounts.store import AccountStore

    account = AccountStore().by_username(username)
    if account is None:
        console.print(f"[red]Error:[/red] no account named {username!r}")
        raise typer.Exit(1)
    return account


@accounts_app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Address to listen on; 0.0.0.0 for the local network."),
    port: int = typer.Option(8800, "--port", "-p", help="Port to listen on."),
    autostart: bool = typer.Option(True, "--autostart/--no-autostart", help="Start every account's engine now."),
) -> None:
    """Run the account gateway: sign-in, the admin page, and each person's own engine behind it."""
    from aiohttp import web

    from raven.accounts.hub import Hub
    from raven.accounts.store import AccountStore

    hub = Hub(autostart=autostart)
    where = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    if AccountStore().count() == 0:
        console.print(
            f"No account yet: open {where} on this machine to make the superadmin, or run `raven accounts init`."
        )
    console.print(f"Account gateway on {host}:{port} -- {where}")
    web.run_app(hub.app(), host=host, port=port, print=None, handle_signals=True)


@accounts_app.command("stop-engines")
def stop_engines() -> None:
    """Stop every account's engine; they keep running after the gateway stops, for their schedules and messengers."""
    import os
    import subprocess
    import sys

    from raven.accounts.engines import read_serve
    from raven.accounts.paths import engine_home
    from raven.accounts.store import AccountStore

    for account in AccountStore().all():
        home = engine_home(account)
        if read_serve(home) is None:
            continue
        done = subprocess.run(  # noqa: S603 - this interpreter, a fixed argv
            [sys.executable, "-m", "raven", "gateway", "stop"],
            env=dict(os.environ, RAVEN_HOME=str(home)),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        state = "stopping" if done.returncode == 0 else "did not answer"
        console.print(f"{account.username}: {state}")


@accounts_app.command("init")
def init(username: str = typer.Option(..., "--username", "-u", help="The superadmin's username.")) -> None:
    """Make the first account, the superadmin, whose Raven is the one already on this server."""
    from raven.accounts.store import AccountError, AccountStore, AuditLog

    try:
        account = AccountStore().create(username, _password(), role="superadmin", server_home=True, first_only=True)
    except AccountError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    AuditLog().record("terminal", "setup", account.username)
    console.print(f"[green]✓[/green] superadmin {account.username} created")


@accounts_app.command("list")
def list_accounts() -> None:
    """Every account: role, balance, lent access, state."""
    from raven.accounts.store import AccountStore

    table = Table("username", "role", "balance", "lent", "state")
    for a in AccountStore().all():
        role = a.role + (f" ({', '.join(a.permissions)})" if a.role == "admin" and a.permissions else "")
        table.add_row(
            a.username, role, f"{a.balance:,.2f}", ", ".join(a.lent) or "-", "disabled" if a.disabled else "active"
        )
    console.print(table)


@accounts_app.command("add")
def add(
    username: str = typer.Argument(..., help="The new account's username."),
    role: str = typer.Option("user", "--role", help="user or admin."),
) -> None:
    """Make an account with a password."""
    from raven.accounts.settings import SettingsStore
    from raven.accounts.store import AccountError, AccountStore, AuditLog

    if role not in ("user", "admin"):
        console.print("[red]Error:[/red] the role is user or admin")
        raise typer.Exit(1)
    try:
        account = AccountStore().create(username, _password(), role=role)
    except AccountError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    AuditLog().record("terminal", "account_create", account.username, role=role)
    from raven.accounts.billing import Ledger, welcome_bonus

    balance = welcome_bonus(AccountStore(), Ledger(), account.id, SettingsStore().load())
    console.print(f"[green]✓[/green] {account.username} created, {balance:,.0f} credits")


@accounts_app.command("topup")
def topup(
    username: str = typer.Argument(...),
    credits: float = typer.Argument(..., help="Credits to add; negative takes them back."),
    note: str = typer.Option("", "--note"),
) -> None:
    """Add credits to an account."""
    from raven.accounts.billing import Ledger, top_up
    from raven.accounts.store import AccountStore, AuditLog

    account = _account(username)
    balance = top_up(AccountStore(), Ledger(), account.id, credits, actor="terminal", note=note)
    AuditLog().record("terminal", "topup", account.username, credits=credits, balance=balance)
    console.print(f"[green]✓[/green] {account.username}: {balance:,.2f} credits")


@accounts_app.command("price")
def price(
    model: str = typer.Argument(
        "", help="The model, as the usage files name it; `*` for every other, `cli:*` for CLI agents."
    ),
    input_price: float = typer.Option(None, "--input", help="US dollars per million input tokens."),
    output_price: float = typer.Option(None, "--output", help="US dollars per million output tokens."),
    cache_hit: float = typer.Option(0.0, "--cache-hit", help="US dollars per million cached input tokens."),
    remove: bool = typer.Option(False, "--remove"),
) -> None:
    """Show the price table, or set one model's price."""
    from raven.accounts.billing import Price, PriceTable

    prices = PriceTable()
    if model:
        if remove:
            prices.set(model, None)
        elif input_price is None or output_price is None:
            console.print(
                "[red]Error:[/red] give --input and --output (and --cache-hit), in US dollars per million tokens"
            )
            raise typer.Exit(1)
        else:
            prices.set(model, Price(input_price, output_price, cache_hit))
    table = Table("model", "input $/M", "output $/M", "cache hit $/M")
    for name, p in sorted(prices.load().items()):
        table.add_row(name, f"{p.input:g}", f"{p.output:g}", f"{p.cache_hit:g}")
    console.print(table)


@accounts_app.command("set")
def set_setting(
    key: str = typer.Argument(
        ..., help="public_url, registration_open, google_client_id, google_client_secret, disk_quota_gb, ..."
    ),
    value: str = typer.Argument(None, help="The value; omitted for google_client_secret, which is asked for hidden."),
) -> None:
    """Change one of the account gateway's settings."""
    from raven.accounts.settings import SettingsStore

    if value is None:
        value = typer.prompt(key, hide_input=key == "google_client_secret")
    try:
        settings = SettingsStore().update({key: value})
    except (ValueError, TypeError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    shown = settings.public()
    console.print(f"[green]✓[/green] {key} = {shown.get(key, '(set)')}")


@accounts_app.command("role")
def role(
    username: str = typer.Argument(...),
    new_role: str = typer.Argument(..., help="admin or user."),
    grant: list[str] = typer.Option([], "--grant", help="A permission for an admin (repeatable)."),
) -> None:
    """Make an account an admin (with the permissions granted) or a user."""
    from raven.accounts.store import PERMISSIONS, AccountError, AccountStore, AuditLog

    account = _account(username)
    if account.role == "superadmin" or new_role not in ("admin", "user"):
        console.print("[red]Error:[/red] an account other than the superadmin is made an admin or a user")
        raise typer.Exit(1)
    unknown = sorted(set(grant) - set(PERMISSIONS))
    if unknown:
        console.print(f"[red]Error:[/red] unknown permission(s): {', '.join(unknown)}; known: {', '.join(PERMISSIONS)}")
        raise typer.Exit(1)

    def change(row) -> None:
        row.role = new_role
        row.permissions = list(grant) if new_role == "admin" else []

    try:
        AccountStore().update(account.id, change)
    except AccountError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    AuditLog().record("terminal", "account_update", account.username, role=new_role, permissions=list(grant))
    console.print(f"[green]✓[/green] {account.username} is {new_role}")


@accounts_app.command("passwd")
def passwd(username: str = typer.Argument(...)) -> None:
    """Set an account's password."""
    from raven.accounts.store import AccountStore, AuditLog, hash_password

    account = _account(username)
    new_hash = hash_password(_password("New password"))

    def change(row) -> None:
        row.password_hash = new_hash

    AccountStore().update(account.id, change)
    AuditLog().record("terminal", "account_update", account.username, password="reset")  # noqa: S106 - what happened, not a password
    console.print(f"[green]✓[/green] password of {account.username} changed")


@accounts_app.command("audit")
def audit(limit: int = typer.Option(50, "--limit", "-n")) -> None:
    """The latest entries of the audit log."""
    import time

    from raven.accounts.store import AuditLog

    table = Table("when", "actor", "action", "target")
    for row in AuditLog().tail(limit):
        table.add_row(
            time.strftime("%Y-%m-%d %H:%M", time.localtime(row.get("ts", 0))),
            str(row.get("actor")),
            str(row.get("action")),
            str(row.get("target")),
        )
    console.print(table)


__all__ = ["accounts_app"]

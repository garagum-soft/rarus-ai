"""``rarus table``: table apps and the sources they sync from."""

from __future__ import annotations

import asyncio
import json

import typer

table_app = typer.Typer(name="table", help="Table apps kept in step with outside sources (CSV or JSON, file or URL).")


def _store():
    from raven.tables.store import TableStore

    return TableStore()


def _fail(exc: Exception) -> None:
    typer.echo(str(exc), err=True)
    raise typer.Exit(1)


@table_app.command("create")
def create(
    name: str = typer.Argument(...),
    key: str = typer.Option(..., "--key", help="The column that identifies a row"),
    column: list[str] = typer.Option([], "--column", "-c", help="A column, in order; repeatable"),
) -> None:
    """Create an empty table."""
    from raven.tables.store import TableError

    try:
        table = _store().create(name, key, column)
    except TableError as exc:
        _fail(exc)
    typer.echo(f"{table.name}: created, key {table.key}")


@table_app.command("list")
def list_tables() -> None:
    """The tables, with their row and source counts."""
    store = _store()
    for name in store.names():
        table = store.load(name)
        typer.echo(f"{name:<24}{len(table.rows)} rows, {len(table.sources)} source(s)")


@table_app.command("source")
def add_source(
    name: str = typer.Argument(..., help="The table"),
    source: str = typer.Argument(..., help="A name for the source"),
    location: str = typer.Argument(..., help="A file path or an http(s) URL"),
    kind: str = typer.Option("", "--kind", help="csv or json (default: from the extension)"),
    path: str = typer.Option("", "--path", help="Dotted path to the list inside a JSON document"),
    mapping: list[str] = typer.Option([], "--map", help="column=field; repeatable"),
    every: float = typer.Option(0, "--every", help="Seconds between syncs by the gateway's pass (0: on demand)"),
) -> None:
    """Add a source the table syncs from."""
    from raven.tables.store import Source

    guessed = kind or ("json" if location.lower().split("?")[0].endswith(".json") else "csv")
    try:
        pairs = dict(item.split("=", 1) for item in mapping)
        _store().add_source(
            name, Source(name=source, kind=guessed, location=location, path=path, mapping=pairs, every=every or None)
        )
    except ValueError as exc:
        _fail(exc)
    typer.echo(f"{name}: source {source} ({guessed}) added")


@table_app.command("sync")
def sync(
    name: str = typer.Argument(...),
    source: list[str] = typer.Option([], "--source", help="Only this source; repeatable"),
) -> None:
    """Sync the table from its sources now."""
    from raven.tables.store import TableError
    from raven.tables.sync import sync_table

    try:
        results = asyncio.run(sync_table(_store(), name, sources=source or None))
    except TableError as exc:
        _fail(exc)
    for result in results:
        typer.echo(result.summary())
    if any(r.error for r in results):
        raise typer.Exit(1)


@table_app.command("show")
def show(name: str = typer.Argument(...), json_output: bool = typer.Option(False, "--json")) -> None:
    """The table's rows."""
    from raven.tables.store import SOURCE_FIELD, TableError

    try:
        table = _store().load(name)
    except TableError as exc:
        _fail(exc)
    if json_output:
        typer.echo(json.dumps(table.to_json(), ensure_ascii=False, indent=2))
        return
    columns = [*table.columns, SOURCE_FIELD]
    typer.echo("\t".join(columns))
    for row in table.rows:
        typer.echo("\t".join("" if row.get(c) is None else str(row.get(c)) for c in columns))


@table_app.command("set")
def set_cell(
    name: str = typer.Argument(...),
    key: str = typer.Argument(..., help="The row's key"),
    column: str = typer.Argument(...),
    value: str = typer.Argument(...),
) -> None:
    """Edit one cell here. A source's own rows take the source's value again on its next sync."""
    from raven.tables.store import TableError

    try:
        _store().set_cell(name, key, column, value)
    except TableError as exc:
        _fail(exc)
    typer.echo(f"{name}/{key}: {column} = {value}")


@table_app.command("log")
def log(name: str = typer.Argument(...)) -> None:
    """The table's recent syncs."""
    from raven.tables.store import TableError
    from raven.tables.sync import SyncResult

    try:
        entries = _store().load(name).log
    except TableError as exc:
        _fail(exc)
    for entry in entries:
        typer.echo(f"{entry['at']}  {SyncResult(**entry).summary()}")

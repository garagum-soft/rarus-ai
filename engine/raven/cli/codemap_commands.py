"""``raven codemap``: the code index of a project -- build it, read the ranked map, find a definition."""

from __future__ import annotations

from pathlib import Path

import typer

codemap_app = typer.Typer(
    name="codemap", help="Index a code base and map it: ranked outlines and where names are defined."
)


def _locate(path: Path) -> tuple[Path, str]:
    from raven.codemap.index import project_root

    target = path.expanduser().resolve()
    if not target.exists():
        typer.echo(f"{target} does not exist", err=True)
        raise typer.Exit(1)
    if target == Path.home().resolve() or target.parent == target:
        typer.echo(f"refusing to index {target}; name a project directory", err=True)
        raise typer.Exit(2)
    root = project_root(target)
    base = target if target.is_dir() else target.parent
    under = base.relative_to(root).as_posix() if base != root else ""
    return root, "" if under == "." else under


def _refreshed(root: Path, deadline: float):
    from raven.codemap.index import CodeIndex

    index = CodeIndex(root)
    stats = index.refresh(deadline_s=deadline or None)
    return index, stats


@codemap_app.command("index")
def index_cmd(
    path: Path = typer.Argument(Path("."), help="The project (its repository root is used when it has one)"),
    deadline: float = typer.Option(0, "--deadline", help="Stop after this many seconds and say so (0: no limit)"),
) -> None:
    """Build or bring up to date the project's index."""
    root, _ = _locate(path)
    index, stats = _refreshed(root, deadline)
    symbols, _ = index.counts()
    typer.echo(f"{root}: {stats.line()}")
    typer.echo(f"{symbols} definitions; index at {index.db}")


@codemap_app.command("show")
def show_cmd(
    path: Path = typer.Argument(Path("."), help="The project, or a subtree of it to map"),
    query: str = typer.Option("", "--query", "-q", help="Words for what you are looking for"),
    focus: list[str] = typer.Option([], "--focus", help="A file you are working on; its neighbours rank higher"),
    budget: int = typer.Option(6000, "--budget", help="Characters of output"),
) -> None:
    """Print the ranked map: the files that matter, each with the outline of what it defines."""
    from raven.codemap.query import render_map

    root, under = _locate(path)
    index, stats = _refreshed(root, 0)
    rel_focus = []
    for item in focus:
        try:
            rel_focus.append(Path(item).expanduser().resolve().relative_to(root).as_posix())
        except ValueError:
            typer.echo(f"--focus {item}: not inside {root}", err=True)
            raise typer.Exit(2) from None
    typer.echo(render_map(index, query=query, focus=rel_focus, under=under, budget=budget))


@codemap_app.command("find")
def find_cmd(
    name: str = typer.Argument(..., help="A name, or Parent.name"),
    path: Path = typer.Argument(Path("."), help="The project, or a subtree to search"),
    limit: int = typer.Option(20, "--limit", help="At most this many definitions"),
) -> None:
    """Where a name is defined."""
    from raven.codemap.query import render_find

    root, under = _locate(path)
    index, _ = _refreshed(root, 0)
    typer.echo(render_find(index, name, under=under, limit=limit))


@codemap_app.command("drop")
def drop_cmd(path: Path = typer.Argument(Path("."), help="The project whose index to delete")) -> None:
    """Delete the project's index; the next use builds it again."""
    from raven.codemap.index import CodeIndex

    root, _ = _locate(path)
    index = CodeIndex(root)
    typer.echo(f"dropped {index.db}" if index.drop() else f"no index for {root}")

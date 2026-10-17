"""``rarus areas``: roster agents that own a part of a project, with a journal and, if asked, a standing duty."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import typer

areas_app = typer.Typer(
    name="areas", help="Areas of responsibility: an agent owns part of a project, keeps a journal, may keep watch."
)


def _manager():
    from raven.agent.subagent.manager import SubagentManager
    from raven.config.loader import load_config
    from raven.providers.auth import MissingCredentialsError
    from raven.providers.factory import make_lazy_provider
    from raven.providers.pool import ProviderPool

    config = load_config()
    try:
        provider = make_lazy_provider(config)
    except MissingCredentialsError:
        provider = None
    return SubagentManager(
        provider=provider,  # type: ignore[arg-type]
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        sandbox_config=config.tools.sandbox,
        agents=config.subagents.agents,
        provider_pool=ProviderPool(config),
    )


def _when(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if at else "never"


@areas_app.command("assign")
def assign(
    name: str = typer.Argument(..., help="The area's name (lowercase, '-' or '_')"),
    agent: str = typer.Option(..., "--agent", "-a", help="The roster agent that owns it"),
    path: list[str] = typer.Option(
        [], "--path", "-p", help="A directory (all under it) or glob it covers, relative to the project; repeatable"
    ),
    topic: list[str] = typer.Option([], "--topic", "-t", help="A topic word it covers; repeatable"),
    charter: str = typer.Option("", "--charter", help="Standing instructions: what it looks after, what good means"),
    charter_file: Path | None = typer.Option(None, "--charter-file", help="Read the charter from a file"),
    project: Path | None = typer.Option(None, "--project", help="The checkout the owner works in (default: here)"),
    every: str = typer.Option("off", "--every", help="Run its standing duty this often (6h, 1d; off: only when asked)"),
) -> None:
    """Give an agent an area to own; assigning a name again replaces it, keeping its journal."""
    from raven.agent.subagent.areas import Area, AreaError, AreaStore, parse_every

    text = charter
    if charter_file is not None:
        try:
            text = charter_file.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(f"--charter-file: {exc}", err=True)
            raise typer.Exit(2) from None
    try:
        area = Area(
            name=name,
            agent=agent,
            project=str((project or Path.cwd()).expanduser().resolve()),
            paths=list(path),
            topics=list(topic),
            charter=text,
            every_s=parse_every(every),
        )
        store = AreaStore()
        previous = store.get(name)
        if previous is not None:
            area.last_run_at, area.last_status = previous.last_run_at, previous.last_status
        store.put(area)
    except AreaError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    duty = f"standing duty every {area.every_s}s" if area.every_s else "runs only when asked"
    typer.echo(
        f"{area.agent} owns '{area.name}' in {area.project}: {', '.join(area.paths + area.topics) or 'its charter'}; {duty}"
    )
    if area.every_s:
        typer.echo("a running gateway starts keeping watch on its next restart; `rarus areas run` runs it now")


@areas_app.command("list")
def list_areas() -> None:
    """Every area, its owner, what it covers and its last run."""
    from raven.agent.subagent.areas import AreaStore

    areas = AreaStore().areas()
    if not areas:
        typer.echo("no area is assigned")
        return
    for a in sorted(areas, key=lambda a: a.name):
        duty = f"every {a.every_s}s" if a.every_s else "on demand"
        typer.echo(f"{a.name}  owner {a.agent}  {duty}  last run {_when(a.last_run_at)} {a.last_status}".rstrip())
        typer.echo(
            f"  {a.project}: {', '.join(a.paths) or '-'}" + (f"  topics: {', '.join(a.topics)}" if a.topics else "")
        )


@areas_app.command("remove")
def remove(name: str = typer.Argument(..., help="The area")) -> None:
    """Take an area away; its journal stays."""
    from raven.agent.subagent.areas import AreaStore

    if not AreaStore().remove(name):
        typer.echo(f"no area {name!r}", err=True)
        raise typer.Exit(1)
    typer.echo(f"removed '{name}'; its journal stays")


@areas_app.command("who")
def who(subject: str = typer.Argument(..., help="A path, or text describing some work")) -> None:
    """Who owns a path or a piece of work, the most specific owner first."""
    from raven.agent.subagent.areas import AreaStore, owners_of_path, owners_of_text

    areas = AreaStore().areas()
    target = Path(subject).expanduser()
    found = owners_of_path(areas, str(target.resolve())) if target.exists() else []
    found = found or owners_of_path(areas, subject) or owners_of_text(areas, subject)
    if not found:
        typer.echo("nobody owns it")
        raise typer.Exit(1)
    for area in found:
        typer.echo(f"{area.agent}  (area '{area.name}')")


@areas_app.command("run")
def run(
    name: str = typer.Argument(..., help="The area"),
    task: str = typer.Option("", "--task", help="Work to do in the area (default: its standing duty)"),
) -> None:
    """Run the owner in its area now -- a task, or its standing duty -- and journal it."""
    from raven.agent.subagent.areas import AreaStore, run_area

    store = AreaStore()
    area = store.get(name)
    if area is None:
        typer.echo(f"no area {name!r}", err=True)
        raise typer.Exit(1)
    manager = _manager()
    entry = asyncio.run(run_area(store, area, manager.run_in, task=task))
    if not entry.get("ok"):
        typer.echo(f"{area.agent} failed in '{area.name}': {entry.get('error')}", err=True)
        raise typer.Exit(1)
    typer.echo(entry.get("report", ""))
    for flag in entry.get("attention") or []:
        typer.echo(f"! raised for attention: {flag}", err=True)


@areas_app.command("log")
def log(
    name: str = typer.Argument(..., help="The area"),
    limit: int = typer.Option(10, "--limit", help="The latest this many entries"),
) -> None:
    """The area's journal: what its owner did, newest last."""
    from raven.agent.subagent.areas import AreaStore

    entries = AreaStore().journal(name.strip().lower(), limit=limit)
    if not entries:
        typer.echo(f"nothing journaled for '{name}'")
        return
    for e in entries:
        state = "ok" if e.get("ok") else "failed"
        said = (e.get("report") or e.get("error") or "").strip().splitlines()
        typer.echo(f"{_when(e.get('at') or 0)}  {e.get('kind')}  {state}  {e.get('task', '')[:80]}")
        if said:
            typer.echo(f"  {said[0][:200]}")
        for flag in e.get("attention") or []:
            typer.echo(f"  ! {flag}")

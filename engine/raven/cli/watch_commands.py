"""``rarus watch``: projects whose failing checks and red CI become tasks by themselves."""

from __future__ import annotations

import time
from pathlib import Path

import typer

watch_app = typer.Typer(
    name="watch", help="Watch a project: failing checks and red CI are filed as tasks on their own."
)


def _every(text: str) -> int:
    text = text.strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text[:-1].isdigit() and text[-1:] in units:
        return int(text[:-1]) * units[text[-1]]
    if text.isdigit():
        return int(text)
    typer.echo(f"--every takes 30m, 2h, 1d or seconds, not {text!r}", err=True)
    raise typer.Exit(2)


@watch_app.command("add")
def add(
    project: Path = typer.Argument(Path("."), help="The project directory"),
    check: list[str] = typer.Option([], "--check", "-c", help="name=command, run in the project; repeatable"),
    every: str = typer.Option("1h", "--every", help="Scan at least this often (and whenever the commit moves)"),
    github: str = typer.Option("", "--github", help="owner/repo whose CI runs to read (GITHUB_TOKEN if private)"),
    branch: str = typer.Option("", "--branch", help="The branch whose CI to read (default: the checkout's)"),
    detect: bool = typer.Option(False, "--detect", help="Also watch the project's recorded and detected checks"),
) -> None:
    """Register a project: its checks are run and its CI read on a schedule, and failures filed as tasks."""
    from raven.stint.project_watch import MIN_EVERY_S, WatchCheck, WatchedProject, WatchStore
    from raven.stint.verify import parse_check_spec

    root = project.expanduser().resolve()
    if not root.is_dir():
        typer.echo(f"{root} is not a directory", err=True)
        raise typer.Exit(1)
    checks: list[WatchCheck] = []
    for entry in check:
        try:
            spec = parse_check_spec(entry)
        except ValueError as exc:
            typer.echo(f"--check {entry!r}: {exc}", err=True)
            raise typer.Exit(2) from exc
        checks.append(WatchCheck(name=spec.name, run=spec.command))
    if detect:
        import json

        from raven.stint.bootstrap import detect_checks
        from raven.stint.checks import checks_path

        try:
            ledger = json.loads(checks_path(root).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ledger = {}
        for key, row in ledger.items():
            if isinstance(row, dict) and row.get("run") and key not in {c.name for c in checks}:
                checks.append(WatchCheck(name=str(key), run=str(row["run"])))
        for spec in detect_checks(root):
            if spec.name not in {c.name for c in checks}:
                checks.append(WatchCheck(name=spec.name, run=spec.command))
    if not checks and not github:
        typer.echo("nothing to watch: give --check name=command, --detect or --github owner/repo", err=True)
        raise typer.Exit(2)
    seconds = _every(every)
    if seconds < MIN_EVERY_S:
        typer.echo(f"--every is at least {MIN_EVERY_S}s", err=True)
        raise typer.Exit(2)
    WatchStore().put(WatchedProject(path=str(root), every_s=seconds, checks=checks, github=github, branch=branch))
    typer.echo(
        f"watching {root}: {', '.join(c.name for c in checks) or 'no local checks'}"
        + (f"; CI of {github}" if github else "")
    )
    typer.echo("a running gateway starts watching it on its next restart; `rarus watch scan` runs a scan now")


@watch_app.command("list")
def list_projects() -> None:
    """Watched projects, when each was last scanned and what is failing."""
    from raven.stint.project_watch import WatchStore

    projects = WatchStore().projects()
    if not projects:
        typer.echo("no project is watched")
        return
    for p in projects:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.last_scan_at)) if p.last_scan_at else "never"
        typer.echo(
            f"{p.path}  every {p.every_s}s  last scan {when}  checks: {', '.join(c.name for c in p.checks) or '-'}"
            + (f"  CI: {p.github}" if p.github else "")
        )
        for key, detail in p.failing.items():
            typer.echo(f"  ! {key}: {detail[:160]}")


@watch_app.command("remove")
def remove(project: Path = typer.Argument(Path("."), help="The project directory")) -> None:
    """Stop watching a project; its filed tasks stay on its backlog."""
    from raven.stint.project_watch import WatchStore

    if not WatchStore().remove(str(project.expanduser().resolve())):
        typer.echo(f"{project} is not watched", err=True)
        raise typer.Exit(1)
    typer.echo(f"stopped watching {project.expanduser().resolve()}")


@watch_app.command("scan")
def scan_now(
    force: bool = typer.Option(True, "--force/--when-due", help="Scan now, or only projects that are due"),
) -> None:
    """Scan the watched projects now and file what they found."""
    from raven.stint.project_watch import WatchStore, watch_once

    results = watch_once(WatchStore(), force=force)
    if not results:
        typer.echo("no project is watched")
        return
    for result, done in results:
        if not result.scanned:
            typer.echo(f"{result.project}: skipped ({result.reason})")
            continue
        typer.echo(
            f"{result.project} @ {result.head[:10] or '-'}: {len(result.findings)} problem(s), {len(result.passing)} passing"
        )
        for f in result.findings:
            mark = "new task" if f.key in done["filed"] else "already filed"
            typer.echo(f"  ! {f.key} ({mark}): {f.detail[:160]}")
            if f.link:
                typer.echo(f"    {f.link}")
        for key in done["cleared"]:
            typer.echo(f"  + {key}: passing again")
        if result.reason:
            typer.echo(f"  ({result.reason})")

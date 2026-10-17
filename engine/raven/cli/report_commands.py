"""``raven report``: reports, retrospectives and the run archive, built from the journals."""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer

report_app = typer.Typer(name="report", help="Reports, retrospectives and the run archive, from what Raven recorded.")


def _paths() -> tuple[Path, Path]:
    from raven.config.loader import load_config
    from raven.home import raven_home

    return Path(load_config().workspace_path).expanduser(), raven_home() / "telemetry"


def _since(text: str) -> int:
    from raven.reports.report import parse_since

    try:
        return parse_since(text)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


@report_app.command("build")
def build(
    since: str = typer.Option("7d", "--since", help="7d, 24h, 90m or a date (2026-10-01)"),
    until: str = typer.Option("", "--until", help="A date; default now"),
    session: str = typer.Option("", "--session", help="Only this conversation"),
    out: Path = typer.Option(None, "--out", "-o", help="Where to write it (default: <RARUS_HOME>/reports/)"),
    json_output: bool = typer.Option(False, "--json", help="Print the report as JSON instead"),
) -> None:
    """A report for a period: conversations, runs, a retrospective and cost. Archives the finished runs."""
    from raven.reports.archive import Archive
    from raven.reports.report import build_report, render_markdown
    from raven.reports.runs import RunEntry

    workspace, telemetry = _paths()
    since_ms, until_ms = _since(since), (_since(until) if until else 0)
    report = build_report(workspace, telemetry, since_ms=since_ms, until_ms=until_ms, session=session or None)
    archived = Archive().update(
        RunEntry(**{k: v for k, v in r.items() if k in RunEntry.__dataclass_fields__}) for r in report["runs"]
    )
    if json_output:
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
        return
    text = render_markdown(report)
    if out is None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = Archive().root / f"report-{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    c = report["conversations"]
    typer.echo(f"report: {out}")
    typer.echo(f"  {c['conversations']} conversations, {len(report['runs'])} runs, {archived} newly archived")
    for item in report["retro"]["went_wrong"][:5]:
        typer.echo(f"  ! {item}")


@report_app.command("runs")
def runs(
    since: str = typer.Option("30d", "--since", help="7d, 24h, 90m or a date"),
    kind: str = typer.Option("", "--kind", help="dag, spawn or stint"),
    failed: bool = typer.Option(False, "--failed", help="Only runs that failed or did not finish"),
    archived: bool = typer.Option(False, "--archived", help="From the archive, including runs whose record is gone"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """The run archive: every graph run, spawn and stint, newest first."""
    from raven.reports.archive import Archive
    from raven.reports.runs import collect_runs

    workspace, _ = _paths()
    since_ms = _since(since)
    live = collect_runs(workspace, since_ms=since_ms)
    archive = Archive()
    archive.update(live)
    rows = archive.rows() if archived else [e.to_dict() for e in live]
    rows = [r for r in rows if (r.get("started_ms") or 0) >= since_ms or not r.get("started_ms")]
    if archived:
        rows.sort(key=lambda r: r.get("started_ms") or 0, reverse=True)
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    if failed:
        rows = [r for r in rows if r.get("failures") or r.get("status") in ("failed", "interrupted", "cancelled")]
    if json_output:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        typer.echo("no runs")
        return
    for r in rows:
        when = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime((r.get("started_ms") or 0) / 1000))
            if r.get("started_ms")
            else "-"
        )
        nodes = ", ".join(f"{n} {s}" for s, n in sorted((r.get("nodes") or {}).items()))
        typer.echo(f"{when}  {r['kind']:<5} {r['status']:<11} {r['id']}  {str(r.get('title') or '')[:50]}  [{nodes}]")


@report_app.command("retro")
def retro(run_id: str = typer.Argument(..., help="A graph run id, a spawn's node id or a stint id")) -> None:
    """The retrospective of one run: what went well, what went wrong, what is still open."""
    from raven.reports.archive import Archive
    from raven.reports.retro import run_retro
    from raven.reports.runs import RunEntry, collect_runs

    workspace, _ = _paths()
    entry = next((e for e in collect_runs(workspace) if e.id == run_id), None)
    if entry is None:
        row = Archive().find(run_id)
        if row is None:
            typer.echo(f"no run {run_id}", err=True)
            raise typer.Exit(1)
        entry = RunEntry(**{k: v for k, v in row.items() if k in RunEntry.__dataclass_fields__})
    typer.echo(run_retro(entry).render(f"{entry.kind} {entry.id}: {entry.title} ({entry.status})"))
    typer.echo(f"record: {entry.record}")

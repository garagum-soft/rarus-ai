"""``rarus ci``: a branch's, commit's or pull request's GitHub CI, and why the red checks are red."""

from __future__ import annotations

import json
from pathlib import Path

import typer

ci_app = typer.Typer(
    name="ci", help="Read GitHub CI: the checks of a branch, commit or pull request, and their failures."
)


@ci_app.command("status")
def status_cmd(
    path: Path = typer.Argument(Path("."), help="The project checkout (its origin and branch are the defaults)"),
    repo: str = typer.Option("", "--repo", help="owner/repo, when it is not the checkout's origin"),
    ref: str = typer.Option("", "--ref", help="Branch, tag or commit SHA (default: the checkout's branch)"),
    pr: int = typer.Option(0, "--pr", help="A pull request number: read its head commit"),
    wait: float = typer.Option(0, "--wait", help="Wait up to this many seconds for running checks to finish"),
    fail_fast: bool = typer.Option(False, "--fail-fast", help="While waiting, stop at the first failed check"),
    logs: bool = typer.Option(True, "--logs/--no-logs", help="Read the failed jobs' logs"),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON"),
) -> None:
    """Print the CI checks; exit 0 when green, 1 when red, 2 when still running or none, 3 when unreadable."""
    from raven.stint import ci

    local = path.expanduser().resolve()
    if local.is_file():
        local = local.parent
    repo = repo.strip() or ci.repo_of(local)
    if not repo:
        typer.echo(f"{local} has no GitHub 'origin' remote; pass --repo owner/repo", err=True)
        raise typer.Exit(3)
    local_head = ""
    try:
        if pr:
            _, ref = ci.resolve_pr(repo, pr)
        elif not ref.strip():
            ref = ci.branch_of(local)
            if not ref:
                typer.echo("the checkout is on a detached head; pass --ref or --pr", err=True)
                raise typer.Exit(3)
            local_head = ci.head_of(local)
        report = (
            ci.wait(repo, ref.strip(), timeout_s=wait, fail_fast=fail_fast) if wait > 0 else ci.read(repo, ref.strip())
        )
        if report.red:
            ci.explain(report, logs=logs)
    except (ValueError, OSError) as exc:
        typer.echo(f"CI for {repo}@{ref or pr} could not be read: {exc}", err=True)
        raise typer.Exit(3) from None
    typer.echo(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) if as_json else report.render(local_head=local_head)
    )
    raise typer.Exit({"success": 0, "failure": 1}.get(report.state, 2))

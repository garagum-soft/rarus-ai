"""``raven review``: independent review of a branch's diff, and the merge that waits for it."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import typer

review_app = typer.Typer(name="review", help="Independent code review: a verdict from someone else before a merge.")


def _book():
    from raven.stint.review import ReviewBook

    return ReviewBook()


def _fail(exc: Exception) -> None:
    remediation = getattr(exc, "remediation", "")
    typer.echo(f"refused: {exc}" + (f"\n  {remediation}" if remediation else ""), err=True)
    raise typer.Exit(1) from exc


def _a_person_at_the_terminal() -> bool:
    """Whether this command was typed at a terminal rather than run by an agent.

    ``--as user:<name>`` is a claim anyone can type, and an agent with a shell
    can run this command: that is how an author approved its own branch as a
    person. An agent's commands never get a terminal on their input (``exec``
    hands them an empty one), and a sub-agent's carry its mail identity.
    """
    return sys.stdin.isatty() and not os.environ.get("RAVEN_MAIL_AS")


@review_app.command("request")
def request(
    branch: str = typer.Argument(..., help="The branch to review"),
    project: Path = typer.Option(Path("."), "--project", "-p", help="The repository it merges into"),
    workdir: Path = typer.Option(None, "--workdir", help="Where the branch is checked out (default: the project)"),
    author: list[str] = typer.Option([], "--author", help="Who wrote it: agent:<name> or user:<name>; repeatable"),
    policy: str = typer.Option("not_creator", "--policy", help="not_creator, human_only or anyone"),
    check: list[str] = typer.Option([], "--check", help="A checklist item; repeatable"),
) -> None:
    """Put a branch up for review as it is now."""
    from raven.stint.review import ReviewError

    try:
        req = _book().request(
            project, workdir or project, branch, authors=author, policy=policy, checklist=check, source="cli"
        )
    except ReviewError as exc:
        _fail(exc)
    typer.echo(f"{req.id}: {req.branch} into {req.into}, {len(req.files)} file(s), policy {req.policy}")


@review_app.command("list")
def list_reviews(json_output: bool = typer.Option(False, "--json")) -> None:
    """Every review, newest first."""
    from dataclasses import asdict

    reqs = _book().list()
    if json_output:
        typer.echo(json.dumps([asdict(r) for r in reqs], ensure_ascii=False, indent=2))
        return
    if not reqs:
        typer.echo("no reviews")
        return
    for r in reqs:
        typer.echo(f"{r.id}  {r.status:<9} {r.branch} -> {r.into}  ({Path(r.project).name}, by {', '.join(r.authors)})")


@review_app.command("show")
def show(
    rid: str = typer.Argument(..., help="The review id"),
    no_diff: bool = typer.Option(False, "--no-diff", help="Leave the diff out"),
) -> None:
    """The diff under review, its checklist, and the verdicts so far."""
    from raven.stint.review import ReviewError, branch_diff

    try:
        req = _book().get(rid)
    except ReviewError as exc:
        _fail(exc)
    typer.echo(f"{req.id}  {req.status}")
    typer.echo(f"branch   {req.branch} -> {req.into}  ({req.project})")
    typer.echo(f"authors  {', '.join(req.authors) or '(unknown)'}   policy {req.policy}")
    typer.echo(f"diff     {req.diff_sha[:12]}  {len(req.files)} file(s): {', '.join(req.files)}")
    for item in req.checklist:
        typer.echo(f"check    {item}")
    for v in req.verdicts:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(v.get("at", 0)))
        current = "" if v.get("diff_sha") == req.diff_sha else "  (of an earlier diff)"
        typer.echo(f"verdict  {stamp} {v['by']}: {v['decision']} {v.get('comment', '')}{current}")
    if not no_diff:
        patch, _ = branch_diff(req.project, req.into, req.branch)
        typer.echo("\n" + patch)


@review_app.command("verdict")
def verdict(
    rid: str = typer.Argument(..., help="The review id"),
    as_actor: str = typer.Option(..., "--as", help="Who judges: user:<name> or agent:<name>"),
    approve: bool = typer.Option(False, "--approve"),
    reject: bool = typer.Option(False, "--reject"),
    check: list[str] = typer.Option([], "--check", help="item=pass|fail|n/a; repeatable"),
    comment: str = typer.Option("", "--comment", "-m"),
    diff: str = typer.Option("", "--diff", help="The diff hash you read (from `show`), to refuse a stale verdict"),
) -> None:
    """Approve or reject. Not the author's to give (policy not_creator); an approval answers every item."""
    from raven.stint.review import ReviewError

    if approve == reject:
        typer.echo("say --approve or --reject", err=True)
        raise typer.Exit(2)
    if as_actor.strip().startswith("user:") and not _a_person_at_the_terminal():
        typer.echo(
            f"refused: a verdict as {as_actor} is given by that person at an interactive terminal, "
            "and this command has none on its input (an agent's commands never do)",
            err=True,
        )
        raise typer.Exit(1)
    answers: dict[str, str] = {}
    for entry in check:
        item, sep, answer = entry.rpartition("=")
        if not sep:
            typer.echo(f"--check takes item=pass|fail|n/a, not {entry!r}", err=True)
            raise typer.Exit(2)
        answers[item.strip()] = answer.strip().lower()
    book = _book()
    try:
        if diff:
            full = book.get(rid).diff_sha
            diff = full if full.startswith(diff) else diff
        req = book.verdict(
            rid,
            as_actor,
            "approve" if approve else "reject",
            checks=answers,
            comment=comment,
            diff_sha=diff or None,
        )
    except ReviewError as exc:
        _fail(exc)
    typer.echo(f"{req.id}: {req.status} by {as_actor}")


@review_app.command("run")
def run(
    rid: str = typer.Argument(..., help="The review id"),
    agent: str = typer.Option(..., "--agent", help="The roster agent that reviews"),
) -> None:
    """Have a roster agent read the diff and give its verdict (refused before it runs if it wrote the work)."""
    from raven.cli.agents_commands import _roster_manager
    from raven.stint.review import ReviewError, review_by_agent

    manager = _roster_manager()
    try:
        req = asyncio.run(review_by_agent(_book(), rid, agent, manager.run_in))
    except ReviewError as exc:
        _fail(exc)
    last = req.verdicts[-1]
    typer.echo(f"{req.id}: {last['decision']} by agent:{agent} -- {last.get('comment', '')}")


@review_app.command("merge")
def merge(rid: str = typer.Argument(..., help="The review id")) -> None:
    """Merge the reviewed branch -- only with an approving verdict on the change as it is now."""
    from raven.playbook.stint_spec import VerifyEntry
    from raven.stint.checks import resolve_checks
    from raven.stint.integrate import integrate_branch
    from raven.stint.review import ReviewError

    book = _book()
    try:
        req = book.get(rid)
    except ReviewError as exc:
        _fail(exc)
    # The playbook's own checks, when a stint filed the review: a merge by hand
    # is measured as the stint's own merge would have been.
    entries = [VerifyEntry.model_validate(row) for row in req.verify]
    checks, missing = resolve_checks(Path(req.project), req.playbook, entries) if entries else ([], [])
    result = integrate_branch(
        Path(req.project),
        Path(req.workdir),
        req.branch,
        checks=checks,
        message=f"merge {req.branch} (review {req.id})",
        review=book,
    )
    if not result.merged:
        typer.echo(result.escalation(), err=True)
        raise typer.Exit(1)
    note = f"; not measured: {', '.join(missing)}" if missing else ""
    typer.echo(f"merged {req.branch} into {result.into} at {result.commit[:12]}{note}")


@review_app.command("log")
def log(
    rid: str = typer.Argument("", help="Only this review"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """The decision journal: requests, verdicts, refused verdicts and merges."""
    rows = _book().journal(limit=limit, rid=rid or None)
    if not rows:
        typer.echo("no decisions yet")
        return
    for row in rows:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row.get("at", 0)))
        rest = {k: v for k, v in row.items() if k not in ("at", "event", "review")}
        typer.echo(f"{stamp}  {row.get('review', '')}  {row['event']:<10} {json.dumps(rest, ensure_ascii=False)}")

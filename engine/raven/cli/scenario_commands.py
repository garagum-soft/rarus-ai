"""``raven scenario``: list, write and run scripts that drive agents, and read what their runs did."""

from __future__ import annotations

import json
from pathlib import Path

import typer

scenario_app = typer.Typer(
    name="scenario", help="Scripts that run agents, send them messages and wait for what happens."
)

_TEMPLATE = '''"""{summary}

Run with: raven scenario run {name} --arg task="..."
"""

# Parameters the run takes: name -> default, None for one that must be given.
PARAMS = {{"task": None, "agent": "Raven"}}


async def main(sc):
    # sc.run(agent, task) runs one task and waits; sc.start(...) returns a handle to
    # await later, so several agents work at once (sc.wait / sc.first collect them).
    # Runs and this script share mail: await sc.send(name, text), await sc.receive(...).
    reply = await sc.run(sc.args["agent"], sc.args["task"], name="worker")
    if not reply.ok:
        raise RuntimeError(reply.error)
    sc.log(f"worker answered in {{reply.seconds:.1f}}s")
    return reply.text
'''


def _workspace(workspace: Path | None) -> Path:
    return (workspace or Path.cwd()).expanduser().resolve()


def _spec(name: str, workspace: Path):
    from raven.scenario.catalog import discover

    found = discover(workspace)
    if name not in found:
        typer.echo(f"no scenario {name!r}; known: {', '.join(found) or 'none'}", err=True)
        raise typer.Exit(2)
    return found[name]


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
        provider = None  # only Raven's own agents need one; an external agent brings its own
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


@scenario_app.command("list")
def list_cmd(
    workspace: Path = typer.Option(None, "--workspace", "-w", help="Also list this project's .raven/scenarios"),
) -> None:
    """Every scenario, where it comes from, and what it takes."""
    from raven.scenario.catalog import discover

    found = discover(_workspace(workspace))
    if not found:
        typer.echo("no scenarios")
        return
    for spec in found.values():
        params = " ".join(f"{k}{'' if v is not None else '*'}" for k, v in spec.params.items())
        flag = f"  [cannot run: {spec.problem}]" if spec.problem else ""
        typer.echo(f"{spec.name:<18} {spec.source:<9} {spec.summary}{flag}")
        if params:
            typer.echo(f"{'':<28} params: {params}  (* required)")


@scenario_app.command("show")
def show_cmd(
    name: str = typer.Argument(..., help="The scenario"),
    workspace: Path = typer.Option(None, "--workspace", "-w"),
) -> None:
    """A scenario's description, parameters and file."""
    spec = _spec(name, _workspace(workspace))
    typer.echo(f"{spec.name} ({spec.source}): {spec.path}")
    if spec.doc:
        typer.echo("")
        typer.echo(spec.doc)
    typer.echo("")
    for key, default in spec.params.items():
        typer.echo(f"  {key}: " + ("required" if default is None else f"default {default!r}"))
    if spec.problem:
        typer.echo(f"cannot run: {spec.problem}")


@scenario_app.command("new")
def new_cmd(
    name: str = typer.Argument(..., help="Name for the scenario (letters, digits, '-')"),
    force: bool = typer.Option(False, "--force", help="Replace a scenario of that name in your scenarios"),
) -> None:
    """Write a starting scenario to <RAVEN_HOME>/scenarios/ to edit."""
    import re

    from raven.scenario.catalog import default_home_dir

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", name):
        typer.echo("a scenario name is letters, digits and '-', starting with a letter", err=True)
        raise typer.Exit(2)
    path = default_home_dir() / f"{name.replace('-', '_')}.py"
    if path.exists() and not force:
        typer.echo(f"{path} exists; --force replaces it", err=True)
        raise typer.Exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _TEMPLATE.format(name=name, summary=f"{name}: what this scenario does, in one line."), encoding="utf-8"
    )
    typer.echo(f"wrote {path}")


@scenario_app.command("run")
def run_cmd(
    name: str = typer.Argument(..., help="The scenario"),
    arg: list[str] = typer.Option([], "--arg", "-a", help="key=value, repeatable"),
    workspace: Path = typer.Option(None, "--workspace", "-w", help="Where the runs work (default: here)"),
    timeout: float = typer.Option(0, "--timeout", help="Stop the whole scenario after this many seconds (0: no limit)"),
    as_json: bool = typer.Option(False, "--json", help="Print the outcome as JSON"),
) -> None:
    """Run a scenario to the end and print its result; the record is kept under <RAVEN_HOME>/scenario_runs/."""
    import asyncio

    from raven.scenario.api import ScenarioError
    from raven.scenario.runner import run_scenario

    where = _workspace(workspace)
    spec = _spec(name, where)
    given: dict[str, str] = {}
    for item in arg:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            typer.echo(f"--arg {item!r}: expected key=value", err=True)
            raise typer.Exit(2)
        given[key.strip()] = value
    if spec.source != "builtin":
        typer.echo(f"running {spec.path}", err=True)

    def echo(line: str) -> None:
        if not as_json:
            typer.echo(line, err=True)

    try:
        outcome = asyncio.run(
            run_scenario(spec, given, manager=_manager(), workspace=where, timeout=timeout or None, echo=echo)
        )
    except ScenarioError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "id": outcome.run_id,
                    "name": outcome.name,
                    "status": outcome.status,
                    "result": outcome.result,
                    "error": outcome.error,
                    "seconds": outcome.seconds,
                    "runs": outcome.runs,
                    "record": str(outcome.record),
                },
                ensure_ascii=False,
            )
        )
    else:
        if outcome.result:
            typer.echo(outcome.result)
        typer.echo(f"{outcome.name} {outcome.status} in {outcome.seconds:.1f}s; record: {outcome.record}", err=True)
        if outcome.error:
            typer.echo(outcome.error, err=True)
    if not outcome.ok:
        raise typer.Exit(1)


@scenario_app.command("runs")
def runs_cmd(limit: int = typer.Option(20, "--limit", help="At most this many")) -> None:
    """Recent scenario runs, newest first."""
    import time

    from raven.scenario.journal import recent

    rows = recent(limit=limit)
    if not rows:
        typer.echo("no scenario runs yet")
        return
    for meta in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta.get("started_at", 0)))
        runs = meta.get("runs") or []
        typer.echo(f"{meta.get('id')}  {meta.get('status', '?'):<10} {when}  {len(runs)} agent run(s)")


@scenario_app.command("log")
def log_cmd(run_id: str = typer.Argument(..., help="A run id from `raven scenario runs`")) -> None:
    """What one run did, event by event, and its result."""
    import time

    from raven.scenario.journal import default_runs_dir, events

    rows = events(run_id)
    if not rows:
        typer.echo(f"no scenario run {run_id!r}", err=True)
        raise typer.Exit(1)
    for e in rows:
        stamp = time.strftime("%H:%M:%S", time.localtime(e.get("at", 0)))
        kind = e.get("event", "")
        detail = {k: v for k, v in e.items() if k not in ("at", "event")}
        if kind == "run.end":
            line = f"{detail.get('name')} ({detail.get('agent')}) {'ok' if detail.get('ok') else 'failed'} {detail.get('seconds')}s"
            line += f": {detail.get('error')}" if detail.get("error") else ""
        elif kind == "run.start":
            line = f"{detail.get('name')} ({detail.get('agent')}): {str(detail.get('task', ''))[:160]}"
        elif kind == "log":
            line = str(detail.get("text", ""))
        elif kind in ("mail.in", "mail.out"):
            peer = detail.get("sender") or detail.get("to")
            line = f"{'from' if kind == 'mail.in' else 'to'} {peer}: {str(detail.get('body', ''))[:160]}"
        else:
            line = json.dumps(detail, ensure_ascii=False)[:200]
        typer.echo(f"{stamp} {kind:<15} {line}")
    result = default_runs_dir() / run_id / "result.md"
    if result.is_file() and result.read_text(encoding="utf-8").strip():
        typer.echo("")
        typer.echo(result.read_text(encoding="utf-8"))

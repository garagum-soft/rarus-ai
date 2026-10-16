"""Playbook subcommands — owns the ``playbook_app`` Typer instance.

Bundles all ``raven playbook ...`` subcommands over the two-layer library
(packaged builtins under a writable user layer) and the ``playbooks.disabled``
switch in config:

- ``playbook list``               — both layers, with origin and switch state
- ``playbook get <name>``        — raw playbook.md to stdout, source to stderr
- ``playbook validate <target>`` — check a library name or a file path, no run
- ``playbook create <name>``     — generate into the user layer, disabled
- ``playbook enable <name>``     — remove from the disabled list
- ``playbook disable <name>``    — add to the disabled list (builtins too)
- ``playbook run <name> k=v``    — explicit synchronous run
- ``playbook delete <name>``     — user layer only; builtins can only disable

``commands.py`` imports :data:`playbook_app` and registers it on the
top-level ``app`` via ``app.add_typer(playbook_app, name="playbook")``.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from raven.cli.stint_commands import stint_app

console = Console()
err_console = Console(stderr=True)

playbook_app = typer.Typer(help="Manage and run stored playbooks")


def _load_config():
    from raven.config.loader import load_config

    return load_config()


def _store(config):
    from raven.playbook import PlaybookStore

    user_layer = Path(config.playbooks.dir) if config.playbooks.dir else (config.workspace_path / "playbooks")
    return PlaybookStore(user_layer)


def _require_known(store, name: str) -> str:
    origin = store.origin_of(name)
    if origin is None:
        known = ", ".join(store.list_ids()) or "(none)"
        err_console.print(f"[red]No playbook named {escape(repr(name))}. Known: {escape(known)}[/red]")
        raise typer.Exit(code=1)
    return origin


@playbook_app.command("list")
def playbook_list():
    """List both library layers, with origin and switch state."""
    config = _load_config()
    store = _store(config)
    names = store.list_ids()
    if not names:
        console.print("[dim]No playbooks installed.[/dim]")
        return
    disabled = set(config.playbooks.disabled)
    table = Table(title=f"Playbooks ({len(names)})")
    table.add_column("Name", style="cyan")
    table.add_column("Origin", style="green")
    table.add_column("State")
    table.add_column("Description", overflow="fold")
    for name in names:
        try:
            description = escape(store.load(name).description)
        except Exception as exc:  # noqa: BLE001 - a broken file is a row, not a crash
            description = f"[red]unreadable: {escape(str(exc))}[/red]"
        origin = store.origin_of(name) or "?"
        if store.is_shadowing(name):
            origin = "user (shadows builtin)"
        state = "[yellow]disabled[/yellow]" if name in disabled else "enabled"
        table.add_row(escape(name), origin, state, description)
    console.print(table)


@playbook_app.command("get")
def playbook_get(name: str = typer.Argument(..., help="Playbook name, as listed")):
    """Print the raw playbook.md (redirectable); the source path goes to stderr."""
    config = _load_config()
    store = _store(config)
    origin = _require_known(store, name)
    path = store.path_for(name)
    err_console.print(f"[dim]{origin}: {escape(str(path))}[/dim]")
    typer.echo(path.read_text(encoding="utf-8"), nl=False)


@playbook_app.command("validate")
def playbook_validate(
    target: str = typer.Argument(..., metavar="NAME|PATH", help="Library name, or a path to a playbook.md"),
):
    """Validate without running: spec shape plus the field definition's rules."""
    from pydantic import ValidationError

    from raven.playbook.runtime import validation_errors

    config = _load_config()
    as_path = Path(target)
    errors: list[str] = []
    spec = None
    if as_path.suffix == ".md" or as_path.exists():
        path = as_path if as_path.is_file() else as_path / "playbook.md"
        if not path.is_file():
            err_console.print(f"[red]{escape(target)}: no playbook.md here[/red]")
            raise typer.Exit(code=1)
        # Loading through a store keeps this the same parser the runtime
        # uses; the directory name is the name under validation.
        from raven.playbook import PlaybookStore

        probe = PlaybookStore(path.parent.parent, builtin_root=path.parent.parent)
        try:
            spec = probe.load(path.parent.name)
        except (ValidationError, ValueError) as exc:
            errors.append(str(exc))
    else:
        store = _store(config)
        _require_known(store, target)
        path = store.path_for(target)
        try:
            spec = store.load(target)
        except (ValidationError, ValueError) as exc:
            errors.append(str(exc))

    if spec is not None:
        # Validated against this machine's agent table. A playbook is a
        # distribution unit, so a name missing from the table is a real finding
        # here, reported as "not registered" rather than as a malformed file.
        #
        # The table is never empty -- the package's built-in rows are seeds, not
        # config -- so there is no "could not load a table" case to handle on this
        # path. ``validate_structure`` still accepts ``None`` for a caller that
        # genuinely has no table; this is not one.
        from raven.agent.subagent.registry import AgentRegistry

        registry = AgentRegistry()
        registry.apply(config.subagents.agents)
        errors.extend(validation_errors(spec, list(registry.all_names())))
    if errors:
        text = path.read_text(encoding="utf-8")
        for error in errors:
            # soft_wrap: an error line anchors on a file path, and a wrap in
            # the middle of the path breaks copy-paste and any caller that
            # greps the output (a long tmp path did exactly that in CI).
            console.print(f"[red]{escape(_located(path, text, error))}[/red]", soft_wrap=True)
        raise typer.Exit(code=1)
    console.print(f"[green]OK[/green] {escape(str(path))}", soft_wrap=True)


def _located(path: Path, text: str, error: str) -> str:
    """Best-effort line anchor: the first line mentioning the failing field.

    Precise positions would need a location-preserving YAML parser; anchoring
    on the field name is right whenever the field appears once, and degrades
    to the bare path rather than to a wrong number when it does not.
    """
    field = error.split(":", 1)[0].split(".")[-1].split("[")[0].strip()
    hits = [i for i, line in enumerate(text.splitlines(), start=1) if line.lstrip().startswith(f"{field}:")]
    if len(hits) == 1:
        return f"{path}:{hits[0]}: {error}"
    return f"{path}: {error}"


def _skeleton_path() -> Path:
    """The shipped stint skeleton, beside the agent scaffold `agents new` copies."""
    import raven

    return Path(raven.__file__).resolve().parent / "templates" / "stint_skeleton.md"


@playbook_app.command("new-stint")
def playbook_new_stint(
    name: str = typer.Argument(..., help="Name for the new playbook (its directory name)"),
    description: str = typer.Option("", "--description", "-d", help="One line: when to use it"),
):
    """Write a `mode: stint` skeleton into the library for you to fill in.

    The other modes are generated -- `playbook create` hands a description to a
    model and the model writes the spec. A stint is not, and deliberately: its
    `verify` commands are shell that runs on this machine every round, under one
    approval that covers the whole run, so nothing a model writes gets to
    schedule them (`tests/test_playbook_generator.py` holds that line).

    Which left the only way to make one being to copy an existing file and find
    out field by field what this build accepts. This is the cheap half of the
    answer: no model, no network, a shape with every field explained and the
    traps named next to the field that springs them. It validates as written, so
    the first `raven playbook validate` reports what you changed rather than
    where you started.
    """
    import re

    from raven.playbook.types import NAME_RE

    if not re.fullmatch(NAME_RE, name):
        err_console.print(f"[red]Playbook names are kebab-case ({escape(NAME_RE)}); got {escape(repr(name))}.[/red]")
        raise typer.Exit(code=1)
    config = _load_config()
    store = _store(config)
    if (origin := store.origin_of(name)) is not None:
        err_console.print(f"[red]Playbook {escape(repr(name))} already exists ({escape(origin)}).[/red]")
        raise typer.Exit(code=1)

    said = description.strip() or f"what {name} is for -- one line, written for retrieval"
    body = (
        _skeleton_path()
        .read_text(encoding="utf-8")
        .replace("{{name}}", name)
        .replace("{{description}}", said)
        .replace("{{task_summary}}", f"one round of {name}")
    )
    path = Path(store.root) / name / "playbook.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")

    console.print(f"[green]Wrote[/green] {escape(str(path))}")
    console.print(
        "Two roles, three rounds, nothing checked -- a shape, not a plan. Fill in the "
        "promptTemplates and what each role owns, then:"
    )
    console.print(f"  raven playbook validate {escape(name)}")
    console.print(
        "[dim]It is usable as soon as it validates; no enabling step. Read it before you run it -- "
        "one approval covers every round.[/dim]"
    )


@playbook_app.command("create")
def playbook_create(
    name: str = typer.Argument(..., help="Name for the new playbook (its directory name)"),
    input_text: str = typer.Option(None, "--input", "-i", help="Describe the workflow to capture"),
    from_files: list[Path] = typer.Option(None, "--from", help="File(s) whose content describes the workflow"),
):
    """Generate a playbook into the user layer; it starts disabled for review."""
    pieces = [input_text] if input_text else []
    for f in from_files or []:
        pieces.append(f.read_text(encoding="utf-8"))
    if not pieces:
        err_console.print("[red]Nothing to generate from: pass --input and/or --from FILE.[/red]")
        raise typer.Exit(code=1)

    import re

    from raven.playbook.types import NAME_RE

    if not re.fullmatch(NAME_RE, name):
        err_console.print(f"[red]Playbook names are kebab-case ({escape(NAME_RE)}); got {escape(repr(name))}.[/red]")
        raise typer.Exit(code=1)
    config = _load_config()
    store = _store(config)
    if store.origin_of(name) is not None:
        err_console.print(f"[red]Playbook {escape(repr(name))} already exists ({store.origin_of(name)}).[/red]")
        raise typer.Exit(code=1)

    from raven.agent.subagent.registry import AgentRegistry
    from raven.playbook import PlaybookGenerator, agent_profiles_from_registry, live_inventory
    from raven.providers.factory import make_provider

    registry = AgentRegistry()
    registry.apply(config.subagents.agents)
    generator = PlaybookGenerator(
        make_provider(config),
        None,
        lambda: agent_profiles_from_registry(registry),
        # No tool registry is built on this path, so only the mcp half is known;
        # an unknown *tool* was never checked here anyway (``check_assets`` reads
        # skills and mcps).
        live_inventory(config.tools.mcp_servers),
        model=config.playbooks.model,
    )
    generated = asyncio.run(generator.generate("\n\n".join(pieces), dag_only=True))
    from raven.playbook.unified import unified_from_legacy

    spec = unified_from_legacy(generated.spec, name=name)
    path = store.save(spec, notes=generated.notes)
    console.print(f"[green]Created[/green] {escape(str(path))}")
    if generated.notes:
        console.print("Open questions for your review:")
        for note in generated.notes:
            console.print(f"  - {escape(note)}")
    # Not switched off on arrival, matching the tool entry: the deny list is
    # read live now, so writing the name onto it here would make a playbook the
    # user just created immediately invisible to a running agent.
    console.print(f"Usable now. Review the file, and `raven playbook disable {name}` if you want it held back.")


@playbook_app.command("enable")
def playbook_enable(name: str = typer.Argument(..., help="Playbook name, as listed")):
    """Remove a playbook from the disabled list, making it matchable."""
    from raven.config.update import set_playbook_disabled

    config = _load_config()
    _require_known(_store(config), name)
    changed = set_playbook_disabled(name, False)
    console.print(f"{escape(name)}: {'enabled' if changed else 'already enabled'}")


@playbook_app.command("disable")
def playbook_disable(name: str = typer.Argument(..., help="Playbook name, as listed")):
    """Add a playbook to the disabled list (builtins too); it stays explicitly runnable."""
    from raven.config.update import set_playbook_disabled

    config = _load_config()
    _require_known(_store(config), name)
    changed = set_playbook_disabled(name, True)
    console.print(f"{escape(name)}: {'disabled' if changed else 'already disabled'}")


secret_app = typer.Typer(help="The secret params this machine holds for a playbook's carried MCP servers")
playbook_app.add_typer(secret_app, name="secret")


@secret_app.command("set")
def playbook_secret_set(
    name: str = typer.Argument(..., help="Playbook name, as listed"),
    param: str = typer.Argument(..., help="A param the playbook declares with type: secret"),
    value: Optional[str] = typer.Option(
        None,
        "--value",
        help="The value. Omit to be prompted without echo -- a value on the command line is visible in `ps`.",
    ),
):
    """Store one secret param for a playbook on this machine.

    The value lands in the Raven credentials root (RAVEN_HOME) under
    playbooks/<name>/, mode 0600, and is read at load, so a run no longer has to
    be handed it -- and it never enters a conversation or the playbook file.
    """
    from raven.playbook.credentials import set_secret_param

    config = _load_config()
    store = _store(config)
    _require_known(store, name)
    spec = store.load(name)
    declared = spec.params.get(param)
    if declared is None or declared.type != "secret":
        err_console.print(f"[red]{escape(name)} declares no secret param named {escape(param)}.[/red]")
        raise typer.Exit(code=1)
    if value is None:
        value = typer.prompt(f"{param}", hide_input=True)
    set_secret_param(name, param, value)
    console.print(f"stored {escape(param)} for {escape(name)}")


@secret_app.command("clear")
def playbook_secret_clear(
    name: str = typer.Argument(..., help="Playbook name, as listed"),
    param: str = typer.Argument(..., help="The param to forget"),
):
    """Forget one stored secret param."""
    from raven.playbook.credentials import clear_secret_param

    config = _load_config()
    _require_known(_store(config), name)
    clear_secret_param(name, param)
    console.print(f"cleared {escape(param)} for {escape(name)}")


@playbook_app.command("auth")
def playbook_auth(
    name: str = typer.Argument(..., help="Playbook name, as listed"),
    server: str = typer.Argument(..., help="A server the playbook carries with auth: oauth"),
):
    """Run the browser OAuth flow for a server this playbook carries.

    Tokens land under the playbook's own credential scope, never under the
    host's, so a carried server and a host server of one name stay apart.
    """
    from raven.playbook.credentials import credential_scope

    config = _load_config()
    store = _store(config)
    _require_known(store, name)
    spec = store.load(name)
    cfg = (spec.mcp_servers or {}).get(server)
    if cfg is None:
        err_console.print(f"[red]{escape(name)} carries no MCP server named {escape(server)}.[/red]")
        raise typer.Exit(code=1)
    if cfg.auth != "oauth":
        err_console.print(f"[red]{escape(server)} has auth={cfg.auth!r}; only an oauth server is authorized.[/red]")
        raise typer.Exit(code=1)

    async def _run() -> dict:
        from raven.agent.tools.registry import ToolRegistry
        from raven.mcp.manager import MCPConnectionManager

        mgr = MCPConnectionManager(ToolRegistry(), credential_scope=credential_scope(name))
        try:
            return await mgr.connect(server, cfg, interactive=True)
        finally:
            await mgr.aclose()

    console.print(f"authorizing {escape(server)} for {escape(name)} -- your browser will open...")
    snap = asyncio.run(_run())
    if snap.get("state") == "connected":
        console.print(f"[green]authorized[/green] -- {snap.get('tool_count', 0)} tools, tokens saved")
    else:
        err_console.print(
            f"[red]authorization failed[/red] ({snap.get('state')}): {escape(str(snap.get('error') or ''))}"
        )
        raise typer.Exit(code=1)


@playbook_app.command("run")
def playbook_run(
    name: str = typer.Argument(..., help="Playbook name, as listed"),
    params: list[str] = typer.Argument(None, metavar="[K=V ...]", help="Parameter values"),
    fill: list[str] = typer.Option(
        None,
        "--fill",
        metavar="NODE.FIELD=VALUE",
        help="Fill a node field the playbook left blank, e.g. --fill draft.promptTemplate='...'",
    ),
):
    """Run one playbook to completion, in this process, and print the result.

    The explicit entry: it works on disabled playbooks too, because disabling only
    takes a playbook out of what the model is offered and this is the user's own
    hand. Runs synchronously -- for long graphs prefer asking the agent, which
    dispatches in the background.

    ``--fill`` is how this path supplies what a playbook left for a model to
    write. Without it, such a playbook simply cannot run here: there is no model
    in the room to compose the missing prompt, and running a graph with a blank
    step would dispatch a sub-agent with nothing to do. The gap is reported with
    the exact flags to pass instead.
    """
    values: dict[str, str] = {}
    for pair in params or []:
        key, eq, value = pair.partition("=")
        if not eq or not key:
            err_console.print(f"[red]Parameters are K=V pairs, got {escape(repr(pair))}.[/red]")
            raise typer.Exit(code=1)
        values[key] = value

    fills: dict[str, dict[str, str]] = {}
    for pair in fill or []:
        target, eq, value = pair.partition("=")
        node_id, dot, field_name = target.partition(".")
        if not (eq and dot and node_id and field_name):
            err_console.print(f"[red]--fill takes NODE.FIELD=VALUE, got {escape(repr(pair))}.[/red]")
            raise typer.Exit(code=1)
        fills.setdefault(node_id, {})[field_name] = value

    config = _load_config()
    store = _store(config)
    _require_known(store, name)

    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.subagent.manager import SubagentManager
    from raven.playbook import PlaybookExecutor, PlaybookRuntime, agent_profiles_from_registry
    from raven.playbook.credentials import credential_scope, stored_secret_params
    from raven.playbook.mcp import (
        declared_mcp_names,
        playbook_mcp_servers,
        preflight_mcp_source,
        unusable_servers,
    )
    from raven.playbook.params import resolve_params, secret_param_names
    from raven.providers.factory import make_provider
    from raven.providers.pool import ProviderPool

    # Read before the graph tool is built, because the tool's approval channel
    # depends on the playbook's mode (see below). Nothing wired an MCP source on
    # this path, so a node's `mcps` resolved to "not connected on the host"
    # however well the machine was configured. The spec is read here rather than
    # taken from the runtime because the pre-flight has to know what to dial
    # before the graph starts; a file that cannot be parsed is left to the
    # runtime, which reports it below.
    try:
        spec = store.load(name)
    except Exception:  # noqa: BLE001 - the runtime reports an unloadable file
        spec = None

    provider = make_provider(config)
    manager = SubagentManager(
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        sandbox_config=config.tools.sandbox,
        agents=config.subagents.agents,
        provider_pool=ProviderPool(config),
    )
    # The manager's table, so a node here resolves to the same agent it would in
    # a conversation -- built-in rows included, which is what lets the CLI run a
    # playbook at all now that no synthetic per-node backend is built for it.
    dag_tool = SubAgentDagTool(
        workspace=config.workspace_path,
        registry=manager.registry,
        guide_skill_id=None,
        state_for=manager.instance_state,
        model_for=manager.session_model_for,
        # A stint only. Its `verify` is shell from a file, which is why such a
        # playbook must keep `confirm: true`, and a gate nobody is asked at does
        # not hold that line. Every other mode keeps this path's approval as it
        # was -- with no channel wired, the graph runs -- because changing what
        # `confirm` means on the command line for every playbook is its own
        # decision, and a script that runs one unattended would start failing.
        ask=ask_at_the_terminal if spec is not None and spec.mode == "stint" else None,
    )
    executor = PlaybookExecutor(
        dag_tool=dag_tool,
        provider=provider,
        compose_model=config.playbooks.model,
        # The project a multi-round plan works, which is the repository the
        # person is standing in -- not the agent home above, which is where runs
        # and plan files are kept and is nobody's project.
        workspace=Path.cwd(),
        background=False,
        # This path composes a prompt-mode graph itself. In a conversation the
        # caller is a model and gets the guidance to compose from; here there is
        # nobody to hand it to, so without this the CLI would lose the ability to
        # run a prompt-mode playbook at all.
        compose_prompt_mode=True,
    )
    executor.set_agent_profiles(lambda: agent_profiles_from_registry(manager.registry))
    runtime = PlaybookRuntime(
        store=store,
        executor=executor,
        disabled=config.playbooks.disabled,
    )

    async def run_with_mcp():
        if spec is None:
            return await runtime.load(name, values, fills, allow_disabled=True)
        # The same precedence the conversation path applies: a secret this
        # machine stores stands in for one the command line did not supply.
        param_values, _ = resolve_params(spec, {**stored_secret_params(spec), **values})
        secrets = secret_param_names(spec)
        declared = declared_mcp_names(spec, fills)
        async with preflight_mcp_source(
            host_servers=config.tools.mcp_servers,
            playbook_servers=playbook_mcp_servers(spec, param_values),
            declared=declared,
            disabled_tools=lambda: frozenset(config.tools.disabled_tools),
            sandbox_config=config.tools.sandbox,
            workspace=config.workspace_path,
            secret_values=[param_values.get(pname, "") for pname in secrets],
            credential_scope=credential_scope(spec.name) if spec.mcp_servers else None,
        ) as source:
            for server, state in unusable_servers(source, declared or ()):
                hint = ""
                if state == "auth_required":
                    fix = (
                        f"raven playbook auth {name} {server}"
                        if server in spec.mcp_servers
                        else f"raven plugin auth {server}"
                    )
                    hint = f" -- run: {fix}"
                err_console.print(f"[yellow]MCP server {escape(server)}: {escape(state)}{escape(hint)}[/yellow]")
            manager.set_mcp_source(source)
            try:
                plan = await runtime.load(name, values, fills, allow_disabled=True)
                # Inside the pre-flight, not after it: a stint's roles reach the
                # servers it declared, and holding the process open outside this
                # scope would hold it open with the connections already closed.
                # A refused stint opened nothing, and there is nothing to hold for.
                if plan is not None and getattr(spec, "mode", "") == "stint" and executor.rounds.holding():
                    console.print(plan.reply, markup=False, soft_wrap=True)
                    console.print(
                        "[dim]Holding this terminal: a stint's rounds run in this process, and nothing "
                        "else here would advance them. Ctrl-C stops it where it is; "
                        "`raven playbook stints resume` takes it up.[/dim]"
                    )
                    await hold_until_the_stints_end(executor.rounds)
                    held.append(plan)
                return plan
            finally:
                # The source outlives nothing: its connections close with the
                # pre-flight, and a backend still holding it would resolve
                # grants against a manager that has already let go.
                manager.set_mcp_source(None)

    held: list = []
    plan = asyncio.run(run_with_mcp())
    if plan is None:
        err_console.print(f"[red]Playbook {escape(repr(name))} did not load; see the log for the parse error.[/red]")
        raise typer.Exit(code=1)
    if held:
        # Printed before the hold, and a stint that ran to its end is not a
        # failure: returning the load sentinel here told a script a finished
        # stint had not loaded, with exit code 1.
        return
    if plan.kind == "gaps":
        # Named as an unrunnable-here condition rather than a generic failure: the
        # playbook is fine, this entry point just has nobody to fill it in.
        err_console.print("[yellow]This playbook expects values to be supplied at run time.[/yellow]", soft_wrap=True)
    # Payload, not styling: a graph result legitimately contains brackets.
    console.print(plan.reply, markup=False, soft_wrap=True)
    if plan.kind != "dag":
        raise typer.Exit(code=1)


@playbook_app.command("ideate")
def playbook_ideate(
    agent: str = typer.Option("Raven", "--agent", help="The roster agent each category is put to"),
    category: list[str] = typer.Option([], "--category", "-c", help="A category to ideate on; repeatable"),
    project: Path | None = typer.Option(None, "--project", "-p", help="The project (default: here)"),
    out: Path | None = typer.Option(
        None, "--out", help="Where ideas.json and roadmap.md go (default: <project>/.raven/ideation)"
    ),
    events: bool = typer.Option(False, "--events", help="Print every progress event as a JSON line"),
) -> None:
    """Have an agent propose ideas per category, concurrently, and lay a roadmap out from them."""
    from raven.agent.subagent.manager import SubagentManager
    from raven.playbook.ideation import DEFAULT_CATEGORIES, ideate, save
    from raven.providers.auth import MissingCredentialsError
    from raven.providers.factory import make_lazy_provider
    from raven.providers.pool import ProviderPool

    root = (project or Path.cwd()).resolve()
    readme = next((p for p in (root / "README.md", root / "readme.md", root / "README") if p.is_file()), None)
    listing = ", ".join(sorted(p.name for p in root.iterdir() if not p.name.startswith(".")))[:1500]
    brief = (
        readme.read_text(encoding="utf-8", errors="replace")[:4000] if readme else ""
    ) + f"\nTop-level files: {listing}"

    config = _load_config()
    try:
        provider = make_lazy_provider(config)
    except MissingCredentialsError:
        provider = None
    manager = SubagentManager(
        provider=provider,  # type: ignore[arg-type]
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        sandbox_config=config.tools.sandbox,
        agents=config.subagents.agents,
        provider_pool=ProviderPool(config),
    )

    async def ask(name: str, prompt: str) -> str:
        return await manager.run_in(agent, prompt, root)

    def show(event: dict) -> None:
        if events:
            console.print(json.dumps(event, ensure_ascii=False), markup=False, soft_wrap=True)
        else:
            err_console.print(f"{event.get('progress', '')}% {event['event']} {event.get('category', '')}".strip())

    roadmap = asyncio.run(ideate(brief, ask, categories=category or DEFAULT_CATEGORIES, on_event=show))
    ideas_file, page = save(roadmap, out or root / ".raven" / "ideation")
    console.print(f"{len(roadmap.ideas)} ideas; roadmap: {page}", markup=False, soft_wrap=True)
    if not roadmap.ideas:
        raise typer.Exit(code=1)


@playbook_app.command("decompose")
def playbook_decompose(
    goal: str = typer.Argument(..., help="The large task to split"),
    agent: str = typer.Option("Raven", "--agent", help="The roster agent that plans each split"),
    reviewer: str = typer.Option("", "--reviewer", help="A roster agent that reviews the plan (none: no review)"),
    review_rounds: int = typer.Option(1, "--review-rounds", help="How many times the reviewer may send it back"),
    depth: int = typer.Option(3, "--depth", help="Levels of splitting at most"),
    max_children: int = typer.Option(6, "--max-children", help="Subtasks per task at most"),
    max_leaves: int = typer.Option(24, "--max-leaves", help="Leaves in the whole plan at most"),
    worker: str = typer.Option("Raven", "--worker", help="The agent a leaf runs on when the planner named none"),
    project: Path | None = typer.Option(None, "--project", "-p", help="The project (default: here)"),
    dag: bool = typer.Option(False, "--dag", help="Print the leaves as run_subagent_dag nodes (JSON)"),
    events: bool = typer.Option(False, "--events", help="Print every progress event as a JSON line"),
) -> None:
    """Split a large task, recursively, into a tree of tasks one agent can each do; save it and print it."""
    from raven.agent.subagent.manager import SubagentManager
    from raven.playbook.decompose import decompose, plan_dir, save, to_dag_nodes
    from raven.providers.auth import MissingCredentialsError
    from raven.providers.factory import make_lazy_provider
    from raven.providers.pool import ProviderPool

    root = (project or Path.cwd()).resolve()
    config = _load_config()
    try:
        provider = make_lazy_provider(config)
    except MissingCredentialsError:
        provider = None
    manager = SubagentManager(
        provider=provider,  # type: ignore[arg-type]
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        sandbox_config=config.tools.sandbox,
        agents=config.subagents.agents,
        provider_pool=ProviderPool(config),
    )

    async def ask(role: str, prompt: str) -> str:
        return await manager.run_in(reviewer if role == "reviewer" and reviewer else agent, prompt, root)

    def show(event: dict) -> None:
        if events:
            console.print(json.dumps(event, ensure_ascii=False), markup=False, soft_wrap=True)
        elif event["event"] == "split":
            err_console.print(f"split {event['task']}: {len(event['children']) or event['leaf']}", soft_wrap=True)

    try:
        tree = asyncio.run(
            decompose(
                goal,
                ask,
                max_depth=depth,
                max_children=max_children,
                max_leaves=max_leaves,
                agents=manager.registry.names(),
                review_rounds=review_rounds if reviewer else 0,
                on_event=show,
            )
        )
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]", soft_wrap=True)
        raise typer.Exit(code=2) from None
    data, page = save(tree, plan_dir(root, tree.id))
    if dag:
        # Plain echo: rich wraps long lines, which would break the JSON for whoever reads it.
        typer.echo(json.dumps(to_dag_nodes(tree, agent=worker), ensure_ascii=False, indent=2))
    else:
        console.print(tree.outline(), markup=False, soft_wrap=True)
    err_console.print(f"plan saved: {data}", soft_wrap=True)
    if len(tree.leaves()) < 2:
        raise typer.Exit(code=1)


@playbook_app.command("batch")
def playbook_batch(
    name: str = typer.Argument(..., help="Playbook name, as listed"),
    inputs: Path = typer.Option(
        ..., "--inputs", help="A JSON array or JSON-lines file of parameter objects, one per run"
    ),
    concurrency: int = typer.Option(2, "--concurrency", "-j", min=1, max=16, help="Runs at once"),
    events: bool = typer.Option(False, "--events", help="Print every progress event as a JSON line"),
) -> None:
    """Run one playbook once per input, concurrently, with progress events and a result per input.

    Each input is the parameter set one `playbook run` would take. Playbooks
    that bring MCP servers of their own still need `playbook run`, which
    connects them first.
    """
    import asyncio

    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.subagent.manager import SubagentManager
    from raven.playbook import PlaybookExecutor, PlaybookRuntime, agent_profiles_from_registry
    from raven.playbook.batch import run_batch
    from raven.providers.auth import MissingCredentialsError
    from raven.providers.factory import make_lazy_provider
    from raven.providers.pool import ProviderPool

    text = inputs.read_text(encoding="utf-8").strip()
    rows = (
        json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
    )
    if not rows or not all(isinstance(row, dict) for row in rows):
        err_console.print("[red]--inputs must hold one or more JSON objects of parameter values.[/red]")
        raise typer.Exit(code=1)

    config = _load_config()
    store = _store(config)
    _require_known(store, name)
    try:
        provider = make_lazy_provider(config)
    except MissingCredentialsError:
        provider = None
    manager = SubagentManager(
        provider=provider,  # type: ignore[arg-type]
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        sandbox_config=config.tools.sandbox,
        agents=config.subagents.agents,
        provider_pool=ProviderPool(config),
    )
    dag_tool = SubAgentDagTool(
        workspace=config.workspace_path,
        registry=manager.registry,
        guide_skill_id=None,
        state_for=manager.instance_state,
        model_for=manager.session_model_for,
    )
    executor = PlaybookExecutor(
        dag_tool=dag_tool,
        provider=provider,
        compose_model=config.playbooks.model,
        workspace=Path.cwd(),
        background=False,
        compose_prompt_mode=True,
    )
    executor.set_agent_profiles(lambda: agent_profiles_from_registry(manager.registry))
    runtime = PlaybookRuntime(store=store, executor=executor, disabled=config.playbooks.disabled)

    async def run_one(index: int, values: dict) -> str:
        plan = await runtime.load(name, {k: str(v) for k, v in values.items()}, {}, allow_disabled=True)
        reply = str(getattr(plan, "reply", "") or "")
        if plan is None or plan.kind != "dag" or reply.startswith("Error"):
            raise RuntimeError(reply or "the playbook did not run")
        return reply

    def show(event: dict) -> None:
        if events:
            console.print(json.dumps(event, ensure_ascii=False, default=str), markup=False, soft_wrap=True)
        elif event["event"] == "progress":
            err_console.print(f"{event['done']}/{event['total']} done")

    items = asyncio.run(run_batch(rows, run_one, max_concurrency=concurrency, on_event=show))
    if not events:
        for item in items:
            state = "ok" if item.ok else f"failed: {item.error}"
            console.print(
                f"[{item.index}] {json.dumps(item.input, ensure_ascii=False)} -> {state}", markup=False, soft_wrap=True
            )
    if not all(item.ok for item in items):
        raise typer.Exit(code=1)


@playbook_app.command("best-of")
def playbook_best_of(
    task: str = typer.Argument(..., help="The task every attempt is given"),
    agent: str = typer.Option(..., "--agent", help="The roster agent that makes each attempt"),
    attempts: int = typer.Option(3, "--attempts", "-n", min=1, max=10, help="How many attempts"),
    check: list[str] = typer.Option([], "--check", help="A command an attempt must pass; repeatable"),
    project: Path | None = typer.Option(None, "--project", "-p", help="The repository (default: here)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Measure the attempts but apply none"),
) -> None:
    """Try one task N times in N checkouts, measure each with the checks, and apply the best to the base."""
    import asyncio
    import uuid

    from raven.agent.subagent.manager import SubagentManager
    from raven.config.loader import load_config
    from raven.home import raven_home
    from raven.providers.auth import MissingCredentialsError
    from raven.providers.factory import make_lazy_provider
    from raven.providers.pool import ProviderPool
    from raven.stint.best_of import BestOfError, best_of
    from raven.stint.verify import CheckSpec

    config = load_config()
    try:
        provider = make_lazy_provider(config)
    except MissingCredentialsError:
        # Only Raven's own in-process agents need a model; an external agent
        # (cli, acp, openai) brings its own, so best-of still runs on one.
        provider = None
    manager = SubagentManager(
        provider=provider,  # type: ignore[arg-type]
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        sandbox_config=config.tools.sandbox,
        agents=config.subagents.agents,
        provider_pool=ProviderPool(config),
    )
    specs = [CheckSpec(name=f"check-{i}", command=cmd, timeout_sec=600) for i, cmd in enumerate(check, 1)]

    async def attempt(index: int, checkout: Path) -> str:
        return await manager.run_in(agent, task, checkout)

    try:
        result = asyncio.run(
            best_of(
                (project or Path.cwd()).resolve(),
                attempts,
                attempt,
                checks=specs,
                root=raven_home() / "best-of",
                tag=uuid.uuid4().hex[:8],
                apply=not dry_run,
            )
        )
    except BestOfError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(result.report(), markup=False, soft_wrap=True)
    if result.winner is None:
        raise typer.Exit(code=1)


@playbook_app.command("fast")
def playbook_fast(
    task: str = typer.Argument(..., help="The task, as the agent should read it"),
    agent: str = typer.Option(
        "Raven", "--agent", help="The roster agent that does it; 'auto' picks by the capability matrix"
    ),
    check: list[str] = typer.Option([], "--check", help="A command the result must pass; repeatable"),
    attempts: int = typer.Option(2, "--attempts", "-n", min=1, max=5, help="Attempts when a check fails"),
    timeout: float = typer.Option(300.0, "--timeout", min=1, help="Seconds per agent attempt and per check"),
    project: Path | None = typer.Option(None, "--project", "-p", help="Where to work (default: here)"),
    events: bool = typer.Option(False, "--events", help="Print progress as JSON lines"),
) -> None:
    """Fast mode for a small task: no playbook is planned; one agent does it, the checks decide, a failure retries."""
    import asyncio
    import uuid

    from raven.agent.subagent.manager import SubagentManager
    from raven.config.loader import load_config
    from raven.home import raven_home
    from raven.playbook.fast import run_fast, save
    from raven.providers.auth import MissingCredentialsError
    from raven.providers.factory import make_lazy_provider
    from raven.providers.pool import ProviderPool
    from raven.stint.verify import CheckSpec

    config = load_config()
    try:
        provider = make_lazy_provider(config)
    except MissingCredentialsError:
        # Only Raven's own in-process agents need a model; an external agent
        # brings its own, so fast mode still runs on one.
        provider = None
    manager = SubagentManager(
        provider=provider,  # type: ignore[arg-type]
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        sandbox_config=config.tools.sandbox,
        agents=config.subagents.agents,
        provider_pool=ProviderPool(config),
    )
    specs = [CheckSpec(name=f"check-{i}", command=cmd, timeout_sec=timeout) for i, cmd in enumerate(check, 1)]
    from raven.agent.subagent.capability_matrix import CapabilityMatrix, choose, record_run

    matrix = CapabilityMatrix()
    if agent == "auto":
        enabled = [m.name for m in manager.list_agents() if getattr(m, "enabled", True)]
        picked = choose(task, enabled, matrix)
        if picked is None:
            err_console.print("[red]No probed agent to pick from: run `raven agents probe` first.[/red]")
            raise typer.Exit(code=1)
        agent = picked.agent
        console.print(f"agent: {agent} (picked by the capability matrix: {picked.why})", markup=False, soft_wrap=True)

    async def do(brief: str, where: Path) -> str:
        return await manager.run_in(agent, brief, where)

    def emit(event: dict) -> None:
        if events:
            console.print(json.dumps(event, ensure_ascii=False), markup=False, soft_wrap=True)

    result = asyncio.run(
        run_fast(
            task,
            (project or Path.cwd()).resolve(),
            do,
            checks=specs,
            attempts=attempts,
            step_timeout_s=timeout,
            on_event=emit,
        )
    )
    path = save(result, raven_home() / "fast", uuid.uuid4().hex[:8])
    if specs:
        # A checked run is evidence for the matrix; an unchecked one only says the agent answered.
        elapsed_ms = int(sum(s.seconds for s in result.steps) * 1000)
        record_run(agent, task, result.passed, elapsed_ms, matrix, note=f"fast mode, {result.attempts} attempt(s)")
    console.print(result.report(), markup=False, soft_wrap=True)
    console.print(f"record: {path}", markup=False, soft_wrap=True)
    if not result.passed:
        raise typer.Exit(code=1)


@playbook_app.command("verify-receipt")
def playbook_verify_receipt(
    run_dir: Path = typer.Argument(..., help="The run's directory (the one holding receipt.json)"),
    root: str = typer.Option("", "--root", help="The published root to hold the receipt to (sha256:...)"),
    nodes: Path | None = typer.Option(None, "--nodes", help="The node artifact directory (default: beside the runs)"),
) -> None:
    """Recompute a run's receipt from its files and say whether they are the ones the run reported."""
    from raven.agent.subagent.receipts import verify_receipt

    nodes_root = nodes or run_dir.parent.parent / "nodes"
    verdict = verify_receipt(run_dir, nodes_root, expected_root=root or None)
    if verdict.ok:
        console.print(f"verified: {verdict.root}", markup=False, soft_wrap=True)
        return
    for problem in verdict.problems:
        err_console.print(f"- {problem}", markup=False, soft_wrap=True)
    err_console.print("[red]the run's files do not match its receipt[/red]")
    raise typer.Exit(code=1)


@playbook_app.command("delete")
def playbook_delete(
    name: str = typer.Argument(..., help="Playbook name, as listed"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirm prompt"),
):
    """Delete a user playbook. Builtins cannot be deleted — disable them instead."""
    from raven.config.update import set_playbook_disabled

    config = _load_config()
    store = _store(config)
    origin = _require_known(store, name)
    if origin == "builtin":
        err_console.print(
            f"[red]{escape(repr(name))} is a builtin and cannot be deleted. "
            f"Use: raven playbook disable {escape(name)}[/red]"
        )
        raise typer.Exit(code=1)
    directory = store.path_for(name).parent
    if not yes and not typer.confirm(f"Delete {directory}?"):
        raise typer.Exit(code=1)
    shutil.rmtree(directory)
    # A gone name has no business on the deny list; dropping it also means a
    # future playbook reusing the name starts enabled, like any new one.
    set_playbook_disabled(name, False)
    if store.origin_of(name) == "builtin":
        console.print(f"Deleted the user playbook {escape(repr(name))}; the builtin of the same name is visible again.")
    else:
        console.print(f"Deleted {escape(repr(name))}.")


stints_app = typer.Typer(help="Multi-round runs a `mode: stint` playbook started")
playbook_app.add_typer(stints_app, name="stints")

# The project side of the same feature, under the singular: `stints` answers
# "what has run on this machine", and `stint` acts on one project's own layout
# and the backlog its roles share. One family, because a person who found
# `raven playbook run` has no reason to guess at a second top-level noun.
playbook_app.add_typer(stint_app, name="stint")


def _stint_homes(config):
    """Every conversation on this machine, as (its directory, its stint store).

    A stint is kept beside the conversation that started it, and the person
    asking here has a terminal rather than a conversation. Reading only the
    keyless session's store is what made a stint started in the TUI invisible to
    the commands that exist to watch it -- and invisible in the way that reads
    as "nothing has run", not as "you are looking in the wrong place".

    The directory comes back beside the store because deriving it from the
    session key is the thing that does not work here. A conversation launched in
    a project is grouped under that project, and this process has no project, so
    the derivation lands in a directory nothing has ever written to. Searching
    finds the stint; whoever then opens a round is handed the directory rather
    than deriving it again and writing the round somewhere else.

    Still only files: a stint is read far more often than it is run -- what round
    is it on, what did it undo, what is it waiting for -- and constructing the
    sub-agent stack to answer would make `stint list` cost what a dispatch costs.
    """
    from raven.agent.subagent.history import dag_root
    from raven.session.manager import SessionManager
    from raven.stint.record import STINTS_DIRNAME, StintStore

    sessions = SessionManager(config.workspace_path).sessions_dir
    found = []
    for home in sorted(sessions.glob("*/*")):
        root = dag_root(home) / STINTS_DIRNAME
        if home.is_dir() and root.is_dir():
            found.append((home, StintStore(root)))
    return found


def _stint_stores(config):
    """Every stint store on this machine, for a caller with no use for the rest."""
    return [store for _home, store in _stint_homes(config)]


def _all_stints(config):
    """Every stint on the machine, newest first, across conversations.

    Stints whose holder has gone quiet are marked on the way past. A record is
    the only claim a stint makes about itself and a host that died mid-round
    writes nothing on its way out, so without this the list reports a corpse as
    work in progress -- and a person waits for a notification nobody will send.
    Marking only: taking one up again is `stints resume`, which is a person's
    call because it spends money and hours.
    """
    from raven.stint.record import mark_adrift

    stores = _stint_stores(config)
    mark_adrift(stores)
    found = [record for store in stores for record in store.list()]
    found.sort(key=lambda record: (record.started_at_ms, record.stint_id), reverse=True)
    return found


def _require_stint(config, stint_id: str):
    """The stint, the store holding it, and the conversation directory both are in.

    The store so a writer writes back where it read; the directory so a verb
    that opens a round puts the round where the stint's earlier ones are.
    """
    from raven.stint.record import mark_adrift

    if not re.fullmatch(r"[A-Za-z0-9_-]+", stint_id):
        err_console.print(f"[red]{escape(stint_id)} is not a stint id; see `raven playbook stints list`.[/red]")
        raise typer.Exit(code=1)
    homes = _stint_homes(config)
    mark_adrift([store for _home, store in homes])
    for home, store in homes:
        record = store.read(stint_id)
        if record is not None:
            return home, store, record
    err_console.print(f"[red]No stint {escape(stint_id)} on this machine.[/red]")
    raise typer.Exit(code=1)


@stints_app.command("list")
def plan_list():
    """Every stint this machine has started, newest first."""
    records = _all_stints(_load_config())
    if not records:
        console.print("[dim]No stints have been started here.[/dim]")
        return
    table = Table(title=f"Stints ({len(records)})")
    # Folded, never truncated: the id is what every other verb takes, and a
    # reader who was shown `stint-20260920T08584423470...` has to go and find
    # the file to learn the rest.
    table.add_column("Stint", style="cyan", overflow="fold", no_wrap=False)
    table.add_column("Playbook", style="green")
    table.add_column("Round")
    table.add_column("State")
    table.add_column("Why it stopped", overflow="fold")
    for record in records:
        # The status, not `live`. `live` answers "should something be advancing
        # this", which is true of an interrupted stint too -- printing it as
        # `running` is the lie this column exists to stop telling.
        state = f"[yellow]{record.status}[/yellow]" if record.live else record.status
        table.add_row(
            escape(record.stint_id),
            escape(record.playbook),
            str(record.round_index),
            state,
            escape(record.stop_reason or ""),
        )
    console.print(table)


@stints_app.command("get")
def plan_get(stint_id: str = typer.Argument(..., help="Stint id, as listed")):
    """One stint in full: its rounds, what was undone, and what it is waiting on."""
    *_, record = _require_stint(_load_config(), stint_id)
    console.print(f"[cyan]{escape(record.stint_id)}[/cyan]  {escape(record.playbook)}  [{escape(record.status)}]")
    console.print(f"working in {escape(record.workdir)}" + (f" on {escape(record.branch)}" if record.branch else ""))
    if record.stop_reason:
        console.print(f"stopped because {escape(record.stop_reason)}")
    rounds = Table(title="Rounds")
    rounds.add_column("N")
    rounds.add_column("Run", style="dim")
    rounds.add_column("State")
    rounds.add_column("Checks")
    rounds.add_column("Undone")
    for entry in record.rounds:
        checks = ", ".join(f"{row.get('name')}={row.get('status')}" for row in entry.verify) or "-"
        rounds.add_row(
            str(entry.index),
            escape(entry.run_id or "-"),
            escape(entry.status),
            escape(checks),
            str(len(entry.violations)),
        )
    console.print(rounds)
    for entry in record.rounds:
        for note in entry.violations:
            console.print(f"[yellow]round {entry.index}: {escape(note)}[/yellow]")
    for position, question in enumerate(record.questions):
        answered = str(question.get("answer") or "").strip()
        mark = "[green]answered[/green]" if answered else "[red]waiting[/red]"
        console.print(f"{position}. {mark} round {question.get('round')} {escape(str(question.get('text')))}")
        if answered:
            console.print(f"   -> {escape(answered)}")


@stints_app.command("stop")
def plan_stop(
    stint_id: str = typer.Argument(..., help="Stint id, as listed"),
    now: bool = typer.Option(False, "--now", help="Cut the round in flight short instead of letting it finish"),
):
    """Open no further rounds. A round already running finishes first, unless --now."""
    from raven.stint.record import STOPPED

    _, store, record = _require_stint(_load_config(), stint_id)
    if not record.unfinished:
        console.print(f"{escape(stint_id)} is already {escape(record.status)}.")
        return
    # Relayed before the file is written, because only the raven running the
    # round can cut it short: the write is what that side does either way, and
    # doing it twice would be the same write.
    if now and _relayed("playbooks.stints.stop", {"stint_id": stint_id, "now": True}):
        console.print(f"Stopped {escape(stint_id)}, and asked the round in flight to stop where it is.")
        return
    record.status = STOPPED
    record.stop_reason = "a person stopped the stint"
    store.write(record)
    if now:
        console.print(
            f"Stopped {escape(stint_id)}. No raven is serving a page here, so the round in flight could not "
            f"be reached: it is held by the terminal that opened it, and Ctrl-C there stops it where it is."
        )
        return
    console.print(
        f"Stopped {escape(stint_id)}. A round already in flight finishes and reports; no further round opens."
    )


@stints_app.command("pause")
def plan_pause(stint_id: str = typer.Argument(..., help="Stint id, as listed")):
    """Open no further rounds, and keep the stint so `resume` can take it up."""
    from raven.stint.record import PAUSED

    _, store, record = _require_stint(_load_config(), stint_id)
    if not record.unfinished:
        console.print(f"{escape(stint_id)} is already {escape(record.status)}.")
        return
    record.status = PAUSED
    record.stop_reason = "a person paused the stint"
    store.write(record)
    console.print(
        f"Paused {escape(stint_id)}. A round already in flight finishes; no further round opens. "
        f"Take it up again with `raven playbook stints resume {escape(stint_id)}`."
    )


@stints_app.command("answer")
def plan_answer(
    stint_id: str = typer.Argument(..., help="Stint id, as listed"),
    question: int = typer.Option(..., "--question", "-q", help="Which question, by its number in `stint get`"),
    text: str = typer.Option(..., "--text", "-t", help="The answer, as the next round should read it"),
):
    """Answer a question a round left. It reaches the round after this one."""
    _, store, record = _require_stint(_load_config(), stint_id)
    if not 0 <= question < len(record.questions):
        err_console.print(f"[red]{escape(stint_id)} has no question {question}; see `playbook stint get`.[/red]")
        raise typer.Exit(code=1)
    record.questions[question]["answer"] = text
    record.questions[question]["answered_at"] = int(time.time() * 1000)
    store.write(record)
    console.print(f"Answered. The next round of {escape(stint_id)} reads it.")


def _stint_driver(config, home: Path):
    """The rounds driver, with every root pointed at the conversation `home`.

    A stint verb that opens a round needs the whole sub-agent stack behind it and
    not just the stint file, because a round is an ordinary graph and goes out
    through the ordinary entry.

    ``session_dir`` is injected rather than left to the tool's own derivation.
    The tool derives a conversation's directory from its key through a slug-less
    session manager, which is the gateway's grouping and not the one a
    conversation launched in a project has -- so from a terminal the stint file,
    the round's runs and its node artifacts would each be looked for under
    ``sessions/<channel>/``, where nothing has ever written. `_stint_homes`
    searched and found the real one; this is that answer, handed over rather
    than derived a second time.
    """
    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.subagent.manager import SubagentManager
    from raven.playbook.executor import PlaybookExecutor
    from raven.providers.factory import make_provider
    from raven.providers.pool import ProviderPool

    manager = SubagentManager(
        provider=make_provider(config),
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        sandbox_config=config.tools.sandbox,
        agents=config.subagents.agents,
        provider_pool=ProviderPool(config),
    )
    dag_tool = SubAgentDagTool(
        workspace=config.workspace_path,
        registry=manager.registry,
        guide_skill_id=None,
        state_for=manager.instance_state,
        session_dir=lambda _key: home,
        ask=ask_at_the_terminal,
    )
    driver = PlaybookExecutor(dag_tool=dag_tool, workspace=Path.cwd()).rounds
    if driver is None:
        err_console.print("[red]This build has no multi-round driver.[/red]")
        raise typer.Exit(code=1)
    return driver


async def hold_until_the_stints_end(driver, *, every_sec: float = 2.0) -> None:
    """Keep this process alive while a stint it just started still has rounds.

    A stint dispatches each round in the background and opens the next one from
    a callback on the finished run's own task. On a gateway that is somebody
    else's problem -- the host outlives the turn. Here the command *is* the
    host, and returning from ``asyncio.run`` closes the loop out from under the
    round: the receipt says it started, the record says ``running``, and nothing
    ever ran. Held here rather than dispatched in the foreground because the
    hand-over that opens round two only exists on the background path.

    Ctrl-C leaves the stint where it is. The round in flight dies with this
    process and the record stops being touched, so the next reader marks it
    interrupted and `stints resume` takes it up from the node it reached.

    Asked of the driver, not of the records. A record is `live` whenever *some*
    process holds it, and after a refusal -- `resume` of a stint another raven
    is beating for -- that process is not this one: holding on it printed the
    refusal and then never came back, while the banner told the person to run
    the command it was blocking. An interrupted record never stops being live
    at all.
    """
    import asyncio as _asyncio

    while True:
        await _asyncio.sleep(every_sec)
        if not driver.holding():
            return


async def ask_at_the_terminal(_conversation: str, question: str) -> bool:
    """The graph tool's approval gate, for a run whose person is at this terminal.

    A stint is thirty rounds of shell from a file somebody handed over, and the
    gate is what stands between that file and running. Built with no ``ask``,
    the tool took the no-channel branch and approved on the person's behalf --
    without so much as rendering the text that names the round budget and every
    command. From a terminal the person is right here, so they are asked; with
    no terminal to ask at, the answer is no, said out loud.
    """
    if not sys.stdin.isatty():
        err_console.print(
            "[red]This run asks for approval and there is no terminal to ask at, so it was not run. "
            "Run it from an interactive terminal, or start it from a conversation.[/red]"
        )
        return False
    console.print(question, markup=False, soft_wrap=True)
    return await asyncio.to_thread(typer.confirm, "Run it?", default=False)


def _relayed(method: str, params: dict) -> bool:
    """Send a round-opening verb to the raven serving the page, if one is up.

    A round runs in the process that opens it. Opened here, it would run in this
    terminal, report to nobody, and end when the terminal does -- and when the
    terminal is a model's shell tool, that is the tool's timeout. The page's
    raven has the driver, the conversation the stint was started in, and the
    announcer that conversation reads, so the verb goes there when it can.
    False means no page is being served and the verb runs here as before.
    """
    from raven.cli._hosted_rpc import call, hosted_page

    hosted = hosted_page()
    if hosted is None:
        return False
    port, token = hosted
    console.print(f"[dim]Sent to the raven serving the page on port {port}; the round runs there.[/dim]")
    try:
        result = call(port, token, method, params)
    except RuntimeError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc
    reply = str((result or {}).get("reply") or "").strip()
    if reply:
        console.print(escape(reply))
    return True


def _held_here(driver, awaited: "Awaitable[str]") -> str:
    """Say why the terminal is not coming back yet, then hold it."""
    import asyncio as _asyncio

    async def go() -> str:
        text = await awaited
        if driver.holding():
            console.print(text, markup=False, soft_wrap=True)
            console.print(
                "[dim]Holding this terminal: a stint's rounds run in this process, and nothing else "
                "here would advance them. Ctrl-C stops it where it is; `stints resume` takes it up.[/dim]"
            )
            await hold_until_the_stints_end(driver)
            return ""
        return text

    return _asyncio.run(go())


def _stint_session(record) -> str | None:
    """The session the stint was started in, not this terminal's absence of one.

    The driver reads the stint from that conversation's store and submits into
    the same one, where the nodes its earlier rounds left still are.
    """
    return str(record.origin.get("session_key") or "") or None


@stints_app.command("extend")
def plan_extend(
    stint_id: str = typer.Argument(..., help="Stint id, as listed"),
    rounds: int = typer.Option(..., "--rounds", "-n", help="How many more rounds to allow"),
):
    """Give a stint more rounds. A stint that is over opens the next one here.

    For the stint that ran its budget out with work still worth doing. It keeps
    the checkout, the branch and the journal it already has -- starting a second
    stint instead would cut a fresh worktree from the project's HEAD and begin
    again from before this one's first commit.
    """

    config = _load_config()
    home, _, record = _require_stint(config, stint_id)
    if _relayed("playbooks.stints.extend", {"stint_id": stint_id, "rounds": rounds}):
        return
    driver = _stint_driver(config, home)
    answer = _held_here(driver, driver.extend(stint_id, rounds, _stint_session(record)))
    if answer:
        console.print(escape(answer))


@stints_app.command("sweep")
def stint_sweep():
    """Take up every stint here whose holder is gone.

    The one move for "the machine restarted and I want my work back". Reading a
    list marks them; this is the step that spends money, which is why it is a
    verb rather than something a read does on a person's behalf. A stint another
    process is still beating for is left where it is.
    """
    config = _load_config()
    taken: list[str] = []

    async def go() -> None:
        # One loop for every home: the rounds this takes up run in this process,
        # and a loop closed per home closed under the rounds it had just opened
        # -- the receipt said "took up", the record said running, nothing ran.
        drivers = [_stint_driver(config, home) for home, _store in _stint_homes(config)]
        for driver in drivers:
            taken.extend(await driver.sweep(None))
        for stint_id in taken:
            console.print(f"[green]took up[/green] {escape(stint_id)}")
        if any(driver.holding() for driver in drivers):
            console.print(
                "[dim]Holding this terminal: the rounds taken up run in this process. "
                "Ctrl-C stops them where they are; `stints resume` takes them up.[/dim]"
            )
            while any(driver.holding() for driver in drivers):
                await asyncio.sleep(2.0)

    asyncio.run(go())
    if not taken:
        console.print("[dim]Nothing here was left in flight.[/dim]")


@stints_app.command("resume")
def plan_resume(stint_id: str = typer.Argument(..., help="Stint id, as listed")):
    """Take a stint up again from the node it stopped at.

    For the stint whose gateway died mid-round. The roles that finished are named
    rather than re-run, so what they produced is still what the rest of the
    round reads.
    """

    config = _load_config()
    home, _, record = _require_stint(config, stint_id)
    # `unfinished` rather than `live`, which excludes a paused stint: pausing
    # says to take it up later with this command, and a guard that then refused
    # it would make that instruction false.
    if not record.unfinished:
        console.print(f"{escape(stint_id)} is {escape(record.status)} and has nothing left to take up.")
        return
    if _relayed("playbooks.stints.resume", {"stint_id": stint_id}):
        return
    driver = _stint_driver(config, home)
    answer = _held_here(driver, driver.resume(stint_id, _stint_session(record)))
    if answer:
        console.print(escape(answer))

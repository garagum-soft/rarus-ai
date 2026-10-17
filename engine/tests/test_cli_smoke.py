"""Smoke tests covering every CLI command surface.

This file's job is to catch *crash-class* regressions (``NameError`` /
``AttributeError`` / ``ImportError``) that slip through the more focused
per-command tests. It walks every top-level command + every subcommand
group's ``--help`` and asserts:

1. exit code 0
2. no ``Traceback`` printed
3. ``r.exception`` is None (typer.testing.CliRunner captures crashes here)

It also catches the historical regression where ``agent_commands.py`` was
missing the ``sync_workspace_templates`` import after the CLI modularize
refactor — that bug would have been caught here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.config.loader import set_config_path

runner = CliRunner()


def _registered_command_names() -> set[str]:
    """All top-level command / subcommand-group names on the root app."""
    return set(typer.main.get_command(app).commands.keys())


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.json"
    set_config_path(cfg)
    yield cfg
    set_config_path(None)  # type: ignore[arg-type]


TOP_LEVEL_COMMANDS = [
    "onboard",
    # The owner's machine registry (`rarus ops connection add/list/doctor`),
    # lifted from the on-call agent's checkout. Campaigns stay in that checkout;
    # what the host registers here is which machines exist and how to reach them.
    "ops",
    "gateway",
    "agent",
    "status",
    "upgrade",
    "doctor",
    "channels",
    "cron",
    "instructions",
    "provider",
    "sandbox",
    "sentinel",
    "skill",
]


@pytest.mark.parametrize("command", TOP_LEVEL_COMMANDS)
def test_top_level_command_help_does_not_crash(command: str) -> None:
    """Every top-level command's ``--help`` exits 0 with no leaked crash."""
    r = runner.invoke(app, [command, "--help"])
    assert r.exit_code == 0, f"{command} --help exited {r.exit_code}: {r.stdout}"
    assert r.exception is None, f"{command} --help raised an unexpected exception: {r.exception!r}"


def test_chat_is_not_a_registered_command() -> None:
    """Tripwire for a `raven chat` REPL landing.

    The e2e tier carried an xfail-strict placeholder that spawned `raven chat`
    to prove it does not exist -- a real subprocess per run for a command that
    is neither implemented nor planned. This is the same signal for free, and
    unlike that file it actually runs. Whoever adds a chat REPL will see this
    fail and can write coverage for the real thing.
    """
    assert "chat" not in _registered_command_names()


def test_root_help_does_not_crash() -> None:
    """``raven --help`` should list every command without crashing."""
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    assert r.exception is None
    for cmd in TOP_LEVEL_COMMANDS:
        assert cmd in r.stdout, f"missing command in root --help: {cmd}"


# Subcommand --help coverage (depth 2): catches missing imports inside
# subcommand-group modules.

CHANNEL_SUBCOMMANDS = [
    "status",
    "login",
    "enable",
    "disable",
    "set",
    "get",
    "reset",
    "show",
    "list",
]


@pytest.mark.parametrize("subcmd", CHANNEL_SUBCOMMANDS)
def test_channels_subcommand_help_does_not_crash(subcmd: str) -> None:
    """Every ``channels`` subcommand's ``--help`` exits cleanly."""
    r = runner.invoke(app, ["channels", subcmd, "--help"])
    assert r.exit_code == 0, f"channels {subcmd} --help exited {r.exit_code}"
    assert r.exception is None


SKILL_SUBCOMMANDS = ["list", "get"]


@pytest.mark.parametrize("subcmd", SKILL_SUBCOMMANDS)
def test_skill_subcommand_help_does_not_crash(subcmd: str) -> None:
    """Every ``skill`` subcommand's ``--help`` exits cleanly."""
    r = runner.invoke(app, ["skill", subcmd, "--help"])
    assert r.exit_code == 0, f"skill {subcmd} --help exited {r.exit_code}"
    assert r.exception is None


SENTINEL_SUBCOMMANDS = [
    "status",
    "tick",
    "ticks",
    "nudges",
    "decisions",
    "discover-now",
    "routines",
]


@pytest.mark.parametrize("subcmd", SENTINEL_SUBCOMMANDS)
def test_sentinel_subcommand_help_does_not_crash(subcmd: str) -> None:
    """Every ``sentinel`` subcommand's ``--help`` exits cleanly."""
    r = runner.invoke(app, ["sentinel", subcmd, "--help"])
    assert r.exit_code == 0, f"sentinel {subcmd} --help exited {r.exit_code}"
    assert r.exception is None


# Read-only command bodies that don't need network / LLM:


def test_status_command_body_does_not_crash(tmp_config: Path) -> None:
    """``rarus status`` reads config + prints rows without crashing."""
    r = runner.invoke(app, ["status"])
    assert r.exception is None, f"status crashed: {r.exception!r}"
    assert r.exit_code == 0


def test_channels_list_body_does_not_crash(tmp_config: Path) -> None:
    """``rarus channels list`` enumerates channels without crashing."""
    r = runner.invoke(app, ["channels", "list"])
    assert r.exception is None
    assert r.exit_code == 0


def test_cron_list_body_does_not_crash(tmp_config: Path) -> None:
    """``rarus cron list`` reads cron jobs without crashing."""
    r = runner.invoke(app, ["cron", "list"])
    assert r.exception is None
    assert r.exit_code == 0


# Full set of top-level commands + subcommand groups registered on the root
# app (superset of TOP_LEVEL_COMMANDS, which only lists the --help-probed ones).
REGISTERED_COMMAND_NAMES = {
    # The A2A protocol face (`rarus a2a serve`): refuses to start in a
    # sub-agent process, since serving a port does not ride the tool
    # registry the way outbound `a2a_send` does.
    "a2a",
    # The ACP agent server: an editor spawns `rarus acp` and speaks
    # JSON-RPC to its stdio. Distinct from the client direction, which is
    # not a command -- raven spawns those agents itself.
    "acp",
    "agent",
    # Plural beside the singular: `agents` manages the roster of agent
    # products (`agents new` scaffolds one), while `agent` talks to the
    # host's own agent.
    "agents",
    # Areas of responsibility: an agent owns part of a project, keeps a journal, may keep watch.
    "areas",
    # The operator's attention queue: what is waiting on a person, by priority.
    "attention",
    "channels",
    # A branch's, commit's or pull request's GitHub CI, and why the red checks are red.
    "ci",
    # The code index of a project: a ranked map of what it defines, and where a name is.
    "codemap",
    "cron",
    "doctor",
    # Why a refusal happened and which config value lifts it.
    "explain",
    "gateway",
    "import",
    # Revisions of the agent's instruction files (soul.md, agent.md, TOOLS.md):
    # log, diff and rollback.
    "instructions",
    # Agent mail: sub-agents of one conversation write to each other by name;
    # an external agent sends with it, the operator reads the history.
    "mail",
    # Plumbing a sub-agent spawns, not something a person types: `rarus mcp
    # bridge <socket>` pumps frames to a server the host holds open.
    "mcp",
    # Operator notifications: rules route events to webhook, ntfy, sound or desktop.
    "notify",
    "onboard",
    # The owner's machine registry (`rarus ops connection add/list/doctor`),
    # lifted from the on-call agent's checkout. Campaigns stay in that checkout;
    # what the host registers here is which machines exist and how to reach them.
    "ops",
    "playbook",
    # Singular: per-server actions (`plugin auth <server>`). Distinct from the
    # plural listing below, which answers "what is installed".
    "plugin",
    "plugins",
    "provider",
    # Pairing remote devices with a one-time ticket; their tokens reach the
    # gateway from wherever the owner routes them.
    "remote",
    # Reports, retrospectives and the run archive, read off the journals.
    "report",
    # Independent code review: a verdict from someone other than the author
    # before a branch merges, and the merge that waits for it.
    "review",
    # The weights and dictionaries that cannot live in git. On the CLI rather
    # than only under `scripts/`, because the wheel carries no scripts: this is
    # the command the two "not installed" errors name, and the one a one-line
    # install runs for itself.
    "resources",
    # Which model router is in force, and where a request would go and why.
    "routing",
    "sandbox",
    # Python scripts that run agents, message them and wait on what happens.
    "scenario",
    "sentinel",
    "serve",
    "sessions",
    "skill",
    "status",
    # Table apps and the outside sources a sync pass keeps them in step with.
    "table",
    # Joining, stopping and removing roster agents through requests the leader
    # approves.
    "team",
    "tracing",
    "trajectory",
    "tui",
    "upgrade",
    # Voice input: the transcription settings in force, and a clip through the channels' door.
    "voice",
    # Projects whose failing checks and red CI become tasks by themselves.
    "watch",
    # The page, beside `serve`, which is the gateway. `web` attaches to a
    # gateway already running rather than starting a second one.
    "web",
}


def test_version_flag_matches_installed_metadata() -> None:
    """``raven --version`` reports the installed package version, not a
    hand-written literal, so it can never drift from ``pyproject.toml``."""
    from importlib.metadata import version as pkg_version

    r = runner.invoke(app, ["--version"])
    assert r.exception is None
    assert r.exit_code == 0
    assert f"Raven v{pkg_version('raven')}" in r.stdout


def test_cli_import_does_not_pull_litellm() -> None:
    """The CLI entry module must not eagerly import litellm (it dominates cold
    start). Checked in a subprocess: ``sys.modules`` is process-global, so a full
    ``pytest tests/`` run pollutes it via sibling tests that import litellm and an
    in-process assertion would false-fail.
    """
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-c", "import raven.cli.commands, sys; assert 'litellm' not in sys.modules"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


def test_no_logs_subcommand_registered() -> None:
    """There is no ``raven logs`` command; adding one must break this test."""
    assert "logs" not in _registered_command_names()


def test_a_pattern_argument_reaches_the_command_as_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the real entry point: on Windows Click would expand `a*` into the
    files it matches before the command saw it (two files here: an extra-argument
    error). The pattern is a path pattern for the area, and it is stored as typed."""
    import sys

    from raven.agent.subagent.areas import AreaStore
    from raven.cli.commands import run

    (tmp_path / "a1.txt").write_text("x", encoding="utf-8")
    (tmp_path / "a2.txt").write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["raven", "areas", "assign", "probe", "-a", "Raven", "-p", "a*", "-t", "t"])

    with pytest.raises(SystemExit) as done:
        run()

    assert done.value.code in (0, None)
    assert AreaStore().get("probe").paths == ["a*"]


def test_registered_command_set_is_pinned() -> None:
    """Pin the exact command surface so any add/remove trips a test."""
    assert _registered_command_names() == REGISTERED_COMMAND_NAMES

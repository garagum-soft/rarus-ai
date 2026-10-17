"""Scenarios: scripts that run real roster agents, message them and wait, with a record of each run.

The agents are command-line agents run by the real sub-agent manager -- small
Python programs that read the task on stdin -- so every run here goes through
the same dispatch path, mail identity and backend a configured agent does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.agent.subagent.manager import SubagentManager
from raven.config.schema import ThirdPartyCliSubagentConfig
from raven.scenario.catalog import bind_args, discover, read_spec
from raven.scenario.journal import events
from raven.scenario.runner import run_scenario

PY = sys.executable.replace("\\", "/")


def _agent(code: str) -> str:
    return f'"{PY}" -c "{code}"'


WRITER = _agent("import sys; t=sys.stdin.read(); print('draft v2' if 'reviewer asks' in t else 'draft v1')")
REVIEWER = _agent("import sys; t=sys.stdin.read(); print('APPROVED' if 'draft v2' in t else 'make it v2')")
ECHO = _agent("import sys; sys.stdout.write('echo: ' + sys.stdin.read().strip()[:60])")
SLOW = _agent("import time, sys; sys.stdin.read(); time.sleep(5); print('slow done')")
NAP = _agent("import time, sys; sys.stdin.read(); time.sleep(0.6); print('nap done')")
BROKEN = _agent("import sys; sys.stderr.write('tool missing'); sys.exit(3)")
REPORTER = _agent(
    "import os, sys; sys.stdin.read(); from raven.agent.subagent.mailbox import Mailbox; "
    "Mailbox(os.environ['RAVEN_MAIL_SESSION']).send(os.environ['RAVEN_MAIL_AS'], 'scenario', 'status: all green'); "
    "print('reported')"
)
LISTENER = _agent(
    "import os, sys, time; sys.stdin.read(); from raven.agent.subagent.mailbox import Mailbox; "
    "box = Mailbox(os.environ['RAVEN_MAIL_SESSION']); me = os.environ['RAVEN_MAIL_AS']; mail = []; "
    "end = time.time() + 15\nwhile not mail and time.time() < end:\n    mail = box.take(me); time.sleep(0.2)\n"
    "print('heard: ' + (mail[0].body if mail else 'nothing'))"
)


class _NoModel:
    """Every agent here is a command-line one; nothing asks Raven's own model."""

    def get_default_model(self) -> str:
        return "none"


def _manager(workspace: Path) -> SubagentManager:
    agents = {
        "writer": WRITER,
        "reviewer": REVIEWER,
        "echo": ECHO,
        "slow": SLOW,
        "nap": NAP,
        "broken": BROKEN,
        "reporter": REPORTER,
        "listener": LISTENER,
    }
    return SubagentManager(
        provider=_NoModel(),  # type: ignore[arg-type]
        workspace=workspace,
        agents=[ThirdPartyCliSubagentConfig(name=n, command=c) for n, c in agents.items()],
    )


def _scenario(home: Path, name: str, body: str, params: str = "{}") -> None:
    directory = home / "scenarios"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(f'"""{name} under test."""\n\nPARAMS = {params}\n\n{body}', encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    # Set for the agents' own processes too: they write the scenario's mail here.
    monkeypatch.setenv("RARUS_HOME", str(path))
    return path


async def _run(name: str, workspace: Path, args: dict[str, str] | None = None, **kw):
    spec = discover(workspace)[name]
    return await run_scenario(spec, args or {}, manager=_manager(workspace), workspace=workspace, **kw)


# -- the catalogue ---------------------------------------------------------------------------


def test_the_shipped_scenarios_are_listed_with_what_they_take(home: Path, tmp_path: Path) -> None:
    found = discover(tmp_path)

    assert {"review-loop", "debate", "fan-out"} <= set(found)
    review = found["review-loop"]
    assert review.source == "builtin" and not review.problem
    assert review.params == {"task": None, "writer": "Raven", "reviewer": "Raven", "rounds": "3"}
    assert review.summary.startswith("Write, review, revise")


def test_the_more_specific_place_wins_and_listing_never_runs_a_file(home: Path, tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    _scenario(home, "debate", "async def main(sc):\n    return 'mine'\n")
    workspace = tmp_path / "proj"
    (workspace / ".raven" / "scenarios").mkdir(parents=True)
    (workspace / ".raven" / "scenarios" / "probe.py").write_text(
        f"open({str(marker)!r}, 'w').write('x')\n\nasync def main(sc):\n    return 1\n", encoding="utf-8"
    )
    (workspace / ".raven" / "scenarios" / "broken.py").write_text("PARAMS = {'a': len('x')}\n", encoding="utf-8")
    # Saved with a byte-order mark, as an editor on Windows may.
    (workspace / ".raven" / "scenarios" / "bom.py").write_text(
        '"""Saved with a BOM."""\n\nasync def main(sc):\n    return 1\n', encoding="utf-8-sig"
    )

    found = discover(workspace)

    assert found["debate"].source == "home"
    assert found["probe"].source == "workspace" and not marker.exists()
    assert "PARAMS must be a literal" in found["broken"].problem
    assert found["bom"].problem == "" and found["bom"].summary == "Saved with a BOM."


def test_arguments_take_defaults_and_refuse_what_the_scenario_does_not_name(home: Path, tmp_path: Path) -> None:
    spec = discover(tmp_path)["review-loop"]

    assert bind_args(spec, {"task": "x"})["rounds"] == "3"
    with pytest.raises(ValueError, match="needs task"):
        bind_args(spec, {})
    with pytest.raises(ValueError, match="takes no colour"):
        bind_args(spec, {"task": "x", "colour": "red"})


# -- running ---------------------------------------------------------------------------------


async def test_a_shipped_scenario_drives_two_agents_until_approval(home: Path, tmp_path: Path) -> None:
    lines: list[str] = []

    outcome = await _run(
        "review-loop",
        tmp_path,
        {"task": "write the notes", "writer": "writer", "reviewer": "reviewer"},
        echo=lines.append,
    )

    assert outcome.ok, outcome.error
    assert outcome.result.startswith("draft v2") and "Approved by the reviewer in round 2" in outcome.result
    assert [(r["name"], r["ok"]) for r in outcome.runs] == [
        ("writer", True),
        ("reviewer", True),
        ("writer", True),
        ("reviewer", True),
    ]
    meta = json.loads((outcome.record / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "succeeded" and meta["args"]["rounds"] == "3"
    assert (outcome.record / "result.md").read_text(encoding="utf-8") == outcome.result
    kinds = [e["event"] for e in events(outcome.run_id)]
    assert kinds[0] == "scenario.start" and kinds[-1] == "scenario.end" and kinds.count("run.end") == 4
    assert any("approved in round 2" in line for line in lines)


async def test_runs_started_together_work_at_once_and_first_cancels_the_rest(home: Path, tmp_path: Path) -> None:
    _scenario(
        home,
        "race",
        "async def main(sc):\n"
        "    a, b = sc.start('nap', 'go', name='a'), sc.start('nap', 'go', name='b')\n"
        "    both = await sc.wait(a, b)\n"
        "    winner = await sc.first(sc.start('slow', 'go', name='tortoise'), sc.start('echo', 'go', name='hare'))\n"
        "    return ' | '.join([r.text.strip() for r in both] + [winner.name])\n",
    )

    outcome = await _run("race", tmp_path)

    assert outcome.ok, outcome.error
    assert outcome.result == "nap done | nap done | hare"
    log = [(e["event"], e.get("name")) for e in events(outcome.run_id)]
    assert log.index(("run.start", "b")) < log.index(("run.end", "a")), "the two naps overlapped"
    assert ("run.cancelled", "tortoise") in log
    assert outcome.seconds < 4.5, "the slow run was not waited for"


async def test_a_failed_run_is_a_reply_and_a_missing_agent_stops_the_scenario(home: Path, tmp_path: Path) -> None:
    _scenario(
        home,
        "fragile",
        "async def main(sc):\n"
        "    bad = await sc.run('broken', 'build it')\n"
        "    late = await sc.run('slow', 'wait', timeout=0.5)\n"
        "    return f'{bad.ok}|{bad.error[:60]}|{late.ok}|{late.error}'\n",
    )
    _scenario(home, "typo", "async def main(sc):\n    await sc.run('nobody', 'hello')\n")

    fragile = await _run("fragile", tmp_path)
    typo = await _run("typo", tmp_path)

    assert fragile.ok and fragile.result.startswith("False|") and fragile.result.endswith("|False|timed out after 0.5s")
    assert "tool missing" in fragile.result or "exit" in fragile.result.lower()
    assert typo.status == "failed" and "no enabled agent 'nobody'" in typo.error


@pytest.mark.slow(reason="Two real agent processes; the listener must be running before mail is written to it")
async def test_agents_and_the_script_talk_through_the_scenarios_mail(home: Path, tmp_path: Path) -> None:
    _scenario(
        home,
        "chatter",
        "import asyncio\n\n"
        "async def main(sc):\n"
        "    report = await sc.run('reporter', 'report your status')\n"
        "    note = await sc.receive(sender='reporter', match='green', timeout=10)\n"
        "    listening = sc.start('listener', 'wait for the word')\n"
        "    # Written while the listener runs, so it arrives in its inbox mid-run; mail\n"
        "    # written before a run starts travels with its task instead.\n"
        "    await asyncio.sleep(1.5)\n"
        "    await sc.send('listener', 'the word is swordfish')\n"
        "    heard = await listening\n"
        "    return f'{report.text.strip()} / {note.body} / {heard.text.strip()}'\n",
    )

    outcome = await _run("chatter", tmp_path)

    assert outcome.ok, outcome.error
    assert outcome.result == "reported / status: all green / heard: the word is swordfish"
    log = events(outcome.run_id)
    assert any(e["event"] == "mail.in" and e["sender"] == "reporter" for e in log)
    assert any(e["event"] == "mail.out" and e["to"] == "listener" for e in log)


async def test_a_scenario_past_its_time_is_stopped_with_its_runs(home: Path, tmp_path: Path) -> None:
    _scenario(home, "stuck", "async def main(sc):\n    await sc.run('slow', 'wait')\n")

    outcome = await _run("stuck", tmp_path, timeout=1.0)

    assert outcome.status == "timed out"
    assert outcome.runs == [{"name": "slow", "agent": "slow", "ok": False, "cancelled": True, "seconds": None}]
    assert outcome.seconds < 4.5


async def test_an_error_in_the_script_is_the_outcome_with_its_traceback(home: Path, tmp_path: Path) -> None:
    _scenario(
        home,
        "oops",
        "async def main(sc):\n    sc.log('about to fail')\n    raise RuntimeError('the plan fell through')\n",
    )

    outcome = await _run("oops", tmp_path)

    assert outcome.status == "failed" and "RuntimeError: the plan fell through" in outcome.error
    assert [e["text"] for e in events(outcome.run_id) if e["event"] == "log"] == ["about to fail"]


def test_a_file_that_cannot_run_is_refused_before_anything_starts(home: Path, tmp_path: Path) -> None:
    path = tmp_path / "nomain.py"
    path.write_text('"""no main"""\n', encoding="utf-8")

    assert read_spec(path, "home").problem == "no `async def main(sc)`"


def test_run_in_without_mail_is_unchanged(tmp_path: Path) -> None:
    """The one shared change: run_in takes mail only when asked, so best-of and the probes run as before."""
    import asyncio

    from raven.agent.subagent import mailbox

    seen = []

    class _Backend:
        kind = "cli"

        async def run(self, task, **_kw):
            seen.append(mailbox.current())
            return "ok"

    manager = _manager(tmp_path)
    manager._resolve_backend = lambda agent: _Backend()  # type: ignore[method-assign]

    asyncio.run(manager.run_in("echo", "t", tmp_path))
    asyncio.run(manager.run_in("echo", "t", tmp_path, mail_session="scenario:x", mail_name="w1"))

    assert seen[0] is None
    assert (seen[1].session_key, seen[1].name, seen[1].agent) == ("scenario:x", "w1", "echo")


# -- the command line ------------------------------------------------------------------------


def test_the_command_line_lists_writes_runs_and_reads_back(home: Path, tmp_path: Path, monkeypatch) -> None:
    from raven.cli import scenario_commands
    from raven.cli.commands import app

    monkeypatch.setattr(scenario_commands, "_manager", lambda: _manager(tmp_path))
    runner = CliRunner()

    listed = runner.invoke(app, ["scenario", "list", "-w", str(tmp_path)])
    made = runner.invoke(app, ["scenario", "new", "greet"])
    again = runner.invoke(app, ["scenario", "new", "greet"])
    shown = runner.invoke(app, ["scenario", "show", "greet", "-w", str(tmp_path)])
    missing = runner.invoke(app, ["scenario", "run", "greet", "-w", str(tmp_path)])
    ran = runner.invoke(
        app,
        ["scenario", "run", "greet", "-w", str(tmp_path), "--arg", "task=hi there", "--arg", "agent=echo", "--json"],
    )
    runs = runner.invoke(app, ["scenario", "runs"])
    outcome = json.loads(ran.output.strip().splitlines()[-1])
    logged = runner.invoke(app, ["scenario", "log", outcome["id"]])

    assert listed.exit_code == 0 and "review-loop" in listed.output and "task*" in listed.output
    assert made.exit_code == 0 and (home / "scenarios" / "greet.py").is_file()
    assert again.exit_code == 1
    assert "task: required" in shown.output
    assert missing.exit_code == 2 and "needs task" in missing.output
    assert ran.exit_code == 0 and outcome["status"] == "succeeded" and outcome["result"] == "echo: hi there"
    assert outcome["id"] in runs.output and "succeeded" in runs.output
    assert "worker (echo) ok" in logged.output and "echo: hi there" in logged.output

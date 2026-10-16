"""Capability probes record what agents were seen to do; assignment picks the agent whose record fits."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.agent.subagent.capability_matrix import (
    PROBES,
    CapabilityMatrix,
    Observation,
    choose,
    infer_needs,
    probe_agent,
    rank,
    record_run,
)

PY = sys.executable.replace("\\", "/")

# An agent that does what the probes ask: reads, writes, runs and codes for real.
DOER = r"""
import hashlib, pathlib, re, subprocess, sys
task = sys.stdin.read()
m = re.search(r"Reply with exactly this token and nothing else: (\S+)", task)
if m: print(m.group(1)); sys.exit()
m = re.search(r"Read the file (\S+) in your working directory", task)
if m: print(pathlib.Path(m.group(1)).read_text()); sys.exit()
m = re.search(r"Create a file named (\S+) in your working directory containing exactly the text (\S+)\.", task)
if m: pathlib.Path(m.group(1)).write_text(m.group(2)); print("DONE"); sys.exit()
m = re.search(r"\n(python -c .+)$", task)
if m:
    cmd = m.group(1).replace("python", '"' + sys.executable + '"', 1)
    print(subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()); sys.exit()
m = re.search(r"Write a Python file (\S+) in your working directory", task)
if m:
    pathlib.Path(m.group(1)).write_text("def digit_sum(n):\n    return sum(int(d) for d in str(n))\n"); print("DONE"); sys.exit()
pathlib.Path("value.txt").write_text("42\n"); print("did it")
"""

# An agent that only talks: it can repeat a token and nothing else.
TALKER = r"""
import re, sys
task = sys.stdin.read()
m = re.search(r"token and nothing else: (\S+)", task)
print(m.group(1) if m else "I would do that, sure.")
"""


@pytest.fixture
def matrix(tmp_path: Path) -> CapabilityMatrix:
    return CapabilityMatrix(tmp_path / "matrix.json")


async def _script_runner(scripts: dict[str, str], tmp_path: Path):
    import asyncio

    paths = {}
    for name, body in scripts.items():
        path = tmp_path / f"{name}.py"
        path.write_text(body, encoding="utf-8")
        paths[name] = path

    async def run(agent: str, task: str, workspace: Path) -> str:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(paths[agent]),
            cwd=workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate(task.encode("utf-8"))
        return out.decode("utf-8")

    return run


async def test_a_probe_leaves_an_entry_in_the_matrix_and_assignment_picks_the_agent_that_passed(
    matrix: CapabilityMatrix, tmp_path: Path
) -> None:
    run = await _script_runner({"doer": DOER, "talker": TALKER}, tmp_path)

    doer = await probe_agent("doer", run, matrix, root=tmp_path / "ws")
    talker = await probe_agent("talker", run, matrix, root=tmp_path / "ws")

    assert {o.capability: o.ok for o in doer} == dict.fromkeys(PROBES, True)
    assert {o.capability: o.ok for o in talker} == {**dict.fromkeys(PROBES, False), "reply": True}
    assert matrix.cell("doer", "run_commands").passes == 1 and matrix.cell("talker", "write_files").fails == 1

    picked = choose("Read the failing test log, fix the bug and run the tests", ["talker", "doer"], matrix)
    assert picked is not None and picked.agent == "doer"
    plain = rank({"reply"}, ["talker", "doer"], matrix)
    assert plain[0].score == plain[1].score, "for a plain answer both have the same record"


def test_needs_are_read_from_the_tasks_words() -> None:
    assert infer_needs("what is 2+2") == {"reply"}
    assert infer_needs("Read the config file and tell me the port") >= {"reply", "read_files"}
    assert infer_needs("Implement a function and run the tests") >= {"code", "run_commands"}
    assert infer_needs("Прочитай файл и исправь функцию") >= {"read_files", "write_files", "code"}


def test_an_unprobed_agent_sits_at_an_even_chance_and_evidence_breaks_ties(matrix: CapabilityMatrix) -> None:
    matrix.record(Observation("a", "code", True, 100))
    matrix.record(Observation("a", "code", False, 100))
    ranked = rank({"code"}, ["a", "b"], matrix)

    assert [(r.agent, round(r.score, 3)) for r in ranked] == [("a", 0.5), ("b", 0.5)], "1/2 observed vs prior: tie"
    assert "code 1/2" in ranked[0].why and "code unprobed" in ranked[1].why


def test_a_failed_probe_ranks_below_an_unprobed_one_but_only_probed_agents_are_picked(matrix: CapabilityMatrix) -> None:
    matrix.record(Observation("a", "run_commands", False, 10))
    assert [r.agent for r in rank({"run_commands"}, ["a", "b"], matrix)] == ["b", "a"]
    assert choose("run the tests", ["a", "b"], matrix).agent == "a", "b was never seen to run at all"
    assert choose("run the tests", ["b"], matrix) is None
    assert choose("run the tests", ["b"], matrix, require_evidence=False).agent == "b"


def test_a_real_run_counts_for_each_need_of_its_task(matrix: CapabilityMatrix) -> None:
    record_run("a", "run the tests", True, 500, matrix, note="fast mode")
    assert matrix.cell("a", "run_commands").passes == 1 and matrix.cell("a", "reply").passes == 1
    assert matrix.cell("a", "run_commands").notes == ["run: fast mode"]


async def test_a_run_that_times_out_or_raises_is_a_failed_observation(matrix: CapabilityMatrix, tmp_path: Path) -> None:
    import asyncio

    async def slow(agent, task, ws):
        await asyncio.sleep(5)

    async def broken(agent, task, ws):
        raise RuntimeError("agent crashed")

    [timed] = await probe_agent("s", slow, matrix, root=tmp_path, capabilities=["reply"], timeout_s=0.1)
    [crashed] = await probe_agent("b", broken, matrix, root=tmp_path, capabilities=["reply"])

    assert not timed.ok and "no answer" in timed.note and not crashed.ok and "agent crashed" in crashed.note


def test_the_command_line_probes_shows_picks_and_routes_fast_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main path: roster agents from config, probed through the manager, then picked by fast mode."""
    from raven.cli.commands import app

    home = tmp_path / "home"
    home.mkdir()
    for name, body in (("doer", DOER), ("talker", TALKER)):
        (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
    agents = [
        {"name": name, "kind": "cli", "command": f'"{PY}" "{(tmp_path / f"{name}.py").as_posix()}"'}
        for name in ("doer", "talker")
    ]
    (home / "config.json").write_text(json.dumps({"subagents": {"agents": agents}}), encoding="utf-8")
    monkeypatch.setenv("RAVEN_HOME", str(home))
    runner = CliRunner()

    probed = runner.invoke(app, ["agents", "probe", "doer", "talker", "--cap", "reply", "--cap", "write_files"])
    shown = runner.invoke(app, ["agents", "matrix"])
    picked = runner.invoke(app, ["agents", "pick", "write the value file"])
    project = tmp_path / "proj"
    project.mkdir()
    check = f"\"{PY}\" -c \"import pathlib,sys; sys.exit(pathlib.Path('value.txt').read_text().strip() != '42')\""
    fast = runner.invoke(
        app,
        ["playbook", "fast", "create value.txt holding 42", "--agent", "auto", "--check", check, "-p", str(project)],
    )

    assert probed.exit_code == 0, probed.output
    assert "doer" in shown.output and "talker" in shown.output
    assert picked.output.splitlines()[1].startswith("doer"), picked.output
    assert fast.exit_code == 0, fast.output
    assert "agent: doer (picked by the capability matrix" in fast.output
    record = json.loads((home / "capability_matrix.json").read_text(encoding="utf-8"))
    assert record["doer"]["write_files"]["passes"] == 2, "the probe and the checked fast-mode run"

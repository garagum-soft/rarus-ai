"""Reports, retrospectives and the run archive, built from records Raven's own code wrote.

Every record here comes from the real writer -- a graph run through the DAG
tool with two command-line agents, spawns through the sub-agent manager, a stint
through its store, a conversation through the session manager, usage rows in the
ledger's own shape -- so the report is held to the formats as they are written,
not as a test imagines them.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.config.schema import ThirdPartyCliSubagentConfig
from raven.providers.base import LLMResponse
from raven.reports.archive import Archive
from raven.reports.report import build_report, parse_since, render_markdown
from raven.reports.retro import period_retro, run_retro
from raven.reports.runs import collect_runs
from raven.session.manager import SessionManager
from raven.stint.record import FINISHED, RoundRecord, StintRecord, StintStore
from tests.test_subagent_dag_runner import draining_dag_runs

PY = sys.executable.replace("\\", "/")
ECHO = f'"{PY}" -c "import sys; sys.stdout.write(sys.stdin.read())"'
BROKEN = f'"{PY}" -c "import sys; sys.stderr.write(\'the build tool is missing\'); sys.exit(3)"'


async def _graph_runs(workspace: Path) -> None:
    """One graph that fails half-way and one that completes, run by the real DAG tool."""
    tool = SubAgentDagTool(
        workspace=workspace,
        agents=[
            ThirdPartyCliSubagentConfig(name="echo", command=ECHO),
            ThirdPartyCliSubagentConfig(name="builder", command=BROKEN),
        ],
    )
    tool.set_context("tui", "report", "tui:report")
    async with draining_dag_runs():
        await tool.execute(
            task_summary="build and ship",
            nodes=[
                {"id": "plan", "subagent": "echo", "node_summary": "plan it", "prompt_template": "the plan"},
                {
                    "id": "build",
                    "subagent": "builder",
                    "node_summary": "build it",
                    "prompt_template": "{{ plan.output }}",
                    "depends_on": ["plan"],
                },
                {
                    "id": "ship",
                    "subagent": "echo",
                    "node_summary": "ship it",
                    "prompt_template": "{{ build.output }}",
                    "depends_on": ["build"],
                },
            ],
            background=False,
        )
        await tool.execute(
            task_summary="say hello twice",
            nodes=[
                {"id": "hello", "subagent": "echo", "node_summary": "say hello", "prompt_template": "hello"},
                {
                    "id": "again",
                    "subagent": "echo",
                    "node_summary": "say it again",
                    "prompt_template": "{{ hello.output }}",
                    "depends_on": ["hello"],
                },
            ],
            background=False,
        )


class _Scripted:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = replies

    def get_default_model(self) -> str:
        return "scripted"

    async def chat_with_retry(self, **_: Any) -> LLMResponse:
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


async def _spawns(workspace: Path, monkeypatch) -> None:
    from raven.agent.subagent.builtin_agents import GENERIC_AGENT
    from raven.agent.subagent.manager import SubagentManager

    manager = SubagentManager(provider=_Scripted([]), workspace=workspace)

    async def _quiet(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(manager, "_announce_result", _quiet)
    origin = {"channel": "tui", "chat_id": "report", "session_key": "tui:report", "agent": GENERIC_AGENT}
    ok = _Scripted([LLMResponse(content="the notes are written", finish_reason="stop")])
    bad = _Scripted([LLMResponse(content="Error: upstream overloaded", finish_reason="error")])
    await manager._run_subagent_inner("sp-ok", "write the notes", "notes", origin, None, ok, "m")
    await manager._run_subagent_inner("sp-bad", "summarise the logs", "logs", origin, None, bad, "m")


def _stint(workspace: Path) -> None:
    sdir = SessionManager(workspace).session_dir("tui:report")
    store = StintStore(sdir / "subagents" / "mas_dag" / "stints")
    now = int(time.time() * 1000)
    store.write(
        StintRecord(
            stint_id="stint-1",
            playbook="game-dev",
            spec={"roles": [{"as": "planner", "name": "echo"}, {"as": "builder", "name": "coder"}]},
            branch="stint/1",
            status=FINISHED,
            stop_reason="the round budget of 2 is spent",
            rounds=[
                RoundRecord(index=1, status="completed", verify=[{"name": "tests", "status": "ok"}]),
                RoundRecord(
                    index=2,
                    status="completed",
                    verify=[{"name": "tests", "status": "failed"}],
                    violations=["wrote src/x outside its globs"],
                ),
            ],
            questions=[
                {
                    "round": 2,
                    "role": "integration",
                    "text": "merge conflicts in shared.txt",
                    "answered_at": None,
                    "answer": "",
                }
            ],
            integration={"status": "conflict"},
            started_at_ms=now - 600_000,
            ended_at_ms=now - 60_000,
        )
    )


def _conversation(workspace: Path) -> None:
    sm = SessionManager(workspace)
    s = sm.get_or_create("tui:report")
    s.add_message("user", "build and ship it")
    s.add_message("assistant", "Done: the graph ran.")
    s.add_message("user", "and the logs?")
    s.add_message("assistant", "(turn failed: provider error)", turn_ended={"status": "failed", "reason": "provider"})
    sm.save(s)


def _usage(home: Path) -> None:
    telemetry = home / "ledger"
    telemetry.mkdir(parents=True)
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 30))
    rows = [
        {
            "ts": ts,
            "schema_version": 2,
            "model": "claude-x",
            "input_tokens": 1000,
            "output_tokens": 200,
            "cost_usd": 0.012,
            "session_key": "tui:report",
            "root_session_key": "tui:report",
        },
        {
            "ts": ts,
            "schema_version": 2,
            "model": "local-llm",
            "input_tokens": 500,
            "output_tokens": 50,
            "cost_usd": None,
            "session_key": "tui:other",
            "root_session_key": "tui:other",
        },
        {"_type": "tool_call", "ts": ts, "schema_version": 2, "session_key": "tui:report", "name": "read_file"},
    ]
    (telemetry / f"usage-{day}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
async def recorded(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    workspace, home = tmp_path / "ws", tmp_path / "home"
    workspace.mkdir()
    monkeypatch.setenv("RARUS_HOME", str(home))
    await _graph_runs(workspace)
    await _spawns(workspace, monkeypatch)
    _stint(workspace)
    _conversation(workspace)
    _usage(home)
    return workspace, home


async def test_every_run_kind_is_collected_from_its_real_record(recorded) -> None:
    workspace, _ = recorded

    runs = collect_runs(workspace)
    by = {(r.kind, r.title): r for r in runs}

    failed = by[("dag", "build and ship")]
    assert failed.status == "failed"
    assert failed.nodes.get("completed") == 1 and failed.nodes.get("failed") == 1
    assert [f["node"] for f in failed.failures] == ["build"] and failed.failures[0]["agent"] == "builder"
    assert failed.agents == ["echo", "builder"]
    ok = by[("dag", "say hello twice")]
    assert ok.status == "completed" and ok.nodes == {"completed": 2} and ok.receipt == "verified"
    assert ok.started_ms and ok.ended_ms >= ok.started_ms
    spawns = sorted((r for r in runs if r.kind == "spawn"), key=lambda r: r.id)
    assert [(s.title, s.status) for s in spawns] == [("logs", "failed"), ("notes", "completed")]
    assert spawns[0].failures and "overloaded" in spawns[0].failures[0]["error"]
    stint = next(r for r in runs if r.kind == "stint")
    assert stint.extra["failing_checks"] == ["tests"] and stint.extra["integration"] == "conflict"
    assert stint.extra["open_questions"] == ["merge conflicts in shared.txt"] and stint.agents == ["echo", "coder"]
    assert all(r.session_key == "tui:report" for r in runs)


async def test_a_retrospective_says_what_went_well_wrong_and_what_is_open(recorded) -> None:
    workspace, _ = recorded
    runs = {r.title: r for r in collect_runs(workspace)}

    failed = run_retro(runs["build and ship"])
    ok = run_retro(runs["say hello twice"])
    stint = run_retro(next(r for r in runs.values() if r.kind == "stint"))
    period = period_retro(list(runs.values()))

    assert "1 of 3 nodes completed" in failed.went_well
    assert any(line.startswith("build (builder) failed:") for line in failed.went_wrong)
    assert any("skipped" in line for line in failed.went_wrong)
    assert failed.open_items == ["rerun or fix: build"]
    assert ok.went_well[:2] == [
        "all 2 nodes completed",
        "the run's receipt verifies: the graph, prompts and outputs are as recorded",
    ]
    assert ok.went_wrong == []
    assert "checks failing in the last round: tests" in stint.went_wrong
    assert "1 ownership violation over the rounds" in stint.went_wrong
    assert "the branch was not merged (conflict)" in stint.went_wrong
    assert stint.open_items == ["unanswered: merge conflicts in shared.txt"]
    assert "builder: 1 failed node" in period.went_wrong
    assert any("unanswered" in item for item in period.open_items)


async def test_the_period_report_counts_conversations_runs_and_reported_cost_only(recorded) -> None:
    workspace, home = recorded

    report = build_report(workspace, home / "ledger", since_ms=parse_since("1d"))
    text = render_markdown(report)

    assert report["conversations"] == {"conversations": 1, "asked": 2, "answered": 1, "turns_ended": {"failed": 1}}
    assert report["runs_by_kind"]["dag"] == {"completed": 1, "failed": 1}
    assert report["usage"]["calls"] == 2 and report["usage"]["cost_usd"] == 0.012
    assert report["usage"]["cost_missing_calls"] == 1, "a call with no reported cost is counted, not priced"
    assert report["usage"]["top_sessions"] == [{"session_key": "tui:report", "cost_usd": 0.012}]
    for needle in (
        "# Raven report",
        "## Retrospective",
        "## Runs",
        "## Failed runs",
        "build (builder) failed",
        "## Cost by model",
        "1 calls without a reported cost",
    ):
        assert needle in text, needle
    only_other = build_report(workspace, home / "ledger", since_ms=parse_since("1d"), session="tui:other")
    assert only_other["runs"] == [] and only_other["usage"]["calls"] == 1


async def test_the_archive_keeps_finished_runs_once_and_outlives_their_records(recorded) -> None:
    import shutil

    workspace, home = recorded
    archive = Archive(home / "reports")

    first = archive.update(collect_runs(workspace))
    again = archive.update(collect_runs(workspace))
    shutil.rmtree(workspace / "sessions")
    kept = archive.rows()

    assert first == 5 and again == 0
    assert {r["kind"] for r in kept} == {"dag", "spawn", "stint"} and len(kept) == 5
    assert archive.find("stint-1")["extra"]["integration"] == "conflict"


def test_periods_parse_and_a_bad_one_is_refused() -> None:
    now = 1_800_000_000.0
    assert parse_since("7d", now=now) == int((now - 7 * 86400) * 1000)
    assert parse_since("90m", now=now) == int((now - 5400) * 1000)
    assert parse_since("2026-10-01") > 0
    with pytest.raises(ValueError, match="not a period"):
        parse_since("last week")


async def test_the_command_line_builds_a_report_lists_runs_and_retros_one(recorded, monkeypatch) -> None:
    from raven.cli.commands import app

    workspace, home = recorded
    (home / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(workspace)}}}), encoding="utf-8"
    )
    runner = CliRunner()
    out = home / "weekly.md"

    built = runner.invoke(app, ["report", "build", "--since", "1d", "--out", str(out)])
    listed = runner.invoke(app, ["report", "runs", "--failed"])
    stint_retro = runner.invoke(app, ["report", "retro", "stint-1"])
    missing = runner.invoke(app, ["report", "retro", "nope"])
    shown = runner.invoke(app, ["report", "runs", "--json", "--kind", "dag"])

    assert built.exit_code == 0, built.output
    assert "5 newly archived" in built.output and "! builder: 1 failed node" in built.output
    assert out.read_text(encoding="utf-8").startswith("# Raven report")
    assert listed.exit_code == 0 and "failed" in listed.output and "say hello twice" not in listed.output
    assert "checks failing in the last round: tests" in stint_retro.output
    assert missing.exit_code == 1
    assert {r["title"] for r in json.loads(shown.output)} == {"build and ship", "say hello twice"}
    assert (home / "reports" / "runs.jsonl").is_file()

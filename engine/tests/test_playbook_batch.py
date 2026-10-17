"""One workflow over a list of inputs: a run per input, events for each, results in input order."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.playbook.batch import run_batch


async def test_three_inputs_make_three_runs_and_each_reports_progress() -> None:
    events: list[dict] = []
    calls: list[int] = []

    async def run_one(index: int, values: dict) -> str:
        calls.append(index)
        await asyncio.sleep(0.01 * (3 - index))
        return f"report on {values['topic']}"

    items = await run_batch(
        [{"topic": "a"}, {"topic": "b"}, {"topic": "c"}], run_one, max_concurrency=3, on_event=events.append
    )

    assert sorted(calls) == [0, 1, 2]
    assert [item.result for item in items] == ["report on a", "report on b", "report on c"], "results in input order"
    assert events[0] == {"event": "batch_started", "total": 3}
    for index in range(3):
        assert any(e["event"] == "item_started" and e["index"] == index for e in events)
        assert any(e["event"] == "item_finished" and e["index"] == index and e["ok"] for e in events)
    assert [e["done"] for e in events if e["event"] == "progress"] == [1, 2, 3]
    assert events[-1] == {"event": "batch_finished", "ok": 3, "failed": 0}


async def test_a_failing_input_is_that_inputs_result_and_the_others_finish() -> None:
    async def run_one(index: int, values: dict) -> str:
        if index == 1:
            raise RuntimeError("bad input")
        return "fine"

    items = await run_batch([{}, {}, {}], run_one)

    assert [(i.ok, i.error) for i in items] == [(True, ""), (False, "RuntimeError: bad input"), (True, "")]


async def test_concurrency_is_capped() -> None:
    running = 0
    peak = 0

    async def run_one(index: int, values: dict) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1

    await run_batch([{}] * 6, run_one, max_concurrency=2)
    assert peak == 2
    with pytest.raises(ValueError):
        await run_batch([{}], run_one, max_concurrency=0)


def test_the_command_line_runs_a_playbook_once_per_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The main path: `raven playbook batch` over a stored playbook and a CLI agent."""
    from raven.cli.commands import app

    home = tmp_path / "home"
    home.mkdir()
    python = sys.executable.replace("\\", "/")
    echo = f'"{python}" -c "import sys; sys.stdout.write(sys.stdin.read())"'
    (home / "config.json").write_text(
        json.dumps({"subagents": {"agents": [{"name": "echo", "kind": "cli", "command": echo}]}}), encoding="utf-8"
    )
    monkeypatch.setenv("RARUS_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    from raven.config.loader import load_config
    from raven.playbook import PlaybookStore
    from raven.playbook.types import PlaybookSpec

    spec = PlaybookSpec.model_validate(
        {
            "name": "summarise",
            "description": "summarise a topic",
            "taskSummary": "summarise",
            "mode": "dag",
            "confirm": False,
            "triggers": {"keywords": ["summarise"]},
            "params": {"topic": {"description": "what to summarise", "required": True}},
            "nodes": [
                {
                    "id": "sum",
                    "subagent": "echo",
                    "nodeSummary": "summarise",
                    "promptTemplate": "summarise {{ params.topic }}",
                }
            ],
        }
    )
    PlaybookStore(load_config().workspace_path / "playbooks").save(spec)
    inputs = tmp_path / "inputs.jsonl"
    inputs.write_text("\n".join(json.dumps({"topic": t}) for t in ("rust", "go", "zig")), encoding="utf-8")

    result = CliRunner().invoke(app, ["playbook", "batch", "summarise", "--inputs", str(inputs), "--events", "-j", "2"])

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    finished = [e for e in events if e["event"] == "item_finished"]
    assert sorted(e["index"] for e in finished) == [0, 1, 2] and all(e["ok"] for e in finished)
    assert [e["done"] for e in events if e["event"] == "progress"] == [1, 2, 3]
    replies = {e["index"]: e["result"] for e in finished}
    assert "summarise rust" in replies[0] and "summarise go" in replies[1] and "summarise zig" in replies[2]

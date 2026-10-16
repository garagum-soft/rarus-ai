"""Verifiable run receipts: issued when a run finishes, checked against the files later."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.agent.subagent.receipts import RECEIPT_FILE, issue_receipt, load_receipt, verify_receipt
from raven.config.schema import ThirdPartyCliSubagentConfig
from tests.test_subagent_dag_runner import draining_dag_runs


def _run(tmp_path: Path) -> tuple[Path, Path]:
    run_root = tmp_path / "mas_dag" / "run-1"
    nodes = tmp_path / "nodes"
    run_root.mkdir(parents=True)
    nodes.mkdir()
    (run_root / "graph.json").write_text(json.dumps({"nodes": [{"id": "plan"}, {"id": "build"}]}), encoding="utf-8")
    (run_root / "manifest.json").write_text(json.dumps({"plan": {"status": "completed"}}), encoding="utf-8")
    for node in ("plan", "build"):
        (nodes / f"{node}.prompt.md").write_text(f"do {node}", encoding="utf-8")
        (nodes / f"{node}.out.md").write_text(f"{node} done", encoding="utf-8")
    return run_root, nodes


def test_a_receipt_covers_the_run_and_every_node_and_passes_verification(tmp_path: Path) -> None:
    run_root, nodes = _run(tmp_path)

    receipt = issue_receipt(run_root, nodes, "run-1")

    assert [e.path for e in receipt.entries] == [
        "run/graph.json",
        "run/manifest.json",
        "nodes/build.prompt.md",
        "nodes/build.out.md",
        "nodes/plan.prompt.md",
        "nodes/plan.out.md",
    ]
    assert receipt.root.startswith("sha256:") and (run_root / RECEIPT_FILE).is_file()
    verdict = verify_receipt(run_root, nodes, expected_root=receipt.root)
    assert verdict.ok and verdict.problems == []


def test_changing_an_output_fails_verification_and_says_which(tmp_path: Path) -> None:
    run_root, nodes = _run(tmp_path)
    issue_receipt(run_root, nodes, "run-1")

    (nodes / "build.out.md").write_text("build done, and something else", encoding="utf-8")

    verdict = verify_receipt(run_root, nodes)
    assert not verdict.ok
    assert verdict.problems == ["nodes/build.out.md changed since the receipt was issued"]


def test_a_removed_or_extra_artifact_fails_verification(tmp_path: Path) -> None:
    run_root, nodes = _run(tmp_path)
    issue_receipt(run_root, nodes, "run-1")
    (nodes / "plan.prompt.md").unlink()

    assert "nodes/plan.prompt.md is missing" in verify_receipt(run_root, nodes).problems


def test_editing_the_receipt_itself_is_caught(tmp_path: Path) -> None:
    run_root, nodes = _run(tmp_path)
    issued = issue_receipt(run_root, nodes, "run-1")
    (nodes / "plan.out.md").write_text("forged", encoding="utf-8")
    data = json.loads((run_root / RECEIPT_FILE).read_text(encoding="utf-8"))
    forged = issue_receipt.__globals__["_sha"](b"forged")
    for entry in data["entries"]:
        if entry["path"] == "nodes/plan.out.md":
            entry["sha256"] = forged
    (run_root / RECEIPT_FILE).write_text(json.dumps(data), encoding="utf-8")

    verdict = verify_receipt(run_root, nodes, expected_root=issued.root)
    assert not verdict.ok
    assert any("own list was altered" in p for p in verdict.problems)
    assert load_receipt(run_root).root == issued.root


def test_a_published_root_that_differs_fails(tmp_path: Path) -> None:
    run_root, nodes = _run(tmp_path)
    issue_receipt(run_root, nodes, "run-1")
    verdict = verify_receipt(run_root, nodes, expected_root="sha256:" + "0" * 64)
    assert not verdict.ok and "not the published" in verdict.problems[0]


def test_the_command_line_verifies_and_refuses(tmp_path: Path) -> None:
    from raven.cli.commands import app

    run_root, nodes = _run(tmp_path)
    receipt = issue_receipt(run_root, nodes, "run-1")
    runner = CliRunner()

    ok = runner.invoke(app, ["playbook", "verify-receipt", str(run_root), "--root", receipt.root])
    (nodes / "plan.out.md").write_text("tampered", encoding="utf-8")
    bad = runner.invoke(app, ["playbook", "verify-receipt", str(run_root)])

    assert ok.exit_code == 0 and f"verified: {receipt.root}" in ok.output
    assert bad.exit_code == 1 and "plan.out.md changed" in bad.output


class TestTheMainPath:
    @pytest.fixture(autouse=True)
    async def _drain(self):
        async with draining_dag_runs():
            yield

    async def test_a_finished_run_issues_a_receipt_and_quotes_its_root(self, tmp_path: Path) -> None:
        python = sys.executable.replace("\\", "/")
        echo = f'"{python}" -c "import sys; sys.stdout.write(sys.stdin.read())"'
        tool = SubAgentDagTool(workspace=tmp_path, agents=[ThirdPartyCliSubagentConfig(name="echo", command=echo)])
        tool.set_context("cli", "direct", "cli:direct")

        out = await tool.execute(
            [{"id": "only", "subagent": "echo", "node_summary": "echo", "prompt_template": "hello receipts"}],
            task_summary="receipt check",
            background=False,
        )
        text = str(getattr(out, "model_text", out))

        assert "Receipt: sha256:" in text, text
        receipt_path = Path(text.rsplit("; ", 1)[1].rstrip(")").strip())
        run_root = receipt_path.parent
        verdict = verify_receipt(run_root, run_root.parent.parent / "nodes")
        assert verdict.ok, verdict.problems
        assert verdict.root in text
        assert {e.path for e in load_receipt(run_root).entries} >= {"run/graph.json", "nodes/only.out.md"}

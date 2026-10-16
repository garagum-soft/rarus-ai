"""Merging a finished stint's branch back: the two-step merge, its checks, and the escalation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.config.schema import ThirdPartyCliSubagentConfig
from raven.playbook.executor import PlaybookExecutor
from raven.playbook.stint import _summary, integrate_finished
from raven.stint.git import ProjectGit
from raven.stint.integrate import integrate_branch
from raven.stint.record import FINISHED, StintRecord, StintStore
from raven.stint.verify import CheckSpec
from tests import test_playbook_stint as stint_tests
from tests.test_playbook_stint import _spec
from tests.test_subagent_dag_runner import draining_dag_runs


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _commit(cwd: Path, name: str, text: str, message: str) -> None:
    (cwd / name).write_text(text, encoding="utf-8")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", message)


def _repos(tmp_path: Path) -> tuple[Path, Path]:
    """A project on its own branch and a stint checkout cut from it, the way `isolation: worktree` lays them out."""
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    _commit(project, "shared.txt", "base\n", "base")
    tree = tmp_path / "tree"
    ProjectGit(project).worktree_add(tree, "stint/x", ProjectGit(project).head())
    return project, tree


def test_a_finished_branch_is_merged_into_the_project_branch(tmp_path: Path) -> None:
    project, tree = _repos(tmp_path)
    _commit(tree, "feature.txt", "new work\n", "stint work")
    _commit(project, "other.txt", "person's work\n", "meanwhile")

    result = integrate_branch(project, tree, "stint/x")

    assert result.status == "merged", result
    assert result.into == "main"
    assert (project / "feature.txt").read_text(encoding="utf-8") == "new work\n"
    assert (tree / "other.txt").exists(), "the project's branch was merged into the checkout first"
    assert _git(project, "rev-parse", "HEAD") == result.commit
    subprocess.run(["git", "-C", str(project), "merge-base", "--is-ancestor", "stint/x", "main"], check=True)


def test_a_conflict_is_aborted_and_both_trees_are_left_as_they_were(tmp_path: Path) -> None:
    project, tree = _repos(tmp_path)
    _commit(tree, "shared.txt", "stint version\n", "stint edits shared")
    _commit(project, "shared.txt", "person version\n", "person edits shared")
    before = _git(project, "rev-parse", "HEAD")

    result = integrate_branch(project, tree, "stint/x")

    assert result.status == "conflict"
    assert result.files == ["shared.txt"]
    assert _git(project, "rev-parse", "HEAD") == before
    assert (project / "shared.txt").read_text(encoding="utf-8") == "person version\n"
    assert not (Path(_git(tree, "rev-parse", "--git-dir")) / "MERGE_HEAD").exists(), "the merge was aborted"
    assert (tree / "shared.txt").read_text(encoding="utf-8") == "stint version\n"
    assert "shared.txt" in result.escalation()


def test_failing_checks_keep_the_branch_out_of_the_project(tmp_path: Path) -> None:
    project, tree = _repos(tmp_path)
    _commit(tree, "feature.txt", "x\n", "stint work")
    before = _git(project, "rev-parse", "HEAD")
    failing = CheckSpec(name="tests", command=f'"{sys.executable}" -c "raise SystemExit(1)"', timeout_sec=60)

    result = integrate_branch(project, tree, "stint/x", checks=[failing], log_dir=tmp_path / "logs")

    assert result.status == "checks_failed"
    assert result.checks[0]["status"] == "failed"
    assert _git(project, "rev-parse", "HEAD") == before
    assert "tests" in result.escalation()


def test_passing_checks_let_the_merge_through(tmp_path: Path) -> None:
    project, tree = _repos(tmp_path)
    _commit(tree, "feature.txt", "x\n", "stint work")
    passing = CheckSpec(name="tests", command=f'"{sys.executable}" -c "pass"', timeout_sec=60)

    result = integrate_branch(project, tree, "stint/x", checks=[passing], log_dir=tmp_path / "logs")

    assert result.status == "merged"
    assert result.checks == [{"name": "tests", "status": "ok", "returncode": 0}]


def test_a_project_with_uncommitted_edits_is_not_touched(tmp_path: Path) -> None:
    project, tree = _repos(tmp_path)
    _commit(tree, "feature.txt", "x\n", "stint work")
    (project / "shared.txt").write_text("half-done edit\n", encoding="utf-8")

    result = integrate_branch(project, tree, "stint/x")

    assert result.status == "refused" and "uncommitted" in result.detail
    assert (project / "shared.txt").read_text(encoding="utf-8") == "half-done edit\n"


def test_a_stint_that_worked_in_place_has_nothing_to_merge_into(tmp_path: Path) -> None:
    project, _ = _repos(tmp_path)

    result = integrate_branch(project, project, "main")

    assert result.status == "refused" and "isolation: worktree" in result.detail


def _record(project: Path, tree: Path) -> StintRecord:
    return StintRecord(
        stint_id="stint-x",
        playbook="game-dev",
        spec={},
        workdir=str(tree),
        project=str(project),
        branch="stint/x",
        status=FINISHED,
        stop_reason="the round budget of 1 is spent",
        round_index=1,
    )


def test_the_finish_step_merges_and_the_summary_says_so(tmp_path: Path) -> None:
    project, tree = _repos(tmp_path)
    _commit(tree, "feature.txt", "x\n", "stint work")
    record = _record(project, tree)

    integrate_finished(_spec(integrate="merge", isolation="worktree"), record, StintStore(tmp_path / "stints"))

    assert record.integration["status"] == "merged"
    assert record.questions == []
    assert "now merged into main" in _summary(record)
    assert "nothing has merged" not in _summary(record)


def test_the_finish_step_escalates_a_conflict_to_a_person(tmp_path: Path) -> None:
    project, tree = _repos(tmp_path)
    _commit(tree, "shared.txt", "stint\n", "stint")
    _commit(project, "shared.txt", "person\n", "person")
    record = _record(project, tree)

    integrate_finished(_spec(integrate="merge", isolation="worktree"), record, StintStore(tmp_path / "stints"))

    assert record.integration["status"] == "conflict"
    assert record.integration["files"] == ["shared.txt"]
    assert len(record.questions) == 1
    assert record.questions[0]["role"] == "integration" and "shared.txt" in record.questions[0]["text"]
    assert record.questions[0]["answered_at"] is None
    assert "NOT merged (conflict)" in _summary(record)


def test_a_playbook_that_does_not_ask_leaves_the_branch_alone(tmp_path: Path) -> None:
    project, tree = _repos(tmp_path)
    _commit(tree, "feature.txt", "x\n", "stint work")
    record = _record(project, tree)

    integrate_finished(_spec(isolation="worktree"), record, StintStore(tmp_path / "stints"))

    assert record.integration == {}
    assert not (project / "feature.txt").exists()
    assert "nothing has merged" in _summary(record)


def test_integrate_only_belongs_to_rounds_mode() -> None:
    from raven.playbook.types import PlaybookSpec

    with pytest.raises(ValueError, match="integrate"):
        PlaybookSpec.model_validate(
            {
                "name": "g",
                "description": "d",
                "taskSummary": "t",
                "mode": "dag",
                "nodes": [{"id": "a", "name": "echo", "promptTemplate": "x"}],
                "integrate": "merge",
            }
        )


class TestTheMainPath:
    """A real stint, run by the executor, merging its branch as it finishes."""

    @pytest.fixture(autouse=True)
    async def _drain(self):
        async with draining_dag_runs():
            yield

    @staticmethod
    def _executor(tmp_path: Path, announced: list[str]) -> PlaybookExecutor:
        """The stint suite's executor, with an echo agent every platform can start.

        The suite uses `cat`, which a Windows subprocess cannot find; the
        interpreter running this test is always there.
        """
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        ProjectGit(project).ensure_repo()

        async def announce(run_id: str, text: str, origin: dict) -> None:
            announced.append(text)

        python = sys.executable.replace("\\", "/")
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[
                ThirdPartyCliSubagentConfig(
                    name="echo", command=f'"{python}" -c "import sys; sys.stdout.write(sys.stdin.read())"'
                )
            ],
            announce=announce,
            charge=lambda _key: None,
        )
        tool.set_context("web", "default", "web:stint")
        return PlaybookExecutor(dag_tool=tool, workspace=project)

    async def test_a_stint_merges_its_branch_into_the_project_when_it_finishes(self, tmp_path: Path) -> None:
        announced: list[str] = []
        executor = self._executor(tmp_path, announced)
        project = tmp_path / "project"

        await executor.execute(_spec(confirm=False, isolation="worktree", integrate="merge"), {}, max_rounds=1)
        summary = await stint_tests.TestRunning._await_announce(announced)

        record = StintStore(executor.dag_tool.stints_root("web:stint")).list()[0]
        assert record.stop_reason == "the round budget of 1 is spent", summary
        assert record.integration.get("status") == "merged", record.integration
        assert record.integration["into"] == ProjectGit(project).branch()
        subprocess.run(
            ["git", "-C", str(project), "merge-base", "--is-ancestor", record.branch, "HEAD"],
            check=True,
        )
        assert "now merged into" in summary

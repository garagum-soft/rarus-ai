"""Preparing parallel checkouts: ports of their own, and the git-ignored files the project lists."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from raven.stint.git import ProjectGit
from raven.stint.workspace_env import (
    ENV_FILE,
    allocate_ports,
    copy_ignored,
    prepare_checkout,
    release_ports,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    (project / ".gitignore").write_text(".env\ncache/\nbig.log\n", encoding="utf-8")
    (project / ".worktreeinclude").write_text("# local config and the build cache\n.env\ncache/*\n", encoding="utf-8")
    (project / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-q", "-m", "base")
    (project / ".env").write_text("API_URL=http://localhost\n", encoding="utf-8")
    (project / "cache").mkdir()
    (project / "cache" / "deps.json").write_text("{}", encoding="utf-8")
    (project / "big.log").write_text("x" * 1000, encoding="utf-8")
    return project


def test_two_workspaces_get_different_ports_and_keep_them(tmp_path: Path) -> None:
    registry = tmp_path / "ports.json"

    a = allocate_ports(registry, "stint-a", 2)
    b = allocate_ports(registry, "stint-b", 2)

    assert len(a) == 2 and len(b) == 2 and not set(a) & set(b)
    assert allocate_ports(registry, "stint-a", 2) == a, "asking again keeps the same ports"
    release_ports(registry, "stint-a")
    assert "stint-a" not in json.loads(registry.read_text(encoding="utf-8"))


def test_a_port_in_use_is_not_handed_out(tmp_path: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        taken = busy.getsockname()[1]
        ports = allocate_ports(tmp_path / "ports.json", "x", 1, port_range=(taken, taken + 50))
    assert ports and ports[0] != taken


def test_the_ignored_files_the_project_lists_are_copied_and_no_others(tmp_path: Path) -> None:
    project = _project(tmp_path)
    checkout = tmp_path / "tree"
    ProjectGit(project).worktree_add(checkout, "work/x", ProjectGit(project).head())

    copied = copy_ignored(project, checkout)

    assert sorted(copied) == [".env", "cache/deps.json"]
    assert (checkout / ".env").read_text(encoding="utf-8") == "API_URL=http://localhost\n"
    assert not (checkout / "big.log").exists(), "an ignored file the include list does not name stays behind"


def test_two_prepared_checkouts_run_side_by_side_and_their_env_files_are_never_committed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    registry = tmp_path / "ports.json"
    trees = []
    for name in ("one", "two"):
        tree = tmp_path / name
        ProjectGit(project).worktree_add(tree, f"work/{name}", ProjectGit(project).head())
        env = prepare_checkout(project, tree, owner=name, registry=registry, ports=1)
        trees.append((tree, env))

    (tree_one, env_one), (tree_two, env_two) = trees
    assert env_one.ports != env_two.ports
    assert (tree_one / ENV_FILE).read_text(encoding="utf-8") == f"PORT={env_one.ports[0]}\n"
    assert (tree_one / ".env").is_file() and (tree_two / ".env").is_file()
    assert ENV_FILE not in ProjectGit(tree_one).changed(), "the env file is excluded from the repository"
    ProjectGit(tree_one).commit("work")
    assert ENV_FILE not in _git(tree_one, "show", "--name-only", "--format=", "HEAD")


class TestTheMainPath:
    @pytest.fixture(autouse=True)
    async def _drain(self):
        from tests.test_subagent_dag_runner import draining_dag_runs

        async with draining_dag_runs():
            yield

    async def test_a_stint_with_its_own_checkout_gets_a_port_and_the_listed_files(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import sys

        from raven.agent.subagent.dag_tool import SubAgentDagTool
        from raven.config.schema import ThirdPartyCliSubagentConfig
        from raven.playbook.executor import PlaybookExecutor
        from raven.stint.record import StintStore
        from tests import test_playbook_stint as stint_tests
        from tests.test_playbook_stint import _spec

        monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
        project = _project(tmp_path)
        announced: list[str] = []

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
        executor = PlaybookExecutor(dag_tool=tool, workspace=project)

        await executor.execute(_spec(confirm=False, isolation="worktree"), {}, max_rounds=1)
        await stint_tests.TestRunning._await_announce(announced)

        record = StintStore(tool.stints_root("web:stint")).list()[0]
        tree = Path(record.workdir)
        assert tree != project
        assert (tree / ".env").read_text(encoding="utf-8") == "API_URL=http://localhost\n"
        assert (tree / ENV_FILE).read_text(encoding="utf-8").startswith("PORT=")
        held = json.loads((tmp_path / "home" / "workspaces" / "ports.json").read_text(encoding="utf-8"))
        assert record.stint_id in held

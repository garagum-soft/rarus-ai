"""One graph run across two repositories: the manifest, the checkouts, and the per-repository merge."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.config.schema import ThirdPartyCliSubagentConfig
from raven.playbook import multirepo
from raven.playbook.executor import PlaybookExecutor
from raven.playbook.types import PlaybookSpec, RepoEntry
from raven.stint.git import ProjectGit
from tests.test_subagent_dag_runner import draining_dag_runs

# The node's agent: reads the briefing off its prompt and writes one file into
# every checkout it names -- a stand-in for a coding agent that works each repo.
WORKER = (
    "import re, sys, pathlib; text = sys.stdin.read(); "
    "paths = re.findall(r'^- [A-Za-z0-9_-]+: (.+) [(]branch ', text, re.M); "
    "[pathlib.Path(p.strip(), 'built.txt').write_text('built by the run', encoding='utf-8') for p in paths]; "
    "print('worked', len(paths), 'repositories')"
)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _repo(path: Path, name: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _spec(**over: Any) -> PlaybookSpec:
    base: dict[str, Any] = {
        "name": "two-repo-build",
        "description": "build across the api and the web repositories",
        "taskSummary": "build both",
        "mode": "dag",
        "triggers": {"keywords": ["build"]},
        "confirm": False,
        "nodes": [
            {
                "id": "build",
                "subagent": "worker",
                "nodeSummary": "build both",
                "promptTemplate": "build the feature in every repository",
            }
        ],
    }
    base.update(over)
    return PlaybookSpec.model_validate(base)


def test_a_manifest_belongs_to_dag_mode_and_names_each_repository_once() -> None:
    spec = _spec(repos=[{"name": "api", "path": "a"}, {"name": "web", "path": "b"}])
    assert [r.name for r in spec.repos or []] == ["api", "web"]
    with pytest.raises(ValueError, match="unique"):
        _spec(repos=[{"name": "api", "path": "a"}, {"name": "api", "path": "b"}])
    with pytest.raises(ValueError, match="must not carry repos"):
        _spec(mode="prompt", nodes=None, prompts="compose", repos=[{"name": "api", "path": "a"}])


def test_every_repository_gets_its_own_checkout_or_none_do(tmp_path: Path) -> None:
    api, web = _repo(tmp_path / "api", "api"), _repo(tmp_path / "web", "web")

    prepared = multirepo.prepare(
        [RepoEntry(name="api", path=str(api)), RepoEntry(name="web", path="web")],
        run_tag="run 1",
        root=tmp_path / "checkouts",
        workspace=tmp_path,
    )

    assert [(p.name, p.branch, p.base) for p in prepared] == [
        ("api", "raven/run-1", "main"),
        ("web", "raven/run-1", "main"),
    ]
    assert all((p.checkout / "README.md").is_file() for p in prepared)
    assert "- api:" in multirepo.briefing(prepared) and "- web:" in multirepo.briefing(prepared)

    (tmp_path / "plain").mkdir()
    with pytest.raises(multirepo.ManifestError, match="not a git repository"):
        multirepo.prepare(
            [RepoEntry(name="api", path=str(api)), RepoEntry(name="plain", path="plain")],
            run_tag="run 2",
            root=tmp_path / "checkouts",
            workspace=tmp_path,
        )
    assert not (tmp_path / "checkouts" / "run-2").exists(), "a manifest that cannot be worked opens nothing"


def test_each_branch_merges_into_its_own_repository_and_a_conflict_stays_local(tmp_path: Path) -> None:
    api, web = _repo(tmp_path / "api", "api"), _repo(tmp_path / "web", "web")
    prepared = multirepo.prepare(
        [RepoEntry(name="api", path=str(api)), RepoEntry(name="web", path=str(web))],
        run_tag="r",
        root=tmp_path / "checkouts",
        workspace=tmp_path,
    )
    (prepared[0].checkout / "api.py").write_text("x = 1\n", encoding="utf-8")
    (prepared[1].checkout / "README.md").write_text("# web, from the run\n", encoding="utf-8")
    (web / "README.md").write_text("# web, by a person\n", encoding="utf-8")
    _git(web, "commit", "-q", "-am", "person edits")

    results = multirepo.merge_all(prepared, message="merge run r")

    assert results["api"].merged and (api / "api.py").is_file()
    assert results["web"].status == "conflict" and results["web"].files == ["README.md"]
    assert (web / "README.md").read_text(encoding="utf-8") == "# web, by a person\n"
    text = multirepo.report(results)
    assert "api: merged raven/r into main" in text and "web: NOT merged (conflict)" in text


class TestTheMainPath:
    @pytest.fixture(autouse=True)
    async def _drain(self):
        async with draining_dag_runs():
            yield

    async def test_one_run_builds_both_repositories_and_merges_each_branch(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("RARUS_HOME", str(tmp_path / "home"))
        api, web = _repo(tmp_path / "api", "api"), _repo(tmp_path / "web", "web")
        python = sys.executable.replace("\\", "/")
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="worker", command=f'"{python}" -c "{WORKER}"')],
        )
        tool.set_context("cli", "direct", "cli:direct")
        executor = PlaybookExecutor(dag_tool=tool, workspace=tmp_path, background=False)

        plan = await executor.execute(
            _spec(repos=[{"name": "api", "path": str(api)}, {"name": "web", "path": "web"}]), {}
        )

        assert "Repositories:" in plan.reply, plan.reply
        assert "api: merged" in plan.reply and "web: merged" in plan.reply
        assert (api / "built.txt").read_text(encoding="utf-8") == "built by the run"
        assert (web / "built.txt").read_text(encoding="utf-8") == "built by the run"
        assert ProjectGit(api).branch() == "main" and ProjectGit(web).branch() == "main"


def test_the_generator_is_not_offered_repos() -> None:
    from raven.playbook.prompt import emit_tool

    assert "repos" not in emit_tool()[0]["function"]["parameters"]["properties"]

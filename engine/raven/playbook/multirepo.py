"""One graph run across several repositories, and the per-repository merge after it.

A playbook names the repositories it works (``repos:``). Before the graph runs,
each one gets a checkout of its own on a branch the run owns, cut from where the
repository is now; the nodes are told where every checkout is. When the run
finishes, each branch is merged back into its own repository by the stint's
two-step integration (``raven.stint.integrate``), one result per repository: a
conflict or a refusal in one does not stop the others, and a repository the run
did not change merges as a no-op.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raven.stint.git import HistoryError, ProjectGit
from raven.stint.integrate import IntegrationResult, integrate_branch
from raven.stint.workspace_env import prepare_checkout


class ManifestError(ValueError):
    """A repository the manifest names that cannot be worked."""


@dataclass(frozen=True)
class PreparedRepo:
    name: str
    project: Path
    checkout: Path
    branch: str
    base: str


def prepare(repos: Sequence[Any], *, run_tag: str, root: Path, workspace: Path) -> list[PreparedRepo]:
    """A checkout per repository on branch ``raven/<run_tag>``; nothing is made unless every one can be."""
    tag = re.sub(r"[^A-Za-z0-9._-]+", "-", run_tag).strip("-") or "run"
    branch = f"raven/{tag}"
    planned: list[tuple[str, Path, ProjectGit]] = []
    for repo in repos:
        project = Path(repo.path).expanduser()
        if not project.is_absolute():
            project = (workspace / project).resolve()
        git = ProjectGit(project)
        if not git.initialized:
            raise ManifestError(f"repository {repo.name!r}: {project} is not a git repository")
        if not git.head():
            raise ManifestError(f"repository {repo.name!r}: {project} has no commits to branch from")
        planned.append((repo.name, project, git))
    prepared: list[PreparedRepo] = []
    try:
        for name, project, git in planned:
            checkout = root / tag / name
            git.worktree_add(checkout, branch, git.head())
            prepare_checkout(project, checkout, owner=f"{tag}/{name}", registry=root / "ports.json")
            prepared.append(PreparedRepo(name, project, checkout, branch, git.branch()))
    except (HistoryError, OSError) as exc:
        for done in prepared:
            ProjectGit(done.project).worktree_remove(done.checkout)
        raise ManifestError(f"could not open a checkout: {exc}") from exc
    return prepared


def briefing(prepared: Sequence[PreparedRepo]) -> str:
    """What every node is told about the repositories, put ahead of its task."""
    lines = ["This run works several repositories, each in a checkout of its own:"]
    lines += [f"- {repo.name}: {repo.checkout} (branch {repo.branch})" for repo in prepared]
    lines.append(
        "Change a repository only inside its checkout and commit there; each branch is merged back into "
        "its own repository when the run finishes."
    )
    return "\n".join(lines) + "\n\n"


def merge_all(prepared: Sequence[PreparedRepo], *, message: str) -> dict[str, IntegrationResult]:
    """Merge every repository's branch back, each on its own."""
    results: dict[str, IntegrationResult] = {}
    for repo in prepared:
        # Work a node left uncommitted is still the run's work; committing it
        # here is what lets the merge carry it.
        tree = ProjectGit(repo.checkout)
        if tree.dirty():
            tree.commit(f"{message}: uncommitted work in {repo.name}")
        results[repo.name] = integrate_branch(
            repo.project, repo.checkout, repo.branch, message=f"{message} ({repo.name})"
        )
    return results


def report(results: dict[str, IntegrationResult]) -> str:
    lines = ["Repositories:"]
    for name, result in results.items():
        if result.merged:
            lines.append(f"- {name}: merged {result.branch} into {result.into} ({result.commit[:12]})")
        else:
            lines.append(f"- {name}: NOT merged ({result.status}). {result.escalation()}")
    return "\n".join(lines)


__all__ = ["ManifestError", "PreparedRepo", "briefing", "merge_all", "prepare", "report"]

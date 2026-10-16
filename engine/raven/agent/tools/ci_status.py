"""``ci_status``: what CI says about a branch, a commit or a pull request, and why it is red.

Reads GitHub's check runs and commit statuses through :mod:`raven.stint.ci`;
for each red check, its annotations and the error lines of its job log. With
``wait_s`` it polls until the checks finish, which closes the loop an agent
needs to fix a red build: push, wait, read what failed, fix, push again.

Read-only -- nothing is pushed, re-run or written to GitHub; pushing stays with
``exec`` and whatever approval it carries. The repository defaults to the
project checkout's ``origin`` and the ref to its branch, and the checkout path
goes through the same fence as the file tools.

Registered only when ``tools.ciStatus`` is on (see ``ToolsConfig.ci_status``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from raven.agent.tools.filesystem import _FsTool

MAX_WAIT_S = 3600


class CiStatusTool(_FsTool):
    """A commit's CI checks, with failure excerpts, optionally waiting for them to finish."""

    # The longest wait the schema allows, plus room to read the failures' logs.
    timeout_seconds = MAX_WAIT_S + 180.0

    @property
    def name(self) -> str:
        return "ci_status"

    @property
    def description(self) -> str:
        return (
            "Read GitHub CI for a branch, commit or pull request: every check (GitHub Actions and other CI) "
            "with its state, and for each failed one the annotations and the error lines of its job log. "
            "Defaults to the project's origin repository and current branch. Set wait_s to wait for running "
            "checks to finish (e.g. right after a push). Read-only: it never pushes or re-runs anything."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The project checkout, for its origin repository and branch (default: workspace root)",
                },
                "repo": {"type": "string", "description": "owner/repo, when it is not the checkout's origin"},
                "ref": {"type": "string", "description": "Branch, tag or commit SHA (default: the checkout's branch)"},
                "pr": {"type": "integer", "description": "A pull request number: reads its head commit", "minimum": 1},
                "wait_s": {
                    "type": "integer",
                    "description": f"Wait up to this many seconds for running checks to finish (0-{MAX_WAIT_S}, default 0)",
                    "minimum": 0,
                    "maximum": MAX_WAIT_S,
                },
                "fail_fast": {
                    "type": "boolean",
                    "description": "While waiting, stop at the first failed check instead of waiting for all",
                },
                "logs": {"type": "boolean", "description": "Read failed jobs' logs (default true)"},
            },
        }

    async def execute(
        self,
        path: str = ".",
        repo: str = "",
        ref: str = "",
        pr: int | None = None,
        wait_s: int = 0,
        fail_fast: bool = False,
        logs: bool = True,
        **kwargs: Any,
    ) -> str:
        from raven.stint import ci

        try:
            where = self._resolve(path or ".")
        except PermissionError as e:
            return f"Error: {e}"
        local = where if where.is_dir() else where.parent
        repo = (repo or "").strip() or await asyncio.to_thread(ci.repo_of, local)
        if not repo:
            return f"Error: {path!r} has no GitHub 'origin' remote; pass repo as owner/repo (only GitHub CI is read)."
        local_head = branch = ""
        try:
            if pr:
                branch, ref = await asyncio.to_thread(ci.resolve_pr, repo, int(pr))
            elif not (ref or "").strip():
                ref = await asyncio.to_thread(ci.branch_of, local)
                if not ref:
                    return "Error: the checkout is on a detached head; pass ref (a branch or SHA) or pr."
                local_head = await asyncio.to_thread(ci.head_of, local)
            wait = max(0, min(int(wait_s or 0), MAX_WAIT_S))
            if wait:
                report = await ci.wait_async(repo, ref.strip(), timeout_s=wait, fail_fast=bool(fail_fast))
            else:
                report = await asyncio.to_thread(ci.read, repo, ref.strip())
            if report.red:
                report = await asyncio.to_thread(ci.explain, report, logs=bool(logs))
        except ValueError as e:  # CiError and the tracker's HTTP errors are ValueErrors
            return f"Error reading CI for {repo}@{ref or pr}: {e}"
        except OSError as e:
            return f"Error reaching GitHub: {e}"
        text = report.render(local_head=local_head)
        if pr:
            text = f"Pull request #{pr} ({branch or 'head'})\n" + text
        return text


__all__ = ["CiStatusTool"]

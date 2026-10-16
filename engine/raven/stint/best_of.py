"""Best of N: the same task tried N times in N checkouts, the best attempt applied to the base.

Every attempt starts from the same base commit in a checkout of its own
(``best-of/<tag>-<i>``), runs concurrently with the others, and has what it
left committed there. Each attempt is then measured by the same checks, and the
winner is the one with no failing check and the most passing ones; a tie goes to
the smaller change, then to the earlier attempt. The winner's branch is merged
into the project's branch; every attempt's branch is kept so a person can look
at the ones that lost, and their checkouts are removed.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from raven.stint.git import HistoryError, ProjectGit
from raven.stint.integrate import IntegrationResult, integrate_branch
from raven.stint.verify import CheckSpec, run_checks
from raven.stint.workspace_env import prepare_checkout, release_ports

Attempt = Callable[[int, Path], Awaitable[str]]


class BestOfError(ValueError):
    """A run that cannot start: no repository, no commits, or a bad attempt count."""


@dataclass
class AttemptResult:
    index: int
    branch: str
    checkout: Path
    output: str = ""
    error: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    changed_lines: int = 0

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c["status"] == "ok")

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c["status"] in ("failed", "timeout"))

    def rank(self) -> tuple[int, int, int, int]:
        """Bigger is better: clean, then most passing, then smallest change, then earliest."""
        clean = int(not self.error and self.failed == 0)
        return (clean, self.passed, -self.changed_lines, -self.index)


@dataclass
class BestOfResult:
    attempts: list[AttemptResult]
    winner: AttemptResult | None
    applied: IntegrationResult | None

    def report(self) -> str:
        lines = []
        for a in self.attempts:
            mark = "  <- applied" if self.winner is a and self.applied and self.applied.merged else ""
            state = (
                f"error: {a.error}"
                if a.error
                else f"{a.passed} passed, {a.failed} failed, {a.changed_lines} lines changed"
            )
            lines.append(f"attempt {a.index} ({a.branch}): {state}{mark}")
        if self.winner is None:
            lines.append("no attempt is clean; nothing was applied")
        elif self.applied is not None and not self.applied.merged:
            lines.append(f"the winner could not be applied: {self.applied.escalation()}")
        return "\n".join(lines)


async def best_of(
    project: Path,
    attempts: int,
    attempt: Attempt,
    *,
    checks: Sequence[CheckSpec],
    root: Path,
    tag: str,
    apply: bool = True,
) -> BestOfResult:
    """Run ``attempt`` N times in N checkouts, measure each, and apply the best one."""
    if not 1 <= attempts <= 10:
        raise BestOfError("attempts must be between 1 and 10")
    home = ProjectGit(Path(project))
    if not home.initialized:
        raise BestOfError(f"{project} is not a git repository")
    base = home.head()
    if not base:
        raise BestOfError(f"{project} has no commits to start the attempts from")
    tag = re.sub(r"[^A-Za-z0-9._-]+", "-", tag).strip("-") or "run"
    registry = Path(root) / "ports.json"
    results: list[AttemptResult] = []
    try:
        for i in range(1, attempts + 1):
            branch = f"best-of/{tag}-{i}"
            checkout = Path(root) / tag / str(i)
            home.worktree_add(checkout, branch, base)
            prepare_checkout(Path(project), checkout, owner=branch, registry=registry)
            results.append(AttemptResult(index=i, branch=branch, checkout=checkout))
    except (HistoryError, OSError) as exc:
        for made in results:
            home.worktree_remove(made.checkout)
        raise BestOfError(f"could not open a checkout: {exc}") from exc

    async def one(result: AttemptResult) -> None:
        try:
            result.output = await attempt(result.index, result.checkout)
        except Exception as exc:  # noqa: BLE001 - one failed attempt must not stop the others
            result.error = f"{type(exc).__name__}: {exc}"
            return
        tree = ProjectGit(result.checkout)
        if tree.dirty():
            tree.commit(f"best-of attempt {result.index}")
        result.changed_lines = tree.changed_line_count(base)
        if checks:
            ran = await asyncio.to_thread(
                run_checks, checks, cwd=result.checkout, log_dir=result.checkout.parent / f"checks-{result.index}"
            )
            result.checks = [{"name": r.name, "status": r.status} for r in ran]

    await asyncio.gather(*(one(r) for r in results))
    ranked = sorted(results, key=AttemptResult.rank, reverse=True)
    winner = ranked[0] if ranked and ranked[0].rank()[0] == 1 else None
    applied = None
    if winner is not None and apply:
        applied = integrate_branch(
            Path(project), winner.checkout, winner.branch, message=f"best of {attempts}: {winner.branch}"
        )
    for r in results:
        home.worktree_remove(r.checkout)
        release_ports(registry, r.branch)
    return BestOfResult(attempts=results, winner=winner, applied=applied)


__all__ = ["AttemptResult", "BestOfError", "BestOfResult", "best_of"]

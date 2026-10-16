"""Merging a finished stint's branch back into the branch its project is on.

Two steps, so that a conflict always lands where the run's own work is and never
in the person's checkout. First the project's branch is merged into the stint's
checkout; a conflict there is aborted and reported with the files that clashed.
Then the checks the playbook declares are run against that merged checkout, so
what reaches the project has been measured with the project's latest state in
it. Only then is the stint's branch merged into the project's branch. Anything
that stops the merge leaves both trees as they were and says why, so the caller
can hand the question to a person rather than resolve code conflicts unattended.

With a review book, a third gate stands before both steps: the branch merges
only with an approving verdict from an independent reviewer on the change as it
is now (``raven.stint.review``). Asked first, so an unreviewed branch is not
even merged into from the project's side.

What merges is the commit the checks measured, named by its hash: a check runs
the branch's own code, and a commit it made while running would otherwise ride
into the project unreviewed. The checks get the curated environment the rounds
get, not the host's with its keys.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from raven.stint.git import ProjectGit
from raven.stint.verify import CheckSpec, curated_env, run_checks

if TYPE_CHECKING:
    from raven.stint.review import ReviewBook

Outcome = Literal["merged", "conflict", "checks_failed", "unreviewed", "refused"]


@dataclass
class IntegrationResult:
    status: Outcome
    branch: str
    into: str = ""
    commit: str = ""
    files: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    review: str = ""
    """The review id the merge was gated on, when it was."""

    @property
    def merged(self) -> bool:
        return self.status == "merged"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def escalation(self) -> str:
        """The question a person is asked when the merge did not happen."""
        if self.status == "conflict":
            return (
                f"Merging {self.branch} into {self.into} conflicts in {', '.join(self.files) or 'the history'}. "
                "The merge was aborted and both branches are untouched; resolve it by hand."
            )
        if self.status == "checks_failed":
            failed = [str(row.get("name")) for row in self.checks if row.get("status") in ("failed", "timeout")]
            return (
                f"{self.branch} was not merged into {self.into}: with {self.into} merged in, these checks fail: "
                f"{', '.join(failed)}."
            )
        if self.status == "unreviewed":
            return (
                f"{self.branch} was not merged into {self.into}: {self.detail} Review it with "
                f"`raven review show {self.review}`, then `raven review merge {self.review}`."
            )
        return f"{self.branch} was not merged into {self.into or 'the project'}: {self.detail}"


def integrate_branch(
    project: Path,
    workdir: Path,
    branch: str,
    *,
    checks: Sequence[CheckSpec] = (),
    log_dir: Path | None = None,
    message: str = "",
    review: "ReviewBook | None" = None,
) -> IntegrationResult:
    """Merge ``branch`` (checked out at ``workdir``) into the branch ``project`` is on.

    ``review``, when given, makes an approving verdict a precondition.
    """
    home = ProjectGit(Path(project))
    tree = ProjectGit(Path(workdir))
    into = home.branch()
    result = IntegrationResult(status="refused", branch=branch, into=into)
    if not home.initialized:
        result.detail = f"{project} is not a git repository"
        return result
    if into == "HEAD":
        result.detail = f"{project} is on a detached HEAD, so a merge there would land on no branch"
        return result
    if Path(workdir).resolve() == Path(project).resolve() or into == branch:
        result.detail = (
            "the stint worked in the project's own checkout, so there is no other branch to merge into; "
            "use isolation: worktree"
        )
        return result
    # Tracked edits only: untracked files (the stint's own `.stint/` layout among
    # them) cannot be lost to a merge, and git refuses one that would overwrite them.
    if home.has_tracked_changes():
        result.detail = f"{project} has uncommitted changes; commit or stash them so the merge cannot touch them"
        return result
    if review is not None:
        approved, why, result.review = review.gate(project, branch)
        if not approved:
            result.status = "unreviewed"
            result.detail = why
            return result

    took, clashing = tree.merge_branch(into, f"merge {into} into {branch} before integrating")
    if not took:
        result.status = "conflict"
        result.files = list(clashing)
        return result

    measured = tree.head()
    if checks:
        log_dir = log_dir or Path(workdir) / ".stint-integration"
        ran = run_checks(checks, cwd=Path(workdir), log_dir=log_dir, env=curated_env())
        result.checks = [{"name": r.name, "status": r.status, "returncode": r.returncode} for r in ran]
        if any(r.status in ("failed", "timeout") for r in ran):
            result.status = "checks_failed"
            return result
    if tree.head() != measured:
        result.detail = (
            f"{branch} moved while its checks ran ({measured[:12]} -> {tree.head()[:12]}); "
            "what was measured is not what would merge"
        )
        return result

    took, clashing = home.merge_branch(measured, message or f"merge {branch}")
    if not took:
        result.status = "conflict"
        result.files = list(clashing)
        return result
    result.status = "merged"
    result.commit = home.head()
    if review is not None:
        review.mark_merged(result.review, result.commit)
    return result


__all__ = ["IntegrationResult", "integrate_branch"]

"""Independent code review: a branch's diff is judged by someone other than its author before it merges.

The node judge (``judge_node``) grades a step's output inside the run that
produced it. This is the other half: a separate reviewer -- a person, or a
roster agent that did not write the work -- reads the branch's diff against the
branch it would merge into, answers a checklist, and records a verdict. The
merge (``integrate_branch``) asks the verdict first and refuses without one.

Who may judge is a policy, after Paperclip's issue review policy:

- ``not_creator`` (the default): anyone but an author of the work. An author is
  whoever requested the review on the work's behalf -- for a stint, every
  agent that played one of its roles. A request with no known author fails
  closed: nobody can be shown to be independent of it.
- ``human_only``: only a person (``user:<name>``), and not one who wrote it.
- ``anyone``: the verdict is still required, only its author is not checked.

Asking again for the same branch never relaxes the terms: the stricter policy
stands, authors, checklist items and checks accumulate, and an approval given
under other terms is not carried over. The gate reads the approval against the
terms as they are when the merge asks, and against the branch the review named
as its target -- an approval of a merge into ``main`` is not one into ``release``.

A verdict is bound to the diff it read (its hash), not only to a commit. A
branch whose change differs from what was approved -- the author pushed more,
or rewrote it -- is unreviewed again: the approval was of other code. A branch
that only took the target branch in (the first step of the merge does exactly
that) still shows the same change and keeps its verdict. Every request, verdict
and refused verdict goes to an append-only journal, so the decision trail
survives the request files being rewritten.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from raven.stint.git import ProjectGit
from raven.utils.atomic_io import atomic_replace, locked_append, write_transaction

Policy = Literal["not_creator", "human_only", "anyone"]
POLICIES: tuple[str, ...] = ("not_creator", "human_only", "anyone")
#: Loosest first: each policy refuses every actor the one before it refuses.
_STRICTNESS: tuple[str, ...] = ("anyone", "not_creator", "human_only")
CHECK_ANSWERS: tuple[str, ...] = ("pass", "fail", "n/a")
MAX_DIFF_FOR_AGENT = 60_000


def _stricter(a: str, b: str) -> str:
    return max(a, b, key=_STRICTNESS.index)


class ReviewError(Exception):
    """A review step that cannot happen, with a code a caller can branch on."""

    def __init__(self, code: str, message: str, remediation: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.remediation = remediation


@dataclass(frozen=True)
class Actor:
    """Who acts: ``agent:<roster name>`` or ``user:<name>``."""

    kind: Literal["agent", "user"]
    name: str

    @classmethod
    def parse(cls, text: str) -> Actor:
        kind, sep, name = (text or "").strip().partition(":")
        if not sep or kind not in ("agent", "user") or not name.strip():
            raise ReviewError("actor_invalid", f"{text!r} is not an actor; write agent:<name> or user:<name>.")
        return cls(kind, name.strip())  # type: ignore[arg-type]

    def __str__(self) -> str:
        return f"{self.kind}:{self.name}"


@dataclass
class ReviewRequest:
    id: str
    project: str
    workdir: str
    branch: str
    into: str
    head: str
    authors: list[str]
    policy: str = "not_creator"
    checklist: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    diff_sha: str = ""
    source: str = ""
    playbook: str = ""
    verify: list[Any] = field(default_factory=list)
    requested_at: float = 0.0
    status: str = "pending"
    """pending, approved, rejected or merged; for the diff named in ``diff_sha``."""
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    merged_commit: str = ""

    def latest_verdict(self, diff_sha: str | None = None) -> dict[str, Any] | None:
        """The newest verdict on ``diff_sha`` (default: the diff this request shows)."""
        wanted = diff_sha or self.diff_sha
        mine = [v for v in self.verdicts if v.get("diff_sha") == wanted]
        return mine[-1] if mine else None

    def current_diff_sha(self) -> str:
        patch, _ = branch_diff(self.project, self.into, self.branch)
        return _sha(patch)


def default_root() -> Path:
    from raven.home import raven_home

    return raven_home() / "reviews"


def request_id(project: Path | str, branch: str) -> str:
    key = f"{Path(project).resolve()}\0{branch}"
    return "rv-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def branch_diff(project: Path | str, into: str, branch: str) -> tuple[str, list[str]]:
    """The change ``branch`` brings over ``into``, from their merge base, and the files it touches."""
    git = ProjectGit(Path(project))
    patch = git._run("diff", "--no-color", f"{into}...{branch}", check=False).stdout
    names = git._run("diff", "--name-only", f"{into}...{branch}", check=False).stdout.split()
    return patch, sorted(names)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ReviewBook:
    """Review requests, one file each, plus the append-only decision journal."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.journal_path = self.root / "journal.jsonl"

    def _path(self, rid: str) -> Path:
        if not re.fullmatch(r"rv-[0-9a-f]{10}", rid):
            raise ReviewError("review_unknown", f"{rid!r} is not a review id.")
        return self.root / f"{rid}.json"

    def _log(self, event: str, **data: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        row = {"at": time.time(), "event": event, **data}
        locked_append(self.journal_path, [json.dumps(row, ensure_ascii=False)])

    def _write(self, req: ReviewRequest) -> None:
        atomic_replace(self._path(req.id), json.dumps(asdict(req), ensure_ascii=False, indent=2))

    def get(self, rid: str) -> ReviewRequest:
        try:
            raw = json.loads(self._path(rid).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReviewError("review_unknown", f"There is no review {rid}.") from exc
        known = set(ReviewRequest.__dataclass_fields__)
        return ReviewRequest(**{k: v for k, v in raw.items() if k in known})

    def find(self, project: Path | str, branch: str) -> ReviewRequest | None:
        try:
            return self.get(request_id(project, branch))
        except ReviewError:
            return None

    def list(self) -> list[ReviewRequest]:
        out: list[ReviewRequest] = []
        for path in sorted(self.root.glob("rv-*.json")) if self.root.is_dir() else []:
            try:
                out.append(self.get(path.stem))
            except ReviewError:
                continue
        return sorted(out, key=lambda r: r.requested_at, reverse=True)

    def journal(self, limit: int = 50, rid: str | None = None) -> list[dict[str, Any]]:
        try:
            lines = self.journal_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if rid is None or row.get("review") == rid:
                rows.append(row)
        return rows[-limit:] if limit > 0 else rows

    # -- requesting -----------------------------------------------------------------

    def request(
        self,
        project: Path | str,
        workdir: Path | str,
        branch: str,
        *,
        authors: Iterable[str],
        policy: str = "not_creator",
        checklist: Sequence[str] = (),
        source: str = "",
        playbook: str = "",
        verify: Sequence[Any] = (),
    ) -> ReviewRequest:
        """Ask for a review of ``branch`` as it is now; asking again after it moved re-opens it.

        Asking again only adds to the terms of an open request: the stricter
        policy, every author, item and check either request named. Otherwise
        the request the merge gate itself sends people to could turn a
        ``human_only`` review into ``not_creator`` and drop the checks.
        """
        if policy not in POLICIES:
            raise ReviewError("policy_invalid", f"policy must be one of {', '.join(POLICIES)}.")
        for author in authors:
            Actor.parse(author)
        project = Path(project)
        home = ProjectGit(project)
        if not home.initialized:
            raise ReviewError("not_a_repository", f"{project} is not a git repository.")
        into = home.branch()
        if into == branch:
            raise ReviewError(
                "nothing_to_review", f"{project} is on {branch} itself; there is no other branch to merge into."
            )
        head = ProjectGit(Path(workdir)).head()
        if not head:
            raise ReviewError("branch_missing", f"{workdir} has no commit on {branch}.")
        patch, files = branch_diff(project, into, branch)
        rid = request_id(project, branch)
        self.root.mkdir(parents=True, exist_ok=True)
        with write_transaction(self._path(rid)):
            old = self.find(project, branch)
            authors, checklist, verify = sorted(set(authors)), list(checklist), list(verify)
            open_before = old is not None and old.status != "merged"
            if old is not None and open_before:
                policy = _stricter(old.policy, policy)
                authors = sorted(set(old.authors) | set(authors))
                checklist = [*old.checklist, *(item for item in checklist if item not in old.checklist)]
                verify = [*old.verify, *(entry for entry in verify if entry not in old.verify)]
                source, playbook = old.source or source, old.playbook or playbook
            req = ReviewRequest(
                id=rid,
                project=str(project),
                workdir=str(workdir),
                branch=branch,
                into=into,
                head=head,
                authors=authors,
                policy=policy,
                checklist=checklist,
                files=files,
                diff_sha=_sha(patch),
                source=source,
                playbook=playbook,
                verify=verify,
                requested_at=time.time(),
                verdicts=old.verdicts if old is not None else [],
            )
            same_terms = old is not None and (old.policy, old.authors, old.checklist, old.into) == (
                req.policy,
                req.authors,
                req.checklist,
                req.into,
            )
            if old is not None and old.diff_sha == req.diff_sha and (same_terms or not open_before):
                req.status, req.merged_commit = old.status, old.merged_commit
            self._write(req)
        self._log("requested", review=rid, branch=branch, into=into, head=head, authors=req.authors, policy=policy)
        return req

    # -- judging ------------------------------------------------------------------------

    def check_actor(self, req: ReviewRequest, actor: Actor) -> None:
        """Raise unless ``actor`` may judge ``req`` under its policy."""
        if req.policy == "anyone":
            return
        if req.policy == "human_only":
            if actor.kind != "user":
                raise ReviewError(
                    "review_policy_denied",
                    "Review policy `human_only` lets only a person approve or reject this branch.",
                    "Have a person submit the verdict as user:<name>.",
                )
            if str(actor) in req.authors:
                raise ReviewError(
                    "review_policy_denied",
                    f"{actor} wrote this branch, and review policy `human_only` needs another person to judge it.",
                    "Have a person who did not write the work submit the verdict.",
                )
            return
        if not req.authors:
            raise ReviewError(
                "review_policy_denied",
                "Review policy `not_creator` needs to know who wrote the work, and this request names nobody.",
                "Request the review again with --author for everyone who wrote the branch.",
            )
        if str(actor) in req.authors:
            raise ReviewError(
                "review_policy_denied",
                f"{actor} wrote this branch, and review policy `not_creator` needs someone else to judge it.",
                "Have a reviewer who did not write the work submit the verdict.",
            )

    def verdict(
        self,
        rid: str,
        actor: Actor | str,
        decision: str,
        *,
        checks: dict[str, str] | None = None,
        comment: str = "",
        diff_sha: str | None = None,
    ) -> ReviewRequest:
        """Record ``actor``'s verdict on the reviewed diff, or raise and journal why not.

        ``diff_sha`` is the diff the reviewer read, when the caller knows it (an
        agent's run, the CLI's ``--diff``): a verdict on a diff that is not the
        one under review is refused rather than recorded against it.
        """
        who = actor if isinstance(actor, Actor) else Actor.parse(actor)
        try:
            with write_transaction(self._path(rid)):
                req = self.get(rid)
                self._check_verdict(req, who, decision, checks or {}, comment, diff_sha)
                req.verdicts.append(
                    {
                        "by": str(who),
                        "decision": decision,
                        "head": req.head,
                        "diff_sha": req.diff_sha,
                        "checks": dict(checks or {}),
                        "comment": comment.strip(),
                        "at": time.time(),
                    }
                )
                req.status = "approved" if decision == "approve" else "rejected"
                self._write(req)
        except ReviewError as exc:
            self._log("refused", review=rid, by=str(who), decision=decision, code=exc.code, reason=str(exc))
            raise
        self._log("verdict", review=rid, by=str(who), decision=decision, head=req.head, comment=comment.strip())
        return req

    def _check_verdict(
        self,
        req: ReviewRequest,
        who: Actor,
        decision: str,
        checks: dict[str, str],
        comment: str,
        diff_sha: str | None,
    ) -> None:
        if decision not in ("approve", "reject"):
            raise ReviewError("decision_invalid", "The decision is approve or reject.")
        if req.status == "merged":
            raise ReviewError("already_merged", f"{req.branch} is already merged ({req.merged_commit[:12]}).")
        self.check_actor(req, who)
        if diff_sha is not None and diff_sha != req.diff_sha:
            raise ReviewError(
                "stale_diff", "The verdict is about another diff than the one under review; read it again."
            )
        if req.current_diff_sha() != req.diff_sha:
            raise ReviewError(
                "stale_diff",
                f"{req.branch} changed after the review was requested; request it again and review the new diff.",
            )
        unknown = sorted(set(checks) - set(req.checklist))
        if unknown:
            raise ReviewError("check_unknown", f"Not on this review's checklist: {', '.join(unknown)}.")
        bad = sorted(k for k, v in checks.items() if v not in CHECK_ANSWERS)
        if bad:
            raise ReviewError("check_invalid", f"Answer each item with pass, fail or n/a: {', '.join(bad)}.")
        if decision == "approve":
            missing = [item for item in req.checklist if item not in checks]
            failed = [item for item in req.checklist if checks.get(item) == "fail"]
            if missing:
                raise ReviewError(
                    "checklist_incomplete", f"An approval answers every item; missing: {'; '.join(missing)}."
                )
            if failed:
                raise ReviewError(
                    "checklist_failed", f"An item failed, so this cannot be approved: {'; '.join(failed)}."
                )
        elif not comment.strip() and not any(v == "fail" for v in checks.values()):
            raise ReviewError("reason_missing", "A rejection says why: a comment or a failed checklist item.")

    # -- the merge gate ---------------------------------------------------------------

    def gate(self, project: Path | str, branch: str) -> tuple[bool, str, str]:
        """Whether ``branch`` as it is now may merge: (ok, reason, review id)."""
        rid = request_id(project, branch)
        req = self.find(project, branch)
        if req is None:
            return False, f"{branch} has no review; nothing merges without one (`rarus review request`).", rid
        on = ProjectGit(Path(project)).branch()
        if on != req.into:
            return (
                False,
                f"{branch} was reviewed for a merge into {req.into}, and {project} is on {on}; "
                f"switch back to {req.into}, or request a review of the merge into {on}.",
                rid,
            )
        now = req.current_diff_sha()
        verdict = req.latest_verdict(now)
        if verdict is None and now != req.diff_sha:
            return (
                False,
                f"{branch} changed after it was put up for review; the verdict, if any, is of other code "
                f"(`rarus review request` again, then a new verdict).",
                rid,
            )
        if verdict is None:
            return False, f"{branch} has no review verdict yet.", rid
        if verdict["decision"] != "approve":
            return False, f"{verdict['by']} rejected {branch}: {verdict.get('comment') or 'see the checklist'}.", rid
        # Read against the terms as they stand now, which a later request may
        # have tightened: an author added since, a stricter policy, a new item.
        try:
            self.check_actor(req, Actor.parse(str(verdict.get("by", ""))))
        except ReviewError as exc:
            return False, f"the approval by {verdict.get('by')} does not hold under this review's terms: {exc}", rid
        answers = verdict.get("checks") or {}
        unanswered = [item for item in req.checklist if answers.get(item) not in ("pass", "n/a")]
        if unanswered:
            return (
                False,
                f"the approval by {verdict['by']} does not pass every item now on the checklist: "
                f"{'; '.join(unanswered)}.",
                rid,
            )
        return True, f"approved by {verdict['by']}", rid

    def mark_merged(self, rid: str, commit: str) -> None:
        with write_transaction(self._path(rid)):
            req = self.get(rid)
            req.status, req.merged_commit = "merged", commit
            self._write(req)
        self._log("merged", review=rid, commit=commit)


# -- a reviewer agent ------------------------------------------------------------------

Runner = Callable[[str, str, Path], Awaitable[str]]


def agent_prompt(req: ReviewRequest, patch: str) -> str:
    """What a reviewer agent is shown: the diff, the checklist and the answer's shape."""
    shown = patch
    if len(shown) > MAX_DIFF_FOR_AGENT:
        shown = shown[:MAX_DIFF_FOR_AGENT] + f"\n[... diff cut at {MAX_DIFF_FOR_AGENT} characters of {len(patch)}]"
    items = "\n".join(f"- {item}" for item in req.checklist) or "- (no checklist: judge correctness and risk)"
    shape = json.dumps(
        {"decision": "approve|reject", "checks": dict.fromkeys(req.checklist, "pass|fail|n/a"), "comment": "why"}
    )
    return (
        f"You are reviewing a change you did not write: branch {req.branch} into {req.into} "
        f"({len(req.files)} file(s)). Read the diff, answer every checklist item, and decide.\n\n"
        f"Checklist:\n{items}\n\nDiff:\n```diff\n{shown}\n```\n\n"
        f"Reply with one JSON object and nothing else, shaped like: {shape}"
    )


def parse_agent_verdict(reply: str) -> tuple[str, dict[str, str], str] | None:
    """The last JSON object in ``reply`` read as a verdict, or None."""
    for match in reversed(list(re.finditer(r"\{.*\}", reply or "", re.S))):
        try:
            data = json.loads(match.group(0))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("decision") in ("approve", "reject"):
            checks = {str(k): str(v).lower() for k, v in (data.get("checks") or {}).items()}
            return str(data["decision"]), checks, str(data.get("comment") or "")
    return None


async def review_by_agent(book: ReviewBook, rid: str, agent: str, runner: Runner) -> ReviewRequest:
    """Have roster agent ``agent`` review ``rid``; its policy is checked before it is paid to run."""
    req = book.get(rid)
    who = Actor("agent", agent)
    try:
        book.check_actor(req, who)
    except ReviewError as exc:
        book._log("refused", review=rid, by=str(who), decision="", code=exc.code, reason=str(exc))
        raise
    patch, _ = branch_diff(req.project, req.into, req.branch)
    if len(patch) > MAX_DIFF_FOR_AGENT:
        # The prompt would show a cut diff, while the verdict binds the whole
        # one: an approval would cover code nobody read.
        exc = ReviewError(
            "diff_too_large",
            f"The diff is {len(patch)} characters and an agent reviewer is shown at most {MAX_DIFF_FOR_AGENT}, "
            "so an agent cannot judge all of it.",
            "Have a person review it (`rarus review show`, then `rarus review verdict`).",
        )
        book._log("refused", review=rid, by=str(who), decision="", code=exc.code, reason=str(exc))
        raise exc
    reply = await runner(agent, agent_prompt(req, patch), Path(req.workdir))
    parsed = parse_agent_verdict(reply)
    if parsed is None:
        book._log("no_verdict", review=rid, by=str(who), reply=(reply or "")[:500])
        raise ReviewError("no_verdict", f"{agent} answered without a verdict the review can read.")
    decision, checks, comment = parsed
    return book.verdict(rid, who, decision, checks=checks, comment=comment, diff_sha=_sha(patch))


__all__ = [
    "CHECK_ANSWERS",
    "POLICIES",
    "Actor",
    "ReviewBook",
    "ReviewError",
    "ReviewRequest",
    "agent_prompt",
    "branch_diff",
    "parse_agent_verdict",
    "request_id",
    "review_by_agent",
]

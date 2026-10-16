"""The decision log: what was decided, on which assumptions, when to look again, and how it turned out.

Questions waiting on a person live in ``HUMAN_DECISIONS.md``; this is the record
of decisions already taken. Each entry keeps the decision and why, the
assumptions it rests on, and the condition for reviewing it -- a date, a
trigger in words, or both. A review checks each assumption (still holds or no
longer does) and leaves a note; a decision whose assumption failed is marked for
revision. When the result is in, the entry gets its outcome and a verdict, so
the log shows the decision, every review of it and what actually happened, in
that order. Stored as ``.stint/decisions.json`` beside the backlog.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from raven.stint.backlog import STINT_DIR
from raven.utils.atomic_io import atomic_replace

LOG_FILE = "decisions.json"
Status = Literal["active", "needs_revision", "settled"]
Verdict = Literal["good", "mixed", "bad"]


class DecisionLogError(ValueError):
    """An entry or a change the log refuses."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Assumption:
    text: str
    holds: bool | None = None


@dataclass
class Review:
    at: str
    by: str
    note: str
    failed: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    at: str
    by: str
    text: str
    verdict: Verdict


@dataclass
class Decision:
    id: str
    title: str
    decision: str
    rationale: str = ""
    assumptions: list[Assumption] = field(default_factory=list)
    review_when: str = ""
    review_after: str = ""
    status: Status = "active"
    decided_by: str = ""
    decided_at: str = field(default_factory=_now)
    reviews: list[Review] = field(default_factory=list)
    outcome: Outcome | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Decision":
        data = dict(data)
        data["assumptions"] = [Assumption(**a) for a in data.get("assumptions") or []]
        data["reviews"] = [Review(**r) for r in data.get("reviews") or []]
        data["outcome"] = Outcome(**data["outcome"]) if data.get("outcome") else None
        return cls(**data)

    def due(self, today: date) -> bool:
        """Whether this decision should be looked at again now."""
        if self.status == "settled":
            return False
        if self.status == "needs_revision":
            return True
        return bool(self.review_after) and date.fromisoformat(self.review_after) <= today


def log_path(project: Path) -> Path:
    return Path(project) / STINT_DIR / LOG_FILE


def load(project: Path) -> list[Decision]:
    path = log_path(project)
    if not path.is_file():
        return []
    return [Decision.from_dict(row) for row in json.loads(path.read_text(encoding="utf-8"))]


def save(project: Path, decisions: list[Decision]) -> None:
    path = log_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace(path, json.dumps([asdict(d) for d in decisions], indent=2, ensure_ascii=False) + "\n")


def _get(decisions: list[Decision], decision_id: str) -> Decision:
    for entry in decisions:
        if entry.id == decision_id:
            return entry
    raise DecisionLogError(f"no decision {decision_id!r}")


def record(
    project: Path,
    title: str,
    decision: str,
    *,
    assumptions: list[str],
    review_when: str = "",
    review_after: str = "",
    rationale: str = "",
    by: str = "human",
) -> Decision:
    """Log a decision; it must rest on at least one assumption and say when it is to be looked at again."""
    if not title.strip() or not decision.strip():
        raise DecisionLogError("a decision needs a title and what was decided")
    if not [a for a in assumptions if a.strip()]:
        raise DecisionLogError("name at least one assumption the decision rests on")
    if not review_when.strip() and not review_after:
        raise DecisionLogError("say when to review it: --review-when (a condition) or --review-after (a date)")
    if review_after:
        date.fromisoformat(review_after)
    decisions = load(project)
    entry = Decision(
        id=f"D{len(decisions) + 1}",
        title=title.strip(),
        decision=decision.strip(),
        rationale=rationale.strip(),
        assumptions=[Assumption(a.strip()) for a in assumptions if a.strip()],
        review_when=review_when.strip(),
        review_after=review_after,
        decided_by=by,
    )
    decisions.append(entry)
    save(project, decisions)
    return entry


def review(project: Path, decision_id: str, *, note: str, failed: list[int] = (), by: str = "human") -> Decision:
    """Look at a decision again: ``failed`` are the 1-based assumptions that no longer hold."""
    decisions = load(project)
    entry = _get(decisions, decision_id)
    if entry.status == "settled":
        raise DecisionLogError(f"{decision_id} is settled; its outcome is recorded")
    unknown = [i for i in failed if not 1 <= i <= len(entry.assumptions)]
    if unknown:
        raise DecisionLogError(f"{decision_id} has no assumption {unknown[0]}")
    names: list[str] = []
    for index, assumption in enumerate(entry.assumptions, 1):
        assumption.holds = index not in failed
        if not assumption.holds:
            names.append(assumption.text)
    entry.reviews.append(Review(at=_now(), by=by, note=note.strip(), failed=names))
    entry.status = "needs_revision" if names else "active"
    save(project, decisions)
    return entry


def set_outcome(project: Path, decision_id: str, *, text: str, verdict: Verdict, by: str = "human") -> Decision:
    """Record what actually happened; the decision is settled."""
    if verdict not in ("good", "mixed", "bad"):
        raise DecisionLogError("verdict is good, mixed or bad")
    decisions = load(project)
    entry = _get(decisions, decision_id)
    entry.outcome = Outcome(at=_now(), by=by, text=text.strip(), verdict=verdict)
    entry.status = "settled"
    save(project, decisions)
    return entry


def render(entry: Decision) -> str:
    """The decision, every review of it and its outcome, in that order."""
    lines = [
        f"{entry.id} [{entry.status}] {entry.title}",
        f"  decided: {entry.decision} ({entry.decided_at}, {entry.decided_by})",
    ]
    if entry.rationale:
        lines.append(f"  because: {entry.rationale}")
    for index, assumption in enumerate(entry.assumptions, 1):
        state = {None: "unchecked", True: "holds", False: "FAILED"}[assumption.holds]
        lines.append(f"  assumption {index}: {assumption.text} ({state})")
    when = " / ".join(x for x in (entry.review_when, entry.review_after and f"after {entry.review_after}") if x)
    lines.append(f"  review: {when}")
    for r in entry.reviews:
        failed = f"; failed: {', '.join(r.failed)}" if r.failed else ""
        lines.append(f"  reviewed {r.at} by {r.by}: {r.note}{failed}")
    if entry.outcome:
        lines.append(f"  outcome ({entry.outcome.verdict}, {entry.outcome.at}): {entry.outcome.text}")
    return "\n".join(lines)


__all__ = ["Decision", "DecisionLogError", "load", "record", "render", "review", "set_outcome"]

"""Retrospectives: what went well, what went wrong and what is still open.

Built from the records alone, with no model: every line is a fact a record
states (a node failed and why, a check failed in the last round, a question is
unanswered), so a retrospective is reproducible and cannot invent a cause.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from raven.reports.runs import RunEntry


@dataclass
class Retro:
    went_well: list[str] = field(default_factory=list)
    went_wrong: list[str] = field(default_factory=list)
    open_items: list[str] = field(default_factory=list)

    def render(self, heading: str = "", *, level: int = 2) -> str:
        lines = [f"{'#' * level} {heading}", ""] if heading else []
        for title, items in (
            ("What went well", self.went_well),
            ("What went wrong", self.went_wrong),
            ("Still open", self.open_items),
        ):
            lines.append(f"**{title}**")
            lines.extend(f"- {item}" for item in items or ["nothing recorded"])
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def run_retro(entry: RunEntry) -> Retro:
    """The retrospective of one run."""
    r = Retro()
    total = sum(entry.nodes.values())
    done = entry.nodes.get("completed", 0)
    if entry.kind == "dag":
        if total and done == total:
            r.went_well.append(f"all {_plural(total, 'node')} completed")
        elif done:
            r.went_well.append(f"{done} of {_plural(total, 'node')} completed")
        if entry.receipt == "verified":
            r.went_well.append("the run's receipt verifies: the graph, prompts and outputs are as recorded")
        elif entry.receipt == "broken":
            r.went_wrong.append("the run's receipt does not verify: a recorded file changed after the run")
        if entry.nodes.get("skipped"):
            r.went_wrong.append(f"{_plural(entry.nodes['skipped'], 'node')} skipped because what they needed failed")
    if entry.kind == "spawn" and entry.ok:
        r.went_well.append("the sub-agent finished its task")
    for f in entry.failures:
        who = f" ({f['agent']})" if f.get("agent") else ""
        r.went_wrong.append(f"{f['node']}{who} failed: {f['error']}")
    if entry.retried:
        r.went_wrong.append(f"needed another attempt: {', '.join(entry.retried)}")
    if entry.tool_failures:
        r.went_wrong.append(f"{_plural(entry.tool_failures, 'tool call')} failed along the way")
    if entry.kind == "stint":
        x = entry.extra
        rounds = int(x.get("rounds") or 0)
        if rounds:
            r.went_well.append(f"{_plural(rounds, 'round')} run ({x.get('stop_reason') or entry.status})")
        if x.get("integration") == "merged":
            r.went_well.append(f"the branch {x.get('branch') or ''} was merged".replace("  ", " "))
        elif x.get("integration"):
            r.went_wrong.append(f"the branch was not merged ({x['integration']})")
        if x.get("failing_checks"):
            r.went_wrong.append(f"checks failing in the last round: {', '.join(x['failing_checks'])}")
        elif rounds:
            r.went_well.append("no check failed in the last round")
        if x.get("violations"):
            r.went_wrong.append(f"{_plural(int(x['violations']), 'ownership violation')} over the rounds")
        r.open_items.extend(f"unanswered: {q}" for q in x.get("open_questions") or [])
    if entry.status in ("interrupted", "running", "unrecorded"):
        r.open_items.append(f"the run is {entry.status}: it never recorded an end")
    if entry.status == "failed" and entry.failures:
        r.open_items.append(f"rerun or fix: {', '.join(f['node'] for f in entry.failures)}")
    return r


def period_retro(entries: list[RunEntry]) -> Retro:
    """The retrospective of every run in a period."""
    r = Retro()
    finished = [e for e in entries if e.finished]
    ok = [e for e in finished if e.ok]
    if finished:
        r.went_well.append(f"{len(ok)} of {_plural(len(finished), 'finished run')} succeeded")
    verified = sum(1 for e in entries if e.receipt == "verified")
    if verified:
        r.went_well.append(f"{_plural(verified, 'graph run')} with a verified receipt")
    by_agent: Counter[str] = Counter()
    for e in entries:
        for f in e.failures:
            by_agent[f.get("agent") or "(unknown agent)"] += 1
    for agent, n in by_agent.most_common(3):
        r.went_wrong.append(f"{agent}: {_plural(n, 'failed node')}")
    broken = [e.id for e in entries if e.receipt == "broken"]
    if broken:
        r.went_wrong.append(f"receipts that do not verify: {', '.join(broken)}")
    retried = sum(len(e.retried) for e in entries)
    if retried:
        r.went_wrong.append(f"{_plural(retried, 'node')} needed another attempt")
    for e in entries:
        if e.status in ("interrupted", "running", "unrecorded"):
            r.open_items.append(f"{e.kind} {e.id} is {e.status}")
        for q in e.extra.get("open_questions") or []:
            r.open_items.append(f"stint {e.id}: unanswered: {q}")
        if e.extra.get("integration") and e.extra["integration"] != "merged":
            r.open_items.append(f"stint {e.id}: branch not merged ({e.extra['integration']})")
    return r


__all__ = ["Retro", "period_retro", "run_retro"]

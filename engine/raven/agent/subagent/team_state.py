"""The conversation's team as facts, for what a compaction keeps verbatim.

A summary model restates what the transcript says, and what it says about the
team is history: "I spawned the reviewer" is not "the reviewer is still
running". After a compaction the agent acts on its brief, so the brief carries
the team as it is at that moment, read from where Raven keeps it: the runs in
flight (the manager's live index), the instances the conversation can continue
(the instance registry) and the mail waiting to be read (the conversation's
mailbox).
"""

from __future__ import annotations

from typing import Any

_INSTANCES_SHOWN = 12


def team_brief(session_key: str, manager: Any | None = None) -> str:
    """The block, or "" when the conversation has no team to speak of."""
    lines: list[str] = []
    running: set[tuple[str, str]] = set()
    if manager is not None:
        try:
            running = set(manager.live_handles(session_key))
        except Exception:  # noqa: BLE001 -- a manager mid-swap reads as no runs
            running = set()
    if running:
        lines.append("Running now: " + ", ".join(a if h == a else f"{a} ({h})" for a, h in sorted(running)))

    from raven.agent.subagent.instances import get_registry

    rows = [r for r in get_registry().list_instances(session_key) if (r.get("agent"), r.get("handle")) not in running]
    if rows:
        shown = ", ".join(
            f"{r.get('agent')}/{r.get('handle')} [{r.get('status') or 'idle'}]" for r in rows[:_INSTANCES_SHOWN]
        )
        more = f" and {len(rows) - _INSTANCES_SHOWN} more" if len(rows) > _INSTANCES_SHOWN else ""
        lines.append(f"Instances this conversation can continue (instance=<handle>): {shown}{more}")

    from raven.agent.subagent.mailbox import Mailbox

    box = Mailbox(session_key)
    waiting = []
    for name in sorted(box.members()):
        count = len(box.unread(name))
        if count:
            waiting.append(f"{name} {count}")
    if waiting:
        lines.append("Agent mail unread: " + ", ".join(waiting))

    if not lines:
        return ""
    return "[The team when the context was compacted -- current facts, not part of the summary]\n" + "\n".join(
        f"- {line}" for line in lines
    )


__all__ = ["team_brief"]

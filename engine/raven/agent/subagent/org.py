"""The reporting structure of the agent roster: who manages whom, in which department.

A chart is a forest: every member reports to at most one manager, and a member
without a manager is a head. A manager may assign work to anyone below it, at any
depth, and to nobody else; what a member may look at is its own line of command
and everything below it. The chart is plain data built from the ``subagents.org``
config rows, so a roster that configures none behaves exactly as before.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OrgMember:
    name: str
    reports_to: str | None = None
    department: str = ""
    title: str = ""


class OrgChartError(ValueError):
    """The rows do not make a forest."""


class OrgChart:
    """An immutable, validated reporting structure."""

    def __init__(self, members: Iterable[OrgMember] = ()) -> None:
        by_name: dict[str, OrgMember] = {}
        for member in members:
            if member.name in by_name:
                raise OrgChartError(f"{member.name!r} appears twice in the org chart")
            by_name[member.name] = member
        for member in by_name.values():
            if member.reports_to is None:
                continue
            if member.reports_to == member.name:
                raise OrgChartError(f"{member.name!r} cannot report to itself")
            if member.reports_to not in by_name:
                raise OrgChartError(f"{member.name!r} reports to {member.reports_to!r}, who is not in the chart")
        self._members = by_name
        self._children: dict[str, list[str]] = {name: [] for name in by_name}
        for member in by_name.values():
            if member.reports_to is not None:
                self._children[member.reports_to].append(member.name)
        for name in by_name:
            seen = {name}
            current = by_name[name].reports_to
            while current is not None:
                if current in seen:
                    raise OrgChartError(f"{name!r} is part of a reporting cycle")
                seen.add(current)
                current = by_name[current].reports_to

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any] | Any]) -> "OrgChart":
        members = []
        for row in rows:
            get = row.get if isinstance(row, Mapping) else lambda key, default=None, _r=row: getattr(_r, key, default)
            members.append(
                OrgMember(
                    name=str(get("name")),
                    reports_to=get("reports_to") or None,
                    department=str(get("department") or ""),
                    title=str(get("title") or ""),
                )
            )
        return cls(members)

    def __bool__(self) -> bool:
        return bool(self._members)

    def __contains__(self, name: object) -> bool:
        return name in self._members

    def members(self) -> list[OrgMember]:
        return sorted(self._members.values(), key=lambda m: m.name)

    def manager_of(self, name: str) -> str | None:
        member = self._members.get(name)
        return member.reports_to if member else None

    def direct_reports(self, name: str) -> list[str]:
        return sorted(self._children.get(name, []))

    def subordinates(self, name: str) -> list[str]:
        """Everyone below ``name``, at any depth."""
        out: list[str] = []
        stack = list(self._children.get(name, []))
        while stack:
            current = stack.pop()
            out.append(current)
            stack.extend(self._children[current])
        return sorted(out)

    def chain_of_command(self, name: str) -> list[str]:
        """The managers above ``name``, nearest first."""
        chain: list[str] = []
        current = self.manager_of(name)
        while current is not None:
            chain.append(current)
            current = self.manager_of(current)
        return chain

    def department_of(self, name: str) -> str:
        member = self._members.get(name)
        return member.department if member else ""

    def department_members(self, department: str) -> list[str]:
        return sorted(m.name for m in self._members.values() if m.department == department)

    def departments(self) -> list[str]:
        return sorted({m.department for m in self._members.values() if m.department})

    def may_assign(self, actor: str, target: str) -> bool:
        return target in self.subordinates(actor)

    def refusal_for_assign(self, actor: str, target: str) -> str | None:
        """Why ``actor`` may not hand work to ``target``, or ``None`` when it may."""
        if not self:
            return "no org chart is configured (subagents.org is empty)"
        if actor not in self:
            return f"{actor!r} is not in the org chart"
        if target not in self:
            return f"{target!r} is not in the org chart"
        if actor == target:
            return f"{actor!r} cannot assign work to itself"
        if self.may_assign(actor, target):
            return None
        if actor in self.subordinates(target):
            return f"{actor!r} reports to {target!r}; work is assigned from above, not upward"
        return f"{target!r} is not below {actor!r} in the reporting line"

    def visible_to(self, actor: str) -> list[str]:
        """What ``actor`` may see: itself, its chain of command and everything below it."""
        if actor not in self:
            return []
        return sorted({actor, *self.chain_of_command(actor), *self.subordinates(actor)})


__all__ = ["OrgChart", "OrgChartError", "OrgMember"]

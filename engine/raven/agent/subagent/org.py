"""The reporting structure of the agent roster: who manages whom, in which department.

A chart is a forest: every member reports to at most one manager, and a member
without a manager is a head. A manager may assign work to anyone below it, at any
depth, and to nobody else; what a member may look at is its own line of command
and everything below it. The chart is plain data built from the ``subagents.org``
config rows, so a roster that configures none behaves exactly as before.

Members can also be grouped into companies and their departments
(``subagents.companies``, drawn on the agent canvas). Inside a company the rule is who
talks to whom rather than who is above whom: members of one department talk to each
other; members at the company level, the company head and the department heads talk to
each other; nobody talks across companies; a frozen company takes no work at all.
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
    company: str = ""


@dataclass(frozen=True)
class OrgCompany:
    id: str
    name: str
    head: str = ""
    frozen: bool = False
    #: Department name to its head ("" when it has none).
    departments: tuple[tuple[str, str], ...] = ()

    def department_head(self, department: str) -> str:
        return dict(self.departments).get(department, "")


class OrgChartError(ValueError):
    """The rows do not make a forest."""


class OrgChart:
    """An immutable, validated reporting structure."""

    def __init__(self, members: Iterable[OrgMember] = (), companies: Iterable[OrgCompany] = ()) -> None:
        self._companies = {c.id: c for c in companies}
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
    def from_rows(
        cls, rows: Iterable[Mapping[str, Any] | Any], companies: Iterable[Mapping[str, Any] | Any] = ()
    ) -> "OrgChart":
        members = []
        for row in rows:
            get = _getter(row)
            members.append(
                OrgMember(
                    name=str(get("name")),
                    reports_to=get("reports_to") or None,
                    department=str(get("department") or ""),
                    title=str(get("title") or ""),
                    company=str(get("company") or ""),
                )
            )
        groups = []
        for raw in companies:
            get = _getter(raw)
            departments = tuple(
                (str(_getter(d)("name") or ""), str(_getter(d)("head") or "")) for d in (get("departments") or ())
            )
            groups.append(
                OrgCompany(
                    id=str(get("id") or ""),
                    name=str(get("name") or ""),
                    head=str(get("head") or ""),
                    frozen=bool(get("frozen")),
                    departments=departments,
                )
            )
        return cls(members, groups)

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

    def company_of(self, name: str) -> OrgCompany | None:
        member = self._members.get(name)
        return self._companies.get(member.company) if member and member.company else None

    def companies(self) -> list[OrgCompany]:
        return sorted(self._companies.values(), key=lambda c: c.name)

    def _outward(self, member: OrgMember, company: OrgCompany) -> bool:
        """At the company level, its head, or a department head: the ones who talk across departments."""
        if not member.department or company.head == member.name:
            return True
        return company.department_head(member.department) == member.name

    def may_talk(self, a: str, b: str) -> bool:
        """Whether two company members may hand each other work (the module docstring has the rule)."""
        ma, mb = self._members.get(a), self._members.get(b)
        if ma is None or mb is None or not ma.company or ma.company != mb.company:
            return False
        company = self._companies.get(ma.company)
        if company is None:
            return False
        return ma.department == mb.department or (self._outward(ma, company) and self._outward(mb, company))

    def talks_with(self, name: str) -> list[str]:
        """Everyone ``name`` may hand work to inside its company."""
        return sorted(m for m in self._members if m != name and self.may_talk(name, m))

    def refusal_for_target(self, target: str) -> str | None:
        """Why nobody may hand ``target`` work at all right now (its company is frozen), or ``None``."""
        company = self.company_of(target)
        if company is not None and company.frozen:
            return f"{target!r} works in {company.name!r}, which is frozen; unfreeze it on the agent canvas first"
        return None

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
        if (frozen := self.refusal_for_target(target)) is not None:
            return frozen
        actor_company, target_company = self.company_of(actor), self.company_of(target)
        if actor_company is not None or target_company is not None:
            if actor_company is None or target_company is None or actor_company.id != target_company.id:
                return f"{actor!r} and {target!r} are not in one company; work does not cross companies"
            if self.may_talk(actor, target):
                return None
            where = self._members[target].department or self._members[actor].department
            head = actor_company.department_head(where)
            via = f" -- go through its head, {head!r}" if head else " -- it has no head yet to carry work across"
            return f"{target!r} and {actor!r} are not in one department of {actor_company.name!r}{via}"
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


def _getter(row: Mapping[str, Any] | Any) -> Any:
    if isinstance(row, Mapping):
        return row.get
    return lambda key, default=None, _r=row: getattr(_r, key, default)


__all__ = ["OrgChart", "OrgChartError", "OrgCompany", "OrgMember"]

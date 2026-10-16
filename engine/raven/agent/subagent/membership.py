"""Joining, stopping and removing roster agents through requests a leader approves.

A change to who is on the team does not happen when it is asked for. It is
filed as a request -- ``join`` (with the agent's config row), ``shutdown`` (the
agent stays listed but disabled) or ``remove`` -- and applied only when the
team's leader approves it. Who is acting is an identity the caller states
(``--as`` on the command line, ``RAVEN_TEAM_IDENTITY`` for a process an agent
runs); an approval or rejection from anyone but the leader is refused, and so is
a decision on a request that is no longer pending. The ledger keeps every
request and its outcome.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from raven.utils.atomic_io import atomic_replace

Kind = Literal["join", "shutdown", "remove"]
Status = Literal["pending", "approved", "rejected"]
IDENTITY_ENV = "RAVEN_TEAM_IDENTITY"
DEFAULT_LEADER = "owner"


class MembershipError(ValueError):
    """A request or decision the protocol refuses."""


@dataclass
class MembershipRequest:
    id: str
    kind: Kind
    agent: str
    requested_by: str
    reason: str = ""
    entry: dict[str, Any] = field(default_factory=dict)
    status: Status = "pending"
    decided_by: str = ""
    decided_at: float = 0.0
    note: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class RosterOps:
    """How an approved request changes the roster; the config's own write path by default."""

    add: Callable[[dict[str, Any]], None]
    remove: Callable[[str], bool]
    set_enabled: Callable[[str, bool], None]
    names: Callable[[], list[str]]

    @classmethod
    def from_config(cls, config_path: Path | None = None) -> "RosterOps":
        from raven.config import update_subagents as roster

        def set_enabled(name: str, enabled: bool) -> None:
            rows = roster.get_agents(config_path=config_path)
            for row in rows:
                if row.get("name") == name:
                    row["enabled"] = enabled
                    break
            else:
                raise MembershipError(f"{name!r} is not a configured agent")
            roster.set_agents(rows, config_path=config_path)

        return cls(
            add=lambda entry: roster.add_agent(entry, config_path=config_path),
            remove=lambda name: roster.remove_agent(name, config_path=config_path),
            set_enabled=set_enabled,
            names=lambda: [str(r.get("name")) for r in roster.get_agents(config_path=config_path)],
        )


def acting_identity(explicit: str | None = None) -> str:
    """Who is acting: the stated identity, else the environment's, else nobody."""
    identity = (explicit or os.environ.get(IDENTITY_ENV) or "").strip()
    if not identity:
        raise MembershipError(f"say who is acting (--as NAME or {IDENTITY_ENV})")
    return identity


class MembershipLedger:
    """The requests, on disk, and the protocol that decides them."""

    def __init__(self, path: Path, roster: RosterOps, *, leader: str = DEFAULT_LEADER) -> None:
        self.path = Path(path)
        self.roster = roster
        self.leader = leader

    def _read(self) -> list[MembershipRequest]:
        if not self.path.is_file():
            return []
        rows = json.loads(self.path.read_text(encoding="utf-8")).get("requests", [])
        return [MembershipRequest(**row) for row in rows]

    def _write(self, requests: list[MembershipRequest]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {"leader": self.leader, "requests": [asdict(r) for r in requests]}
        atomic_replace(self.path, json.dumps(body, ensure_ascii=False, indent=2))

    def requests(self, status: Status | None = None) -> list[MembershipRequest]:
        return [r for r in self._read() if status is None or r.status == status]

    def get(self, request_id: str) -> MembershipRequest:
        for request in self._read():
            if request.id == request_id:
                return request
        raise MembershipError(f"no request {request_id!r}")

    def request(
        self,
        kind: Kind,
        agent: str,
        *,
        requested_by: str,
        reason: str = "",
        entry: dict[str, Any] | None = None,
    ) -> MembershipRequest:
        """File a request; nothing on the roster changes until it is approved."""
        agent = agent.strip()
        if not agent:
            raise MembershipError("name the agent")
        if kind == "join":
            entry = dict(entry or {})
            if entry.get("name", agent) != agent:
                raise MembershipError("the config row must carry the agent's own name")
            entry["name"] = agent
            if agent in self.roster.names():
                raise MembershipError(f"{agent!r} is already on the roster")
        elif agent not in self.roster.names():
            raise MembershipError(f"{agent!r} is not on the roster")
        requests = self._read()
        if any(r.status == "pending" and r.agent == agent and r.kind == kind for r in requests):
            raise MembershipError(f"a {kind} request for {agent!r} is already pending")
        filed = MembershipRequest(
            id=uuid.uuid4().hex[:8],
            kind=kind,
            agent=agent,
            requested_by=requested_by,
            reason=reason,
            entry=entry or {},
        )
        requests.append(filed)
        self._write(requests)
        return filed

    def _decide(self, request_id: str, approver: str) -> tuple[list[MembershipRequest], MembershipRequest]:
        if approver != self.leader:
            raise MembershipError(f"only the leader ({self.leader}) may decide membership; {approver} is not")
        requests = self._read()
        for request in requests:
            if request.id == request_id:
                if request.status != "pending":
                    raise MembershipError(f"request {request_id} is already {request.status}")
                return requests, request
        raise MembershipError(f"no request {request_id!r}")

    def approve(self, request_id: str, *, approver: str, note: str = "") -> MembershipRequest:
        """The leader approves: the change is applied to the roster, then recorded."""
        requests, request = self._decide(request_id, approver)
        if request.kind == "join":
            self.roster.add({**request.entry, "enabled": True})
        elif request.kind == "shutdown":
            self.roster.set_enabled(request.agent, False)
        elif not self.roster.remove(request.agent):
            raise MembershipError(f"{request.agent!r} could not be removed")
        request.status, request.decided_by, request.decided_at, request.note = "approved", approver, time.time(), note
        self._write(requests)
        return request

    def reject(self, request_id: str, *, approver: str, note: str = "") -> MembershipRequest:
        requests, request = self._decide(request_id, approver)
        request.status, request.decided_by, request.decided_at, request.note = "rejected", approver, time.time(), note
        self._write(requests)
        return request


def default_ledger(config_path: Path | None = None) -> MembershipLedger:
    """The ledger beside the config, deciding against its roster and its leader."""
    from raven.config.loader import load_config
    from raven.home import raven_home

    leader = load_config(config_path).subagents.leader
    return MembershipLedger(
        raven_home() / "team" / "membership.json", RosterOps.from_config(config_path), leader=leader
    )


__all__ = [
    "DEFAULT_LEADER",
    "IDENTITY_ENV",
    "MembershipError",
    "MembershipLedger",
    "MembershipRequest",
    "RosterOps",
    "acting_identity",
    "default_ledger",
]

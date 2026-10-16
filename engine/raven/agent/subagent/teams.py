"""Several teams (projects) in one Raven, each with its own sub-agent limits.

A team is a named set of conversations, matched by their session keys
(``fnmatch`` patterns such as ``web:acme-*``), with its own dispatch budget per
rolling hour and its own cap on sub-agents in flight. One dispatch loop serves
them all -- the manager's process-wide gate still bounds the host -- but a team
spending its budget or filling its slots refuses or queues only its own work.
A conversation no team claims runs under the host's limits alone, as before.
"""

from __future__ import annotations

import asyncio
import fnmatch
import time
from collections import deque
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

WINDOW_SECONDS = 3600.0


@dataclass(frozen=True)
class Team:
    name: str
    sessions: tuple[str, ...]
    max_spawns_per_hour: int | None = None
    max_concurrent: int | None = None

    def claims(self, session_key: str) -> bool:
        return any(fnmatch.fnmatchcase(session_key, pattern) for pattern in self.sessions)


class TeamTable:
    """Resolves a conversation to its team and holds each team's budget and slots."""

    def __init__(self, teams: Iterable[Team] = (), *, clock: Any = time.monotonic) -> None:
        self.teams: list[Team] = []
        seen: set[str] = set()
        for team in teams:
            if team.name in seen:
                raise ValueError(f"team {team.name!r} is declared twice")
            seen.add(team.name)
            self.teams.append(team)
        self._clock = clock
        self._windows: dict[str, deque[float]] = {t.name: deque() for t in self.teams}
        self._slots: dict[str, asyncio.Semaphore] = {
            t.name: asyncio.Semaphore(t.max_concurrent) for t in self.teams if t.max_concurrent
        }
        self._in_flight: dict[str, int] = {t.name: 0 for t in self.teams}

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any] | Any]) -> "TeamTable":
        teams = []
        for row in rows:
            get = row.get if isinstance(row, Mapping) else lambda key, default=None, _r=row: getattr(_r, key, default)
            teams.append(
                Team(
                    name=str(get("name")),
                    sessions=tuple(get("sessions") or ()),
                    max_spawns_per_hour=get("max_spawns_per_hour"),
                    max_concurrent=get("max_concurrent"),
                )
            )
        return cls(teams)

    def __bool__(self) -> bool:
        return bool(self.teams)

    def team_of(self, session_key: str | None) -> Team | None:
        """The first team that claims the conversation, in declaration order."""
        key = session_key or "default"
        return next((team for team in self.teams if team.claims(key)), None)

    def _window(self, team: Team) -> deque[float]:
        window = self._windows[team.name]
        cutoff = self._clock() - WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        return window

    def charge(self, session_key: str | None) -> str | None:
        """Charge one dispatch to the conversation's team; the refusal, or None."""
        team = self.team_of(session_key)
        if team is None or team.max_spawns_per_hour is None:
            return None
        window = self._window(team)
        if len(window) >= team.max_spawns_per_hour:
            return (
                f"team {team.name!r} hit its sub-agent dispatch limit ({team.max_spawns_per_hour} per hour); "
                "other teams are not affected"
            )
        window.append(self._clock())
        return None

    @asynccontextmanager
    async def slot(self, session_key: str | None) -> AsyncIterator[None]:
        """Hold one of the team's in-flight slots for the dispatch, or nothing for an unclaimed conversation."""
        team = self.team_of(session_key)
        if team is None:
            yield
            return
        gate = self._slots.get(team.name)
        if gate is not None:
            await gate.acquire()
        self._in_flight[team.name] += 1
        try:
            yield
        finally:
            self._in_flight[team.name] -= 1
            if gate is not None:
                gate.release()

    def usage(self) -> list[dict[str, Any]]:
        """One row per team: its limits and what it is using now -- the shared dashboard's data."""
        return [
            {
                "team": team.name,
                "sessions": list(team.sessions),
                "dispatches_last_hour": len(self._window(team)),
                "max_spawns_per_hour": team.max_spawns_per_hour,
                "in_flight": self._in_flight[team.name],
                "max_concurrent": team.max_concurrent,
            }
            for team in self.teams
        ]


__all__ = ["Team", "TeamTable"]

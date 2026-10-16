"""The idle sweep: putting agent connections nobody is using to sleep.

An ACP agent is a process Raven keeps open between turns so the next one starts
fast. One that sat idle for long holds memory and, for a hosted agent, a seat
for nothing. The sweep runs on an interval and closes every pooled connection
idle past the threshold (never one with a turn in flight); the agent wakes by
itself on the next request, because the pool launches a connection it does not
hold.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger


@dataclass
class IdleSweeper:
    pool: Callable[[], Any]
    idle_after_s: float
    interval_s: float = 60.0
    _task: asyncio.Task | None = None

    async def sweep(self) -> list[str]:
        return await self.pool().hibernate_idle(self.idle_after_s)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.sweep()
            except Exception as exc:  # noqa: BLE001 - one failed sweep must not end the next
                logger.warning("idle sweep failed: {}", exc)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="acp-idle-sweep")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


__all__ = ["IdleSweeper"]

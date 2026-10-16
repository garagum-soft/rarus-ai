"""Guarding the host: disk, memory and CPU thresholds that stop the agents and tell the owner.

A guard samples the machine on an interval. When a reading crosses its
threshold -- free disk below a share or an amount, memory or CPU above a
share -- the guard tells the owner what crossed and stops the agents: every
running sub-agent is cancelled and delegation is paused, so nothing new starts
on a machine that is out of room. It acts once per breach; when every reading is
back inside its threshold it says so and re-arms, and resuming delegation is
left to the owner.

Readings come from the standard library and the platform itself (``/proc`` and
``getloadavg`` on POSIX, ``GlobalMemoryStatusEx`` and ``GetSystemTimes`` on
Windows), so the guard adds no dependency; a reading the platform cannot give
is skipped rather than guessed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from raven.proactive_engine import attention

Notify = Callable[[str], Awaitable[None] | None]
StopAgents = Callable[[str], Awaitable[None] | None]


@dataclass(frozen=True)
class HostSample:
    disk_free_ratio: float
    disk_free_gb: float
    memory_used_ratio: float | None
    cpu_used_ratio: float | None


@dataclass(frozen=True)
class Thresholds:
    min_disk_free_ratio: float | None = 0.05
    min_disk_free_gb: float | None = None
    max_memory_ratio: float | None = 0.95
    max_cpu_ratio: float | None = None

    def breaches(self, sample: HostSample) -> list[str]:
        found: list[str] = []
        if self.min_disk_free_ratio is not None and sample.disk_free_ratio < self.min_disk_free_ratio:
            found.append(f"disk free {sample.disk_free_ratio:.1%} is below {self.min_disk_free_ratio:.1%}")
        if self.min_disk_free_gb is not None and sample.disk_free_gb < self.min_disk_free_gb:
            found.append(f"disk free {sample.disk_free_gb:.1f} GB is below {self.min_disk_free_gb:.1f} GB")
        if (
            self.max_memory_ratio is not None
            and sample.memory_used_ratio is not None
            and sample.memory_used_ratio > self.max_memory_ratio
        ):
            found.append(f"memory used {sample.memory_used_ratio:.1%} is above {self.max_memory_ratio:.1%}")
        if (
            self.max_cpu_ratio is not None
            and sample.cpu_used_ratio is not None
            and sample.cpu_used_ratio > self.max_cpu_ratio
        ):
            found.append(f"CPU used {sample.cpu_used_ratio:.1%} is above {self.max_cpu_ratio:.1%}")
        return found


def _memory_used_ratio() -> float | None:
    if sys.platform == "win32":
        import ctypes

        class _Status(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _Status()
        status.dwLength = ctypes.sizeof(_Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return None
        return 1.0 - status.ullAvailPhys / status.ullTotalPhys if status.ullTotalPhys else None
    try:
        fields = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            name, _, rest = line.partition(":")
            fields[name] = int(rest.split()[0])
        return 1.0 - fields["MemAvailable"] / fields["MemTotal"]
    except (OSError, KeyError, ValueError, IndexError):
        return None


def _cpu_used_ratio(window_s: float = 0.25) -> float | None:
    if sys.platform == "win32":
        import ctypes

        def times() -> tuple[int, int, int]:
            idle, kernel, user = (ctypes.c_ulonglong() for _ in range(3))
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))  # type: ignore[attr-defined]
            return idle.value, kernel.value, user.value

        i0, k0, u0 = times()
        time.sleep(window_s)
        i1, k1, u1 = times()
        busy_total = (k1 - k0) + (u1 - u0)
        return (busy_total - (i1 - i0)) / busy_total if busy_total else None
    try:
        return min(1.0, os.getloadavg()[0] / (os.cpu_count() or 1))
    except (AttributeError, OSError):
        return None


def measure(path: Path) -> HostSample:
    usage = shutil.disk_usage(path)
    return HostSample(
        disk_free_ratio=usage.free / usage.total if usage.total else 1.0,
        disk_free_gb=usage.free / 1024**3,
        memory_used_ratio=_memory_used_ratio(),
        cpu_used_ratio=_cpu_used_ratio(),
    )


async def _call(fn: Callable[[str], Awaitable[None] | None], text: str) -> None:
    result = fn(text)
    if asyncio.iscoroutine(result):
        await result


@dataclass
class HostGuard:
    thresholds: Thresholds
    notify: Notify
    stop_agents: StopAgents
    sampler: Callable[[], HostSample]
    interval_s: float = 60.0
    tripped: bool = False
    history: list[str] = field(default_factory=list)
    _task: asyncio.Task | None = None

    async def check(self) -> list[str]:
        """One reading: act on a new breach, re-arm on recovery. Returns the breaches found."""
        sample = await asyncio.to_thread(self.sampler)
        breaches = self.thresholds.breaches(sample)
        if breaches and not self.tripped:
            self.tripped = True
            reason = "; ".join(breaches)
            text = f"Host resources are past their limits ({reason}). Raven stopped its agents and paused delegation."
            self.history.append(text)
            logger.warning("host guard: {}", reason)
            await _call(self.stop_agents, reason)
            await asyncio.to_thread(
                attention.raise_quietly,
                "host_guard",
                kind="host_guard",
                priority="critical",
                title="Host resources past their limits; agents stopped",
                detail=reason,
                source="host_guard",
            )
            await _call(self.notify, text)
        elif not breaches and self.tripped:
            self.tripped = False
            text = "Host resources are back within their limits. Delegation stays paused until you resume it."
            self.history.append(text)
            await asyncio.to_thread(attention.resolve_quietly, "host_guard", "recovered")
            await asyncio.to_thread(
                attention.raise_quietly,
                "host_guard:paused",
                kind="host_guard",
                priority="high",
                title="Host resources recovered; delegation is still paused",
                detail="Resume delegation when you are ready, then dismiss this.",
                source="host_guard",
            )
            await _call(self.notify, text)
        return breaches

    async def _loop(self) -> None:
        while True:
            try:
                await self.check()
            except Exception as exc:  # noqa: BLE001 - a failed reading must not end the guard
                logger.warning("host guard: could not read the host: {}", exc)
            await asyncio.sleep(self.interval_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="host-guard")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


__all__ = ["HostGuard", "HostSample", "Thresholds", "measure"]

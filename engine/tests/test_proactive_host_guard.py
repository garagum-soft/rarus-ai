"""The host guard: past a disk, memory or CPU threshold it stops the agents and tells the owner."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.config.schema import Config, HostGuardConfig
from raven.core.proactive_stack import build_host_guard
from raven.proactive_engine.host_guard import HostGuard, HostSample, Thresholds, measure
from raven.proactive_engine.system_events import SystemEventQueue

HEALTHY = HostSample(disk_free_ratio=0.5, disk_free_gb=200.0, memory_used_ratio=0.4, cpu_used_ratio=0.2)
DISK_FULL = HostSample(disk_free_ratio=0.02, disk_free_gb=3.0, memory_used_ratio=0.4, cpu_used_ratio=0.2)


def test_each_threshold_names_what_crossed() -> None:
    limits = Thresholds(min_disk_free_ratio=0.05, min_disk_free_gb=10, max_memory_ratio=0.9, max_cpu_ratio=0.8)

    assert limits.breaches(HEALTHY) == []
    found = limits.breaches(HostSample(0.01, 1.0, 0.95, 0.99))
    assert [b.split()[0] for b in found] == ["disk", "disk", "memory", "CPU"]
    assert Thresholds(max_memory_ratio=0.5).breaches(HostSample(0.5, 9, None, None)) == [], (
        "an unknown reading is skipped"
    )


class _Owner:
    def __init__(self) -> None:
        self.told: list[str] = []
        self.stopped: list[str] = []

    async def notify(self, text: str) -> None:
        self.told.append(text)

    async def stop(self, reason: str) -> None:
        self.stopped.append(reason)


async def test_a_breach_stops_the_agents_and_tells_the_owner_once() -> None:
    readings = [DISK_FULL, DISK_FULL, HEALTHY, DISK_FULL]
    owner = _Owner()
    guard = HostGuard(Thresholds(), notify=owner.notify, stop_agents=owner.stop, sampler=lambda: readings.pop(0))

    await guard.check()
    await guard.check()
    assert len(owner.stopped) == 1 and "disk free 2.0%" in owner.stopped[0]
    assert len(owner.told) == 1 and "stopped its agents" in owner.told[0]

    await guard.check()
    assert "back within" in owner.told[-1] and not guard.tripped

    await guard.check()
    assert len(owner.stopped) == 2, "a new breach after recovery acts again"


async def test_a_healthy_host_is_left_alone() -> None:
    owner = _Owner()
    guard = HostGuard(Thresholds(), notify=owner.notify, stop_agents=owner.stop, sampler=lambda: HEALTHY)
    assert await guard.check() == []
    assert owner.told == [] and owner.stopped == []


def test_this_machine_can_be_measured(tmp_path: Path) -> None:
    sample = measure(tmp_path)
    assert 0.0 <= sample.disk_free_ratio <= 1.0 and sample.disk_free_gb >= 0
    if sample.memory_used_ratio is not None:
        assert 0.0 <= sample.memory_used_ratio <= 1.0
    if sample.cpu_used_ratio is not None:
        assert 0.0 <= sample.cpu_used_ratio <= 1.0


class _Manager:
    def __init__(self) -> None:
        self.paused = False
        self.cancelled: list[str] = []

    def set_paused(self, paused: bool) -> bool:
        self.paused = paused
        return paused

    async def cancel_all(self, *, reason: str) -> int:
        self.cancelled.append(reason)
        return 3


def _config(tmp_path: Path, **guard: Any) -> Config:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.gateway.host_guard = HostGuardConfig(**guard)
    return cfg


def test_the_guard_is_off_unless_configured(tmp_path: Path) -> None:
    assert build_host_guard(_config(tmp_path), subagents=_Manager, system_events=None, wake=None) is None


async def test_the_gateway_guard_pauses_and_cancels_and_tells_the_owner_through_the_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    manager = _Manager()
    events = SystemEventQueue()
    woken: list[str] = []
    guard = build_host_guard(
        _config(tmp_path, enabled=True, min_disk_free_percent=99.999),
        subagents=lambda: manager,
        system_events=events,
        wake=SimpleNamespace(request_wake_now=woken.append),
    )

    breaches = await guard.check()

    assert breaches and "disk free" in breaches[0]
    assert manager.paused and manager.cancelled and "host guard" in manager.cancelled[0]
    queued = events.peek_all()
    assert [e.source for e in queued] == ["host_guard"] and "stopped its agents" in queued[0].text
    assert woken == ["host_guard"]
    assert "host_guard Host resources are past their limits" in (tmp_path / "home" / "notifications.log").read_text(
        encoding="utf-8"
    )

"""Scheduled plugin hooks: a plugin's code runs at the times its manifest names.

A ``[[plugin.contributes.scheduled]]`` entry carries a cron expression and a
timezone. The host keeps one durable row per hook in
``<RARUS_HOME>/plugin_schedules.json`` and a resident host's scheduler fires
the hook when its row comes due. After Multica's plugin hook schedules:

- **Reconciled at activation.** A hook whose cron and timezone are unchanged
  keeps its row, its generation and its next run -- a code-only upgrade does not
  reset the clock. A changed schedule, a new hook, or a hook whose plugin comes
  back after being off starts a new *generation* with its next run computed
  from now. A hook whose plugin is no longer active (disabled, denied, removed)
  is switched off and has no next run.
- **Missed occurrences coalesce.** A host that was down across several
  occurrences fires once, for the latest one that is due, and says how many it
  stands for (``missed = "latest"``); ``"skip"`` drops a late occurrence and
  waits for the next.
- **At most once per occurrence.** A firing is claimed before it runs, under
  the state file's lock, with a delivery id made of the hook, its generation and
  the planned time; a second scheduler (another process on the same home) or a
  repeated tick finds it already claimed. A hook that hangs is cut off at its
  ``timeout_s``.

Every firing, skip and failure is appended to ``plugin_schedule_runs.jsonl``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from croniter import croniter

from raven.utils.atomic_io import atomic_replace, locked_append, write_transaction

logger = logging.getLogger(__name__)

LATE_GRACE_MS = 60_000
"""How late an occurrence may fire under ``missed = "skip"`` and still count as on time."""
MAX_COALESCE = 100_000
KEEP_DELIVERED = 50


@dataclass(frozen=True)
class ScheduledFire:
    """What a scheduled hook is told when it fires."""

    key: str
    plugin_id: str
    name: str
    planned_ms: int
    generation: str
    delivery_id: str
    coalesced: int = 1
    """How many due occurrences this one firing stands for (1 when on time)."""
    manual: bool = False

    @property
    def planned_at(self) -> datetime:
        return datetime.fromtimestamp(self.planned_ms / 1000).astimezone()


class ScheduledHook(Protocol):
    async def run(self, fire: ScheduledFire) -> str | None: ...


def next_after(cron: str, timezone: str, after_ms: int) -> int:
    """The first occurrence of ``cron`` in ``timezone`` strictly after ``after_ms``."""
    base = datetime.fromtimestamp(after_ms / 1000, tz=ZoneInfo(timezone))
    return int(croniter(cron, base).get_next(datetime).timestamp() * 1000)


def latest_due(cron: str, timezone: str, first_ms: int, until_ms: int) -> tuple[int, int]:
    """The latest occurrence in ``[first_ms, until_ms]`` and how many there are (``first_ms`` is one)."""
    plan, count = first_ms, 1
    it = croniter(cron, datetime.fromtimestamp(first_ms / 1000, tz=ZoneInfo(timezone)))
    while count < MAX_COALESCE:
        nxt = int(it.get_next(datetime).timestamp() * 1000)
        if nxt > until_ms:
            break
        plan, count = nxt, count + 1
    return plan, count


def default_state_path() -> Path:
    from raven.home import raven_home

    return raven_home() / "plugin_schedules.json"


class ScheduleBook:
    """The durable schedule rows and the run journal."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_path()
        self.journal_path = self.path.with_name("plugin_schedule_runs.jsonl")

    def rows(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return dict(raw.get("schedules") or {})

    def _save(self, rows: dict[str, dict[str, Any]]) -> None:
        atomic_replace(self.path, json.dumps({"schedules": rows}, ensure_ascii=False, indent=2))

    def log(self, event: str, key: str, **data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": int(time.time() * 1000), "event": event, "hook": key, **data}
        locked_append(self.journal_path, [json.dumps(row, ensure_ascii=False)])

    def journal(self, limit: int = 50, key: str | None = None) -> list[dict[str, Any]]:
        try:
            lines = self.journal_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if key is None or row.get("hook") == key:
                out.append(row)
        return out[-limit:] if limit > 0 else out

    def reconcile(self, active: dict[str, dict[str, Any]], now_ms: int) -> dict[str, dict[str, Any]]:
        """Make the rows match the active hooks; ``active`` maps key -> plugin_id, name, version, cron, timezone."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with write_transaction(self.path):
            rows = self.rows()
            for key, want in active.items():
                row = rows.get(key)
                same_clock = row is not None and row["cron"] == want["cron"] and row["timezone"] == want["timezone"]
                if same_clock and row.get("enabled"):
                    row["version"] = want["version"]
                    continue
                reason = "new" if row is None else ("re-enabled" if same_clock else "schedule changed")
                rows[key] = {
                    **(row or {}),
                    **want,
                    "generation": uuid.uuid4().hex[:12],
                    "enabled": True,
                    "next_run_ms": next_after(want["cron"], want["timezone"], now_ms),
                    "delivered": [],
                }
                self.log("scheduled", key, reason=reason, next_run_ms=rows[key]["next_run_ms"])
            for key, row in rows.items():
                if key not in active and row.get("enabled"):
                    row["enabled"], row["next_run_ms"] = False, None
                    self.log("unscheduled", key, reason="its plugin is not active")
            self._save(rows)
            return rows

    def claim(self, key: str, now_ms: int, missed: str) -> ScheduledFire | None | str:
        """Take the due occurrence of ``key`` for this caller, advancing the row; or say why not.

        Returns the fire to run, ``None`` when nothing is due, or a string naming
        an occurrence that was dropped (``missed = "skip"``).
        """
        with write_transaction(self.path):
            rows = self.rows()
            row = rows.get(key)
            if row is None or not row.get("enabled") or row.get("next_run_ms") is None:
                return None
            first = int(row["next_run_ms"])
            if first > now_ms:
                return None
            plan, count = latest_due(row["cron"], row["timezone"], first, now_ms)
            delivery = hashlib.sha1(f"{key}|{row['generation']}|{plan}".encode()).hexdigest()[:16]
            row["next_run_ms"] = next_after(row["cron"], row["timezone"], max(plan, now_ms))
            if delivery in row.get("delivered", []):
                self._save(rows)
                return None
            row["delivered"] = [*row.get("delivered", []), delivery][-KEEP_DELIVERED:]
            if missed == "skip" and now_ms - plan > LATE_GRACE_MS:
                self._save(rows)
                return f"occurrence {plan} came {now_ms - plan} ms late and the hook skips missed runs"
            row["last_planned_ms"] = plan
            self._save(rows)
            return ScheduledFire(
                key=key,
                plugin_id=row["plugin_id"],
                name=row["name"],
                planned_ms=plan,
                generation=row["generation"],
                delivery_id=delivery,
                coalesced=count,
            )

    def record(self, key: str, status: str, detail: str, fire: ScheduledFire, elapsed_ms: int) -> None:
        with write_transaction(self.path):
            rows = self.rows()
            if (row := rows.get(key)) is not None:
                row.update(
                    last_run_ms=int(time.time() * 1000),
                    last_status=status,
                    last_detail=detail[:300],
                )
                self._save(rows)
        self.log(
            status,
            key,
            planned_ms=fire.planned_ms,
            coalesced=fire.coalesced,
            delivery=fire.delivery_id,
            generation=fire.generation,
            manual=fire.manual,
            elapsed_ms=elapsed_ms,
            detail=detail[:300],
        )


class PluginScheduler:
    """Fires the active plugins' scheduled hooks; started and stopped like a plugin service."""

    contributed_by = None

    def __init__(
        self,
        specs: dict[str, Any],
        build: Callable[[str], ScheduledHook | None],
        *,
        versions: dict[str, tuple[str, str]],
        book: ScheduleBook | None = None,
        now_fn: Callable[[], int] | None = None,
        max_sleep_s: float = 30.0,
    ) -> None:
        self.specs = specs
        """key -> the manifest's scheduled entry (cron, timezone, missed, timeout_s)."""
        self.versions = versions
        """key -> (plugin id, plugin version)."""
        self.book = book or ScheduleBook()
        self._build = build
        self._hooks: dict[str, ScheduledHook | None] = {}
        self._now = now_fn or (lambda: int(time.time() * 1000))
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self.max_sleep_s = max_sleep_s

    def reconcile(self) -> dict[str, dict[str, Any]]:
        active = {
            key: {
                "plugin_id": self.versions[key][0],
                "name": key.split(":", 1)[1],
                "version": self.versions[key][1],
                "cron": spec.cron,
                "timezone": spec.timezone,
            }
            for key, spec in self.specs.items()
        }
        return self.book.reconcile(active, self._now())

    def _hook(self, key: str) -> ScheduledHook | None:
        if key not in self._hooks:
            try:
                self._hooks[key] = self._build(key)
            except Exception as exc:  # noqa: BLE001 - a hook that cannot be built is reported, not fatal
                logger.warning("scheduled hook %s could not be built: %s", key, exc)
                self.book.log("error", key, detail=f"the hook could not be built: {exc}"[:300])
                self._hooks[key] = None
        return self._hooks[key]

    async def tick(self) -> list[tuple[str, str]]:
        """Fire every hook that is due now; returns (key, status) for each one that was."""
        out: list[tuple[str, str]] = []
        for key, spec in self.specs.items():
            claimed = self.book.claim(key, self._now(), spec.missed)
            if claimed is None:
                continue
            if isinstance(claimed, str):
                self.book.log("skipped", key, detail=claimed)
                out.append((key, "skipped"))
                continue
            out.append((key, await self._run(key, claimed)))
        return out

    async def fire_now(self, key: str) -> str:
        """Run ``key`` once now, outside its schedule (``rarus plugins hooks run``)."""
        row = self.book.rows().get(key) or {}
        now = self._now()
        fire = ScheduledFire(
            key=key,
            plugin_id=self.versions[key][0],
            name=key.split(":", 1)[1],
            planned_ms=now,
            generation=str(row.get("generation") or "manual"),
            delivery_id=f"manual-{uuid.uuid4().hex[:8]}",
            manual=True,
        )
        return await self._run(key, fire)

    async def _run(self, key: str, fire: ScheduledFire) -> str:
        hook = self._hook(key)
        started = time.monotonic()
        if hook is None:
            status, detail = "error", "the hook could not be built"
        else:
            try:
                result = await asyncio.wait_for(hook.run(fire), timeout=self.specs[key].timeout_s)
                status, detail = "ok", str(result or "")
            except asyncio.TimeoutError:
                status, detail = "timeout", f"no result in {self.specs[key].timeout_s:.0f}s"
            except Exception as exc:  # noqa: BLE001 - a failing hook is journalled; the scheduler goes on
                status, detail = "error", f"{type(exc).__name__}: {exc}"
        self.book.record(key, status, detail, fire, int((time.monotonic() - started) * 1000))
        return status

    def seconds_to_next(self) -> float:
        rows = self.book.rows()
        due = [
            int(r["next_run_ms"])
            for k, r in rows.items()
            if k in self.specs and r.get("enabled") and r.get("next_run_ms")
        ]
        if not due:
            return self.max_sleep_s
        return max(0.0, min(self.max_sleep_s, (min(due) - self._now()) / 1000))

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - one bad tick must not end the schedule
                logger.exception("plugin scheduler tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.seconds_to_next() + 0.05)
            except asyncio.TimeoutError:
                pass

    async def start(self, handles: Any = None) -> None:
        self.reconcile()
        if self._task is None and self.specs:
            self._task = asyncio.create_task(self._loop(), name="plugin-scheduler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


__all__ = [
    "PluginScheduler",
    "ScheduleBook",
    "ScheduledFire",
    "ScheduledHook",
    "latest_due",
    "next_after",
]

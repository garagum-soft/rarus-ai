"""The sync pass: bringing a table in step with its sources.

One sync of one source reads all of its rows and compares them with the rows
that source brought last time, by the table's key: a key it did not have is
added, a key whose values moved is changed, a key it no longer has is removed.
The source is the truth for the rows it owns, so a local edit to one of them is
overwritten by the next sync -- and is reported as a change. A row entered here,
or brought by another source, is never touched: a source row whose key one of
those already holds is a conflict, reported and skipped. A source that cannot
be read leaves the table as it was.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from raven.tables.sources import fetch_rows
from raven.tables.store import LOG_KEEP, SOURCE_FIELD, Source, Table, TableError, TableStore

Fetch = Callable[[Source], Awaitable[list[dict[str, Any]]]]


@dataclass
class SyncResult:
    table: str
    source: str
    at: str
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    skipped: int = 0
    unchanged: int = 0
    error: str | None = None

    def summary(self) -> str:
        if self.error:
            return f"{self.table}/{self.source}: not synced ({self.error})"
        parts = [f"+{len(self.added)}", f"~{len(self.changed)}", f"-{len(self.removed)}", f"={self.unchanged}"]
        extra = f", {len(self.conflicts)} conflict(s)" if self.conflicts else ""
        extra += f", {self.skipped} without a key" if self.skipped else ""
        return f"{self.table}/{self.source}: {' '.join(parts)}{extra}"


def _mapped(source: Source, row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k not in source.mapping.values() and k != SOURCE_FIELD}
    for column, src_field in source.mapping.items():
        if src_field in row:
            out[column] = row[src_field]
    return out


def apply(table: Table, source: Source, incoming: Sequence[dict[str, Any]], *, now: datetime) -> SyncResult:
    """One sync of ``source``'s rows into ``table``, in place."""
    result = SyncResult(table=table.name, source=source.name, at=now.isoformat(timespec="seconds"))
    by_key = {str(r.get(table.key)): r for r in table.rows}
    seen: set[str] = set()
    for raw in incoming:
        row = _mapped(source, raw)
        if row.get(table.key) in (None, ""):
            result.skipped += 1
            continue
        key = str(row[table.key])
        if key in seen:
            result.skipped += 1
            continue
        seen.add(key)
        for column in row:
            if column not in table.columns:
                table.columns.append(column)
        current = by_key.get(key)
        if current is None:
            new = {**row, SOURCE_FIELD: source.name}
            table.rows.append(new)
            by_key[key] = new
            result.added.append(key)
        elif current.get(SOURCE_FIELD) != source.name:
            result.conflicts.append(key)
        elif any(current.get(c) != v for c, v in row.items()):
            current.update(row)
            result.changed.append(key)
        else:
            result.unchanged += 1
    gone = {k for k, r in by_key.items() if r.get(SOURCE_FIELD) == source.name and k not in seen}
    if gone:
        table.rows = [r for r in table.rows if str(r.get(table.key)) not in gone]
        result.removed = sorted(gone)
    # Full precision, unlike the log's ``at``: ``due`` measures intervals from it.
    source.last_synced = now.isoformat()
    return result


def _log(table: Table, result: SyncResult) -> None:
    table.log = [*table.log, asdict(result)][-LOG_KEEP:]


async def sync_table(
    store: TableStore,
    name: str,
    *,
    sources: Sequence[str] | None = None,
    fetch: Fetch = fetch_rows,
    now: datetime | None = None,
) -> list[SyncResult]:
    """Sync the named sources (default: all) of one table and save it once."""
    table = store.load(name)
    chosen = [table.source(s) for s in sources] if sources else list(table.sources)
    results = []
    for source in chosen:
        when = now or datetime.now()
        try:
            rows = await fetch(source)
        except (TableError, OSError, asyncio.TimeoutError) as exc:
            result = SyncResult(table=name, source=source.name, at=when.isoformat(timespec="seconds"), error=str(exc))
        except Exception as exc:  # noqa: BLE001 - one source failing must not stop the next
            result = SyncResult(table=name, source=source.name, at=when.isoformat(timespec="seconds"), error=repr(exc))
        else:
            result = apply(table, source, rows, now=when)
        _log(table, result)
        results.append(result)
    store.save(table)
    return results


def due(table: Table, now: datetime) -> list[str]:
    """The sources whose own interval has elapsed since their last sync."""
    names = []
    for source in table.sources:
        if not source.every:
            continue
        last = datetime.fromisoformat(source.last_synced) if source.last_synced else None
        if last is None or (now - last).total_seconds() >= source.every:
            names.append(source.name)
    return names


@dataclass
class TableSyncPass:
    """The gateway's pass: every ``interval_s``, sync each table's due sources."""

    store: Callable[[], TableStore]
    interval_s: float = 60.0
    fetch: Fetch = fetch_rows
    _task: asyncio.Task | None = None

    async def run_once(self, now: datetime | None = None) -> list[SyncResult]:
        store = self.store()
        when = now or datetime.now()
        results: list[SyncResult] = []
        for name in store.names():
            try:
                names = due(store.load(name), when)
                if names:
                    results += await sync_table(store, name, sources=names, fetch=self.fetch, now=when)
            except TableError as exc:
                logger.warning("table sync: {}: {}", name, exc)
        for result in results:
            logger.info("table sync: {}", result.summary())
        return results

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - one failed pass must not end the next
                logger.warning("table sync pass failed: {}", exc)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="table-sync")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


__all__ = ["SyncResult", "TableSyncPass", "apply", "due", "sync_table"]

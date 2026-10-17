"""Table apps on disk: one JSON file per table under ``<RARUS_HOME>/tables``.

A table has a key column, its columns in order, its rows, the outside sources it
syncs from, and a short log of past syncs. Each row remembers which source it
came from (``_source``), or none when it was entered here: a sync owns the rows
its source brought and never touches a row it does not own.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SOURCE_FIELD = "_source"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
LOG_KEEP = 20


class TableError(ValueError):
    """A table or source that cannot be what was asked."""


@dataclass
class Source:
    """Where rows come from: ``kind`` csv or json, ``location`` a file path or an http(s) URL.

    ``path`` is the dotted path to the list inside a JSON document; ``mapping``
    maps a table column to the source field it is read from (unmapped columns
    read the field of the same name); ``every`` is how many seconds the
    gateway's pass leaves between syncs of this source (``None``: on demand only).
    """

    name: str
    kind: str
    location: str
    path: str = ""
    mapping: dict[str, str] = field(default_factory=dict)
    every: float | None = None
    last_synced: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"csv", "json"}:
            raise TableError(f"source kind must be csv or json, not {self.kind!r}")
        if not _NAME.match(self.name):
            raise TableError(f"bad source name {self.name!r}")


@dataclass
class Table:
    name: str
    key: str
    columns: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)

    def source(self, name: str) -> Source:
        for src in self.sources:
            if src.name == name:
                return src
        raise TableError(f"table {self.name!r} has no source {name!r}")

    def row(self, key: str) -> dict[str, Any] | None:
        return next((r for r in self.rows if str(r.get(self.key)) == key), None)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Table:
        sources = [Source(**s) for s in data.get("sources") or []]
        return cls(
            name=data["name"],
            key=data["key"],
            columns=list(data.get("columns") or []),
            rows=list(data.get("rows") or []),
            sources=sources,
            log=list(data.get("log") or []),
        )


class TableStore:
    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            from raven.home import raven_home

            root = raven_home() / "tables"
        self.root = root

    def _path(self, name: str) -> Path:
        if not _NAME.match(name):
            raise TableError(f"bad table name {name!r}")
        return self.root / f"{name}.json"

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json")) if self.root.is_dir() else []

    def exists(self, name: str) -> bool:
        return self._path(name).is_file()

    def create(self, name: str, key: str, columns: list[str] | None = None) -> Table:
        if self.exists(name):
            raise TableError(f"table {name!r} already exists")
        cols = [key, *[c for c in columns or [] if c != key]]
        table = Table(name=name, key=key, columns=cols)
        self.save(table)
        return table

    def load(self, name: str) -> Table:
        path = self._path(name)
        try:
            return Table.from_json(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            raise TableError(f"no table {name!r}") from None

    def save(self, table: Table) -> None:
        path = self._path(table.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(table.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def add_source(self, name: str, source: Source) -> Table:
        table = self.load(name)
        if any(s.name == source.name for s in table.sources):
            raise TableError(f"table {name!r} already has a source {source.name!r}")
        table.sources.append(source)
        self.save(table)
        return table

    def set_cell(self, name: str, key: str, column: str, value: Any) -> Table:
        """A local edit. A row the table does not have yet is entered as a local row."""
        table = self.load(name)
        if column == SOURCE_FIELD:
            raise TableError(f"{SOURCE_FIELD} is the table's own bookkeeping")
        row = table.row(key)
        if row is None:
            row = {table.key: key, SOURCE_FIELD: None}
            table.rows.append(row)
        row[column] = value
        if column not in table.columns:
            table.columns.append(column)
        self.save(table)
        return table


__all__ = ["LOG_KEEP", "SOURCE_FIELD", "Source", "Table", "TableError", "TableStore"]
